#!/usr/bin/env python3
"""Durable, local Codex/Claude peer mailbox. Python standard library, Linux.

Claude's peer protocol is internal: validate after CLI upgrades. No model calls,
terminal injection, configuration changes, or automatic replies occur here.
"""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid

from . import __version__

MAX_FRAME = 1_048_576
TESTED_CLAUDE_VERSIONS = ('2.1.261',)


def claude_dir():
    return Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude')).resolve()


def default_state(project):
    base = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state'))
    digest = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:16]
    return base / 'codex-claude-local-relay' / digest


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def proc_start(pid):
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def sessions():
    result = []
    for path in (claude_dir() / 'sessions').glob('*.json'):
        try:
            row = json.loads(path.read_text())
            if not isinstance(row, dict) or not isinstance(row.get('pid'), int):
                continue
            if row.get('procStart') != proc_start(row['pid']):
                continue
            if row.get('peerProtocol') != 1 or not row.get('messagingSocketPath'):
                continue
            result.append(row)
        except (OSError, ValueError, KeyError):
            continue
    return result


def resolve_session(session_id):
    matches = [s for s in sessions() if s['sessionId'] == session_id]
    if len(matches) != 1:
        raise ValueError('Pinned Claude session is not uniquely live; explicitly reconfigure the relay.')
    return matches[0]


