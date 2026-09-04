"""Account-free Claude peer fixture, for subprocess transport/lifecycle tests."""
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import uuid

root = Path(sys.argv[1])
registry = root / 'sessions'
registry.mkdir()
path = root / f'{os.getpid()}.sock'
session_id = str(uuid.uuid4())
started = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()[19]
record = {'pid': os.getpid(), 'procStart': started, 'sessionId': session_id,
          'name': 'test-peer', 'cwd': str(root), 'version': '2.1.261',
          'peerProtocol': 1, 'messagingSocketPath': str(path)}
key = registry / f'{os.getpid()}.{hashlib.sha256(str(path).encode()).hexdigest()}.key'
key.write_text(json.dumps({'peerToken': 'a' * 32, 'procStart': started}))
key.chmod(0o600)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
with socket.socket(socket.AF_UNIX) as server:
    server.bind(str(path))
    server.listen(4)
    (registry / f'{os.getpid()}.json').write_text(json.dumps(record))
    (root / 'ready.json').write_text(json.dumps(record))
    while True:
        with server.accept()[0] as connection:
            connection.settimeout(5)
            frames = [json.loads(line) for line in connection.makefile('rb')]
        for frame in frames:
            if frame.get('type') != 'user':
                continue
            content = frame['message']['content']
            meta_line = next(line for line in content.splitlines() if line.startswith('PROJECT_RELAY '))
            meta = json.loads(meta_line[len('PROJECT_RELAY '):])
            body = 'PROJECT_RELAY ' + json.dumps({'thread': meta['thread'], 'reply_to': frame['msg_id']}) + '\nFixture reply'
            reply = {'type': 'user', 'msg_id': str(uuid.uuid4()),
                     'message': {'role': 'user', 'content': body}}
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5)
                client.connect(frame['from'][4:])
                client.sendall((json.dumps(reply) + '\n').encode())
                client.shutdown(socket.SHUT_WR)
                while client.recv(1024):
                    pass