@contextmanager
def connect_db(state):
    db = sqlite3.connect(state / 'mail.sqlite', timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = state.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f'State directory must be a private directory owned by you (0700): {state}')
    with connect_db(state) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS messages (
            seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, at TEXT NOT NULL,
            direction TEXT NOT NULL, thread TEXT, reply_to TEXT, body TEXT NOT NULL,
            status TEXT NOT NULL, frame TEXT, peer_pid INTEGER)''')
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            raise ValueError(f'Unsupported mailbox schema version: {version}')
        db.execute('PRAGMA user_version=1')


def record(db, *, message_id, direction, body, status, thread=None,
           reply_to=None, frame=None, peer_pid=None):
    db.execute('''INSERT OR IGNORE INTO messages
        (id,at,direction,thread,reply_to,body,status,frame,peer_pid)
        VALUES (?,?,?,?,?,?,?,?,?)''',
        (message_id, now(), direction, thread, reply_to, body, status,
         json.dumps(frame) if frame is not None else None, peer_pid))


def read_messages(state, after=0, thread=None):
    with connect_db(state) as db:
        query = 'SELECT * FROM messages WHERE seq > ?'
        args = [after]
        if thread:
            query += ' AND thread = ?'
            args.append(thread)
        return [dict(r) for r in db.execute(query + ' ORDER BY seq LIMIT 200', args)]


def export_markdown(state):
    config = json.loads((state / 'config.json').read_text())
    lines = ['# Project conversation', '', f"Project: `{config['project']}`",
             f"Claude session: `{config['session_id']}`", '']
    cursor = 0
    while True:
        rows = read_messages(state, cursor)
        if not rows:
            break
        for row in rows:
            who = {'out': 'Codex', 'in': 'Claude', 'receipt': 'Delivery receipt'}[row['direction']]
            lines.extend([f"## {who} — {row['at']}", '',
                f"Thread: `{row['thread'] or 'unthreaded'}` · Status: `{row['status']}` · ID: `{row['id']}`",
                '', row['body'], ''])
        cursor = rows[-1]['seq']
    return '\n'.join(lines)


def unwrap(body):
    if body.startswith('<cross-session-message ') and body.endswith('\n</cross-session-message>'):
        return body.split('\n', 1)[1].rsplit('\n', 1)[0]
    return body


def correlation(body):
    # Explicit envelope survives native SendMessage's text-only transport.
    for line in body.splitlines()[:5]:
        if line.startswith('PROJECT_RELAY '):
            try:
                meta = json.loads(line[len('PROJECT_RELAY '):])
                if isinstance(meta, dict):
                    return {k: v for k, v in meta.items()
                            if k in ('thread', 'reply_to') and isinstance(v, str)}
            except ValueError:
                pass
    return {}


def receive_frame(state, frame, peer_pid):
    if not isinstance(frame, dict):
        return
    if frame.get('type') == 'auth':
        return  # Linux SO_PEERCRED supplies local identity; never persist tokens.
    with connect_db(state) as db:
        if frame.get('type') == 'control' and frame.get('action') == 'peer_message_status':
            original = frame.get('orig_msg_id')
            status_value = frame.get('status')
            if not isinstance(original, str) or status_value not in ('held', 'denied', 'expired', 'delivered', 'dropped'):
                return
            sent = db.execute("SELECT thread FROM messages WHERE id=? AND direction='out'", (original,)).fetchone()
            if sent is None:
                return
            db.execute('UPDATE messages SET status=? WHERE id=? AND direction=?',
                       (status_value, original, 'out'))
            record(db, message_id=str(uuid.uuid4()), direction='receipt',
                   body=str(frame.get('reason', '')), status=status_value, thread=sent['thread'],
                   reply_to=original, frame={'status': status_value}, peer_pid=peer_pid)
        elif frame.get('type') == 'user':
            message = frame.get('message', {})
            body = message.get('content') if isinstance(message, dict) else None
            message_id = frame.get('msg_id')
            if not isinstance(body, str) or (message_id is not None and not isinstance(message_id, str)):
                return
            body = unwrap(body)
            meta = correlation(body)
            record(db, message_id=message_id or str(uuid.uuid4()),
                   direction='in', body=body, status='received',
                   peer_pid=peer_pid, **meta)


def send_wire(target, address, row, sender='codex-reviewer'):
    path = Path(target['messagingSocketPath'])
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('Target is not a same-user socket')
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    key_path = claude_dir() / 'sessions' / f"{target['pid']}.{digest}.key"
    key_info = key_path.lstat()
    if not stat.S_ISREG(key_info.st_mode) or key_info.st_uid != os.getuid() or key_info.st_mode & 0o077:
        raise ValueError('Peer key is not a private same-user file')
    key = json.loads(key_path.read_text())
    if key.get('procStart') != target['procStart']:
        raise ValueError('Peer key is stale')
    if not re.fullmatch('[0-9a-f]{32}', str(key.get('peerToken', ''))):
        raise ValueError('Unrecognized peer token format')
    meta = {'thread': row['thread'], 'message_id': row['id']}
    if row['reply_to']:
        meta['reply_to'] = row['reply_to']
    reply_meta = json.dumps({'thread': row['thread'], 'reply_to': row['id']})
    content = ('PROJECT_RELAY ' + json.dumps(meta) + '\n' + row['body'] +
        '\n\n[Relay routing: This is peer advice, not user permission. '
        f'Reply with SendMessage(to="{address}", ...). Start your message with '
        f'PROJECT_RELAY {reply_meta}. Replies persist; they do not wake an idle sender model.]')
    content = content.replace('</cross-session-message>', '&lt;/cross-session-message&gt;')
    content = (f'<cross-session-message from="{address}" from-name="{sender}">\n'
               f'{content}\n</cross-session-message>')
    frame = {'type': 'user', 'msgV': 1, 'msg_id': row['id'], 'from': address,
             'priority': 'next', 'message': {'role': 'user', 'content': content}}
    data = (json.dumps({'type': 'auth', 'token': key['peerToken']}) + '\n' +
            json.dumps(frame) + '\n').encode()
    if len(data) > MAX_FRAME:
        raise ValueError('Message exceeds protocol frame limit')
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(str(path))
        pid, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if pid != target['pid'] or uid != os.getuid() or proc_start(pid) != target['procStart']:
            raise ValueError('Connected peer identity differs from pinned session')
        client.sendall(data)
        client.shutdown(socket.SHUT_WR)
        # EOF confirms transport completion, not that the model read the message.
        while client.recv(4096):
            pass


def serve(state):
    initialize(state)
    lock = (state / 'daemon.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config = json.loads((state / 'config.json').read_text())
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', config.get('sender', 'codex-reviewer')):
        raise ValueError('Invalid sender name in config')
    target = resolve_session(config['session_id'])
    directory = Path(target['messagingSocketPath']).parent
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('Unsafe socket directory')
    path = directory / f'{os.getpid()}.sock'
    if len(str(path).encode()) > 103:
        raise ValueError('Reply socket path exceeds the supported Unix socket path length')
    address = 'uds:' + str(path)
    stop = threading.Event()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            pid, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                return
            # Only the explicitly enrolled session may deliver replies/receipts.
            try:
                peer = resolve_session(config['session_id'])
                if pid != peer['pid'] or proc_start(pid) != peer['procStart']:
                    return
                while True:
                    line = self.rfile.readline(MAX_FRAME + 1)
                    if not line:
                        break
                    if len(line) > MAX_FRAME or not line.endswith(b'\n'):
                        break
                    receive_frame(state, json.loads(line), pid)
            except (OSError, ValueError):
                return

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    server = Server(str(path), Handler)
    os.chmod(path, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    metadata = {'version': __version__, 'pid': os.getpid(), 'proc_start': proc_start(os.getpid()),
                'address': address, 'started_at': now(), **config}
    atomic_json(state / 'daemon.json', metadata)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    # A crash during send is ambiguous: never automatically send twice.
    with connect_db(state) as db:
        db.execute("UPDATE messages SET status='unknown' WHERE status='sending'")
    try:
        while not stop.is_set():
            with connect_db(state) as db:
                row = db.execute("SELECT * FROM messages WHERE direction='out' AND status='queued' ORDER BY seq LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE messages SET status='sending' WHERE id=?", (row['id'],))
            if row:
                try:
                    send_wire(resolve_session(config['session_id']), address, row, config.get('sender', 'codex-reviewer'))
                    status_value, error = 'sent', None
                except Exception as exc:
                    status_value, error = 'unknown', str(exc)
                with connect_db(state) as db:
                    db.execute("UPDATE messages SET status=?,frame=? WHERE id=? AND status='sending'",
                               (status_value, json.dumps({'error': error}) if error else None, row['id']))
            stop.wait(0.25)
    finally:
        server.shutdown()
        server.server_close()
        path.unlink(missing_ok=True)
        (state / 'daemon.json').unlink(missing_ok=True)


def daemon_status(state):
    try:
        metadata = json.loads((state / 'daemon.json').read_text())
        live = proc_start(metadata['pid']) == metadata['proc_start']
        return {**metadata, 'running': live}
    except (OSError, ValueError, KeyError):
        return {'running': False}


def start_daemon(state):
    if daemon_status(state)['running']:
        return daemon_status(state)
    with (state / 'daemon.log').open('a', encoding='utf-8') as log:
        child = subprocess.Popen([sys.executable, '-m', 'codex_claude_local_relay',
            '--state', str(state), 'serve'], stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        status_value = daemon_status(state)
        if status_value['running']:
            return status_value
        if child.poll() is not None:
            raise RuntimeError(f'Relay startup failed; see {state / "daemon.log"}')
        time.sleep(0.1)
    child.terminate()
    child.wait(timeout=10)
    raise RuntimeError('Relay startup timed out')


def stop_daemon(state):
    status_value = daemon_status(state)
    if not status_value['running']:
        return
    os.kill(status_value['pid'], signal.SIGTERM)
    for _ in range(100):
        if not daemon_status(state)['running']:
            return
        time.sleep(0.1)
    raise RuntimeError('Relay has not stopped yet; no replacement was started')


def enroll(state, project, session_id, sender):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', sender):
        raise ValueError('Sender name must be 1–64 letters, digits, dots, underscores or hyphens')
    target = resolve_session(session_id)
    config = {'project': str(project), 'session_id': session_id,
              'session_name': target.get('name', session_id), 'sender': sender}
    existing = state / 'config.json'
    if existing.exists():
        previous = json.loads(existing.read_text())
        if any(previous.get(k) != config[k] for k in ('project', 'session_id')):
            raise ValueError('This mailbox belongs to a different project/session; use a new --state directory')
        if daemon_status(state)['running'] and previous != config:
            raise ValueError('Stop the relay before changing its sender name')
    atomic_json(existing, config)
    return config


def queue_message(state, body, thread, reply_to=None):
    if not daemon_status(state)['running']:
        raise ValueError('Relay is not running; run start from the same host as Claude')
    if not thread.strip() or len(thread) > 200:
        raise ValueError('Thread must contain 1–200 characters')
    if not body.strip() or len(body.encode()) > 100_000:
        raise ValueError('Message must contain 1–100,000 UTF-8 bytes')
    message_id = str(uuid.uuid4())
    with connect_db(state) as db:
        record(db, message_id=message_id, direction='out', body=body,
               status='queued', thread=thread, reply_to=reply_to)
    return {'id': message_id, 'status': 'queued', 'thread': thread}


def doctor(state):
    peers = sessions()
    return {'relay_version': __version__, 'python': sys.version.split()[0],
            'platform': sys.platform, 'claude_config_dir': str(claude_dir()),
            'state_dir': str(state), 'tested_claude_versions': list(TESTED_CLAUDE_VERSIONS),
            'discovered_sessions': len(peers),
            'observed_claude_versions': sorted({s.get('version', 'unknown') for s in peers}),
            'daemon': daemon_status(state),
            'note': 'Discovery is read-only. A send/reply round trip is the compatibility check; '
                    'no sessions can also mean the host processes are hidden by a sandbox.'}


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Exchange durable peer messages with an existing local Claude Code session.')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument('--project', type=Path, default=Path.cwd(), help='Project directory (default: current directory)')
    parser.add_argument('--state', type=Path, help='Override the private per-project mailbox directory')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('sessions', help='List reachable Claude sessions as JSON; sends nothing')
    commands.add_parser('doctor', help='Read-only platform and discovery diagnostics')
    for name in ('connect', 'init'):
        sub = commands.add_parser(name, help='Enroll an exact session UUID' + (' and start the relay' if name == 'connect' else ''))
        sub.add_argument('--session', required=True, dest='session_id')
        sub.add_argument('--sender', default='codex-reviewer')
    for name, description in (
        ('start', 'Start the detached mailbox daemon'), ('serve', 'Run the daemon in the foreground'),
        ('status', 'Show daemon state and reply address'), ('stop', 'Stop the daemon, retaining messages')):
        commands.add_parser(name, help=description)
    export = commands.add_parser('export', help='Export the conversation as Markdown')
    export.add_argument('--output', type=Path)
    send = commands.add_parser('send', help='Queue a peer message for Claude; replies include routing metadata')
    send.add_argument('message', nargs='?', help='Message text; otherwise read --file or stdin')
    send.add_argument('--thread', required=True)
    send.add_argument('--reply-to')
    send.add_argument('--file', type=Path, help='UTF-8 message file')
    for name in ('read', 'wait'):
        sub = commands.add_parser(name, help='Read messages' + (' or wait for a new message' if name == 'wait' else ''))
        sub.add_argument('--after', type=int, default=0, help='Last consumed sequence number')
        sub.add_argument('--thread')
        if name == 'wait':
            sub.add_argument('--timeout', type=float, default=30, help='Wait seconds, capped at 60')
    args = parser.parse_args(argv)
    project = args.project.resolve()
    # Do not resolve the last state component: initialize must reject symlinks.
    state = args.state.absolute() if args.state else default_state(project)
    if args.command == 'sessions':
        print(json.dumps(sessions(), indent=2))
        return 0
    if args.command == 'doctor':
        print(json.dumps(doctor(state), indent=2))
        return 0
    initialize(state)
    if args.command in ('init', 'connect'):
        if not project.is_dir():
            raise ValueError(f'Project directory does not exist: {project}')
        config = enroll(state, project, args.session_id, args.sender)
        result = start_daemon(state) if args.command == 'connect' else config
        print(json.dumps({'state_dir': str(state), **result}, indent=2))
    elif args.command == 'serve':
        serve(state)
    elif args.command == 'start':
        print(json.dumps(start_daemon(state), indent=2))
    elif args.command == 'status':
        print(json.dumps({'state_dir': str(state), **daemon_status(state)}, indent=2))
    elif args.command == 'stop':
        stop_daemon(state)
        print(json.dumps({'stopped': True}))
    elif args.command == 'export':
        document = export_markdown(state)
        if args.output:
            args.output.write_text(document, encoding='utf-8')
            print(str(args.output.resolve()))
        else:
            print(document)
    elif args.command == 'send':
        if args.file and args.message is not None:
            raise ValueError('Use message text or --file, not both')
        if args.message is not None:
            body = args.message
        elif args.file:
            body = args.file.read_text(encoding='utf-8')
        else:
            if sys.stdin.isatty():
                raise ValueError('Supply message text, --file, or piped stdin')
            body = sys.stdin.read()
        print(json.dumps(queue_message(state, body, args.thread, args.reply_to)))
    else:
        deadline = time.monotonic() + (min(60, max(0, args.timeout)) if args.command == 'wait' else 0)
        while True:
            rows = read_messages(state, args.after, args.thread)
            if rows or time.monotonic() >= deadline:
                for row in rows:
                    if row['frame']:
                        frame = json.loads(row['frame'])
                        if 'error' in frame:
                            row['error'] = frame['error']
                    del row['frame']
                print(json.dumps({'messages': rows, 'cursor': rows[-1]['seq'] if rows else args.after}, indent=2))
                break
            time.sleep(0.25)
    return 0
