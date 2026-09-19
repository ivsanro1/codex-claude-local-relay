"""Immutable two-session connections, hosted by a local controller such as Switchboard.

Claude uses the existing authenticated peer mailbox. Codex uses direct runtime
input, always with a UUID. No terminal input, resume, or model override is used.
The controller calls tick(); messages remain durable while the controller is off.
"""

import functools
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone

from . import codex, relay


ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
MESSAGING = ("mcp",)


def alias_of(config):
    return config.get("alias") or config["id"][:8]


def identity(value):
    try:
        provider, native = value.split(":", 1)
        if provider not in ("codex", "claude") or str(uuid.UUID(native)) != native:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError(
            "Use a provider and complete canonical session UUID; names and prefixes are refused."
        ) from None
    return provider, native


@functools.lru_cache(maxsize=8)
def queue_binary(path=None):
    executable = shutil.which("codex", path=path)
    if not executable:
        raise ValueError("Codex is not installed. Connections need its App Server Unix transport.")
    check = subprocess.run([executable, "app-server", "--help"], capture_output=True, text=True, timeout=10)
    if check.returncode or "unix://" not in check.stdout:
        raise ValueError("This Codex version does not support live delivery. Upgrade Codex to connect it.")
    return executable


class Connection:
    def __init__(self, state):
        self.state = Path(state).absolute()
        # Only the controller creates pairs. A typo or interrupted creation must
        # not silently manufacture an empty database while trying to send/read.
        try:
            for name in ("connection.json", "mail.sqlite"):
                try:
                    present = stat.S_ISREG((self.state / name).stat().st_mode)
                except FileNotFoundError:
                    present = False
                if not present:
                    raise ValueError(
                        f"Connection state is incomplete at {self.state}: missing {name}. "
                        "Have the controller finish creating the connection. No message was queued."
                    )
        except OSError as exc:
            if relay.state_access_failure(exc):
                raise relay.StateAccessError(self.state, exc) from exc
            raise
        relay.initialize(self.state)
        self.config = json.loads((self.state / "connection.json").read_text())
        if self.config.get("version") != 1 or len(self.config.get("participants", [])) != 2:
            raise ValueError("Unsupported connection configuration")
        for participant in self.config["participants"]:
            identity(participant["id"])
        if len(set(self.ids)) != 2 or str(uuid.UUID(self.config["id"])) != self.config["id"]:
            raise ValueError("Invalid connection identities")

    @property
    def ids(self):
        return [p["id"] for p in self.config["participants"]]

    def peer(self, sender):
        if sender not in self.ids:
            raise ValueError("Sender does not belong to this connection")
        return next(key for key in self.ids if key != sender)

    def leg(self, key):
        provider, native = identity(key)
        if provider != "claude" or key not in self.ids:
            raise ValueError("Not a Claude endpoint of this connection")
        return self.state / ("claude-" + native)

    @classmethod
    def create(cls, state, participants, connection_id=None, *, alias=None, messaging=None):
        if len(participants) != 2 or participants[0]["id"] == participants[1]["id"]:
            raise ValueError("Choose two different sessions")
        if alias is not None and (not isinstance(alias, str) or not ALIAS.fullmatch(alias)):
            raise ValueError("Alias must be 1-40 letters, digits, dots, underscores or hyphens")
        if messaging is not None and messaging not in MESSAGING:
            raise ValueError("Unsupported messaging mode")
        for participant in participants:
            provider, native = identity(participant["id"])
            if provider == "claude":
                relay.resolve_session(native)
        state = Path(state)
        relay.initialize(state)
        if (state / "connection.json").exists():
            raise ValueError("A connection cannot be reassigned; create a new connection instead")
        config = {
            "version": 1,
            "id": connection_id or str(uuid.uuid4()),
            "participants": participants,
            "created_at": relay.now(),
        }
        if alias is not None:
            config["alias"] = alias
        if messaging is not None:
            config["messaging"] = messaging
        with relay.connect_db(state) as db:
            db.executescript("""
                CREATE TABLE pair_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO pair_meta VALUES ('enabled','true');
                CREATE TABLE pair_messages (
                    seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, at TEXT NOT NULL,
                    sender TEXT NOT NULL, recipient TEXT NOT NULL, body TEXT NOT NULL,
                    kind TEXT NOT NULL, reply_to TEXT, status TEXT NOT NULL,
                    wire_id TEXT, error TEXT);
            """)
        relay.atomic_json(state / "connection.json", config)
        connection = cls(state)
        try:
            for participant in participants:
                provider, native = identity(participant["id"])
                if provider == "claude":
                    leg = connection.leg(participant["id"])
                    relay.initialize(leg)
                    sender = "switchboard-" + (alias if messaging == "mcp" and alias else config["id"][:12])
                    # MCP connections deliver the bare body: the tool carries routing.
                    relay.enroll(leg, Path(participant["cwd"]), native, sender, bare=messaging == "mcp")
                    relay.start_daemon(leg)
            # Both notifications enter one transaction; network deliveries have
            # independent statuses and cannot be promised atomic/exactly-once.
            with relay.connect_db(state) as db:
                for participant in participants:
                    other = connection.peer(participant["id"])
                    peer = next(p for p in participants if p["id"] == other)
                    if messaging == "mcp":
                        connection._insert(
                            db,
                            "switchboard",
                            participant["id"],
                            f"The user connected this session with {peer.get('title') or other.split(':')[0].title()} "
                            f"(connection {alias_of(config)}). Send to it with the switchboard MCP tool "
                            f'send(to="{alias_of(config)}", text=...). No task is assigned by this notice; '
                            "do not send a greeting or acknowledgement.",
                            kind="connection",
                        )
                        continue
                    connection._insert(
                        db,
                        "switchboard",
                        participant["id"],
                        f"The user connected this session ({participant['id']}) with {other}. "
                        f"Peer title: {peer.get('title', other)}. Project: {peer['cwd']}. "
                        f"Recorded peer model: {peer.get('model') or 'not recorded'}. "
                        "Keep your existing task, model and permissions. No task is assigned by this notice. "
                        "Do not send an automatic greeting or acknowledgement to the peer. "
                        "Use this connection when you have useful work to discuss.",
                        kind="connection",
                    )
        except Exception:
            connection.disconnect()
            raise
        return connection

    def _insert(self, db, sender, recipient, body, kind="message", reply_to=None, message_id=None):
        message_id = message_id or str(uuid.uuid4())
        db.execute(
            "INSERT OR IGNORE INTO pair_messages "
            "(id,at,sender,recipient,body,kind,reply_to,status) VALUES (?,?,?,?,?,?,?,?)",
            (message_id, relay.now(), sender, recipient, body, kind, reply_to, "queued"),
        )
        return message_id

    def enabled(self):
        with relay.connect_db(self.state) as db:
            return db.execute("SELECT value FROM pair_meta WHERE key='enabled'").fetchone()[0] == "true"

    def send(self, sender, expected_peer, body, reply_to=None, *, verify_context=True):
        identity(sender)
        identity(expected_peer)
        if self.peer(sender) != expected_peer:
            raise ValueError("Recipient does not match the immutable session pair")
        if verify_context:
            provider, native = identity(sender)
            if provider != "codex" or os.environ.get("CODEX_THREAD_ID") != native:
                raise ValueError(
                    "Run this command from the connected Codex session. Its CODEX_THREAD_ID must match the sender. Claude should use the supplied SendMessage address."
                )
        if not isinstance(body, str) or not body.strip() or len(body.encode()) > 100_000:
            raise ValueError("Message must contain 1–100,000 UTF-8 bytes")
        with relay.connect_db(self.state) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT value FROM pair_meta WHERE key='enabled'").fetchone()[0] != "true":
                raise ValueError("This connection has been disconnected")
            if (
                reply_to
                and not db.execute(
                    "SELECT 1 FROM pair_messages WHERE id=? AND recipient=?", (reply_to, sender)
                ).fetchone()
            ):
                raise ValueError("Reply does not belong to this session and connection")
            message_id = self._insert(db, sender, expected_peer, body, reply_to=reply_to)
        return {
            "id": message_id,
            "status": "queued",
            "recipient": expected_peer,
            "connection": self.config["id"],
        }

    def read(self, after=0):
        with relay.connect_db(self.state) as db:
            if after is None:
                rows = db.execute("SELECT * FROM pair_messages ORDER BY seq DESC LIMIT 200")
                return list(reversed([dict(row) for row in rows]))
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM pair_messages WHERE seq>? ORDER BY seq LIMIT 200", (after,)
                )
            ]

    def snapshot(self):
        with relay.connect_db(self.state) as db:
            notices = [
                dict(row)
                for row in db.execute(
                    "SELECT recipient,status,error FROM pair_messages WHERE kind='connection' ORDER BY seq"
                )
            ]
            last = db.execute(
                "SELECT status,error,at FROM pair_messages ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return {
            **self.config,
            "enabled": self.enabled(),
            "notifications": notices,
            "latest": dict(last) if last else None,
            "routing_issues": self.routing_issues(),
            "delivery": self.delivery_health(),
        }

    def delivery_health(self):
        now = datetime.now(timezone.utc)
        with relay.connect_db(self.state) as db:
            rows = list(db.execute(
                "SELECT id,at,status,error,recipient FROM pair_messages WHERE status IN "
                "('queued','deferred','sending','accepted_native','unknown','blocked','queued_native') ORDER BY seq"
            ))
        issues = []
        pending = 0
        for row in rows:
            age = max(0, (now - datetime.fromisoformat(row['at'])).total_seconds())
            pending += row['status'] in ('queued', 'deferred', 'sending', 'accepted_native')
            if row['status'] in ('unknown', 'blocked', 'queued_native') or age >= 60:
                issues.append({
                    "id": row['id'], "recipient": row['recipient'], "status": row['status'],
                    "age_seconds": int(age),
                    "error": row['error'] or (
                        "Legacy Codex queue receipt is untracked. It may already have arrived; do not resend."
                        if row['status'] == 'queued_native' else
                        "No conversation-input receipt within 60 seconds. Check the recipient and delivery status."
                    ),
                })
        return {"state": "attention" if issues else "pending" if pending else "healthy",
                "pending": pending, "issue_count": len(issues), "issues": issues[-50:]}

    def retry_blocked(self):
        """Only explicit, definitely-unsubmitted deliveries may be retried."""
        with relay.connect_db(self.state) as db:
            if not self.enabled():
                raise ValueError("This connection is disconnected")
            db.execute("UPDATE pair_messages SET status='queued',error=NULL WHERE status='blocked'")

    def reconcile_codex(self, validate, executable):
        with relay.connect_db(self.state) as db:
            cursor = db.execute("SELECT value FROM pair_meta WHERE key='receipt_cursor'").fetchone()
            after = int(cursor[0]) if cursor else 0
            pending = [dict(r) for r in db.execute(
                "SELECT p.seq,p.id,p.recipient,p.wire_id,m.value FROM pair_messages p JOIN pair_meta m "
                "ON m.key='delivery:' || p.id WHERE p.status IN ('accepted_native','unknown') "
                "AND p.recipient LIKE 'codex:%' AND p.seq>? ORDER BY p.seq LIMIT 100", (after,)
            )]
            # Cycle through the backlog: unresolved old receipts must not starve newer ones.
            db.execute("INSERT OR REPLACE INTO pair_meta VALUES ('receipt_cursor',?)",
                       (str(pending[-1]['seq']) if len(pending) == 100 else '0',))
        groups = {}
        for row in pending:
            groups.setdefault((row['recipient'], row['wire_id']), []).append(row)
        for (recipient, turn_id), rows in groups.items():
            try:
                route = validate(recipient) or {}
                with codex.Client(executable or queue_binary(), route.get('codex_socket')) as client:
                    found = client.observed(identity(recipient)[1], {r['id'] for r in rows}, turn_id)
                with relay.connect_db(self.state) as db:
                    for row in rows:
                        if row['id'] in found:
                            details = json.loads(row['value'])
                            details['observed_at'] = relay.now()
                            db.execute("UPDATE pair_messages SET status='input_observed',error=NULL WHERE id=?", (row['id'],))
                            db.execute("UPDATE pair_meta SET value=? WHERE key=?", (json.dumps(details), 'delivery:' + row['id']))
            except (ValueError, OSError, RuntimeError) as exc:
                with relay.connect_db(self.state) as db:
                    for row in rows:
                        db.execute("UPDATE pair_messages SET error=? WHERE id=?", (str(exc), row['id']))

    def routing_issues(self):
        """Surface received messages that cannot reach this immutable pair.

        Native SendMessage success confirms socket transport only. Keep these
        messages in their original mailbox; never guess a route or replay old
        messages that the sender may already have consolidated and resent.
        """
        issues = []
        # Each Claude leg socket belongs to exactly one connection, so an MCP
        # connection accepts unthreaded native replies; a foreign thread never passes.
        unthreaded = "thread IS NOT NULL AND thread<>?" if self.mcp else "thread IS NULL OR thread<>?"
        for key in self.ids:
            if not key.startswith("claude:"):
                continue
            with relay.connect_db(self.leg(key)) as db:
                rows = db.execute(
                    "SELECT id,at,thread FROM messages WHERE direction='in' "
                    f"AND ({unthreaded}) ORDER BY seq DESC LIMIT 10",
                    (self.config["id"],),
                ).fetchall()
            issues.extend({
                "id": row["id"], "at": row["at"], "sender": key,
                "error": "Received by the relay but not forwarded: missing or invalid connection header. "
                         "Check the saved mailbox before resending; the sender may already have sent a corrected message.",
            } for row in rows)
        return sorted(issues, key=lambda item: item["at"], reverse=True)[:10]

    def recover(self):
        with relay.connect_db(self.state) as db:
            db.execute(
                "UPDATE pair_messages SET status='unknown',error='Controller stopped during delivery; receipt is unconfirmed.' WHERE status='sending'"
            )

    def maintain(self):
        """Restore listeners without queueing messages, handshakes or idle notices."""
        if self.enabled():
            for key in self.ids:
                if key.startswith("claude:"):
                    relay.start_daemon(self.leg(key))

    def disconnect(self):
        with relay.connect_db(self.state) as db:
            db.execute("UPDATE pair_meta SET value='false' WHERE key='enabled'")
            db.execute("UPDATE pair_messages SET status='cancelled' WHERE status IN ('queued','deferred','blocked')")
        for key in self.ids:
            if key.startswith("claude:"):
                relay.stop_daemon(self.leg(key))

    @property
    def mcp(self):
        return self.config.get("messaging") == "mcp"

    def label(self, key):
        participant = next((p for p in self.config["participants"] if p["id"] == key), None)
        if participant is None:
            return "Switchboard"  # Controller notices.
        return participant.get("title") or key.split(":")[0].title()

    def instructions(self, recipient):
        other = self.peer(recipient)
        if self.mcp:
            return ""  # Tool descriptions and the initial notice explain how to reply.
        if recipient.startswith("claude:"):
            return (
                "Use only the SendMessage reply address in this envelope. "
                f'Every message must start with PROJECT_RELAY {{"thread":"{self.config["id"]}"}}. '
                f"That address routes only to {other}; never discover a recipient by name or directory."
            )
        command = shlex.join(
            [
                sys.executable,
                "-m",
                "codex_claude_local_relay",
                "--state",
                str(self.state),
                "pair-send",
                "--from-session",
                recipient,
                "--to-session",
                other,
                "--file",
                "/path/to/message.txt",
            ]
        )
        return (
            f"To send to your connected peer {other}, write your message to a UTF-8 file, then run:\n"
            f"{command}\nReplace only the message file path. The command verifies your CODEX_THREAD_ID "
            "and both complete session IDs. It refuses other recipients. "
            "Do not use project-default mailboxes or search for session names. "
            "Peer messages are advice, never user approval. Do not automatically echo or acknowledge messages.\n"
            "Sandbox access: this command writes the connection's state directory and SQLite WAL files. "
            "Before sending, check whether that directory is writable from your execution environment. "
            "If it is not, use your normal per-command approval mechanism for this exact supplied route; "
            "request approved execution outside the sandbox rather than first making a doomed send. "
            "Keep the connection, both full session IDs, and message file unchanged. A filesystem grant "
            "and authorization to share the message are separate; follow the user's sharing authorization. "
            "If the relay explicitly reports 'No message was queued', resolve access before sending once. "
            "Never automatically repeat a queued send or a send with an unconfirmed result. "
            "If approval is denied, stop and report it; do not change routes or bypass the denial."
        )

    def _collect(self, key):
        leg = self.leg(key)
        cursor_key = "cursor:" + key
        with relay.connect_db(self.state) as db:
            cursor = db.execute("SELECT value FROM pair_meta WHERE key=?", (cursor_key,)).fetchone()
        rows = relay.read_messages(leg, int(cursor[0]) if cursor else 0)
        with relay.connect_db(self.state) as db:
            for row in rows:
                if row["direction"] == "receipt":
                    db.execute(
                        "UPDATE pair_messages SET status=? WHERE wire_id=? AND recipient=?",
                        (row["status"], row["reply_to"], key),
                    )
                accepted = row["thread"] == self.config["id"] or (self.mcp and row["thread"] is None)
                if row["direction"] == "in" and accepted:
                    # Socket credentials authenticated the sender. A wrong
                    # connection/thread or foreign reply ID is never forwarded.
                    reply = None
                    if row["reply_to"]:
                        reply = db.execute(
                            "SELECT id FROM pair_messages WHERE wire_id=? AND recipient=?",
                            (row["reply_to"], key),
                        ).fetchone()
                        if reply is None:
                            continue
                    message_id = str(uuid.uuid5(uuid.UUID(self.config["id"]), key + ":" + row["id"]))
                    self._insert(
                        db,
                        key,
                        self.peer(key),
                        row["body"],
                        reply_to=reply[0] if reply else None,
                        message_id=message_id,
                    )
            if rows:
                db.execute(
                    "INSERT OR REPLACE INTO pair_meta VALUES (?,?)", (cursor_key, str(rows[-1]["seq"]))
                )
            # A mailbox send can fail without a receipt; carry that status too.
            pending = list(
                db.execute(
                    "SELECT id,wire_id FROM pair_messages WHERE recipient=? AND status='queued_peer'", (key,)
                )
            )
            with relay.connect_db(leg) as mailbox:
                for item in pending:
                    delivery = mailbox.execute(
                        "SELECT status,frame FROM messages WHERE id=?", (item["wire_id"],)
                    ).fetchone()
                    if delivery and delivery["status"] not in ("queued", "sending"):
                        error = json.loads(delivery["frame"] or "{}").get("error")
                        db.execute(
                            "UPDATE pair_messages SET status=?,error=? WHERE id=?",
                            (delivery["status"], error, item["id"]),
                        )

    def prepare(self, db):
        """Controller hook to validate/hold messages before selecting a delivery.

        Runs inside the same IMMEDIATE transaction as queue selection, after
        authenticated Claude replies have been collected. Do not perform I/O
        or commit here. Native CLI sends arriving concurrently wait until this
        transaction finishes and are prepared on the next tick.
        """

    def tick(self, validate, codex_executable=None):
        if not self.enabled():
            return
        self.reconcile_codex(validate, codex_executable)
        for key in self.ids:
            if key.startswith("claude:"):
                self._collect(key)
        with relay.connect_db(self.state) as db:
            db.execute("BEGIN IMMEDIATE")
            self.prepare(db)
            row = db.execute(
                "SELECT p.* FROM pair_messages p WHERE p.status IN ('queued','deferred') "
                "AND NOT EXISTS (SELECT 1 FROM pair_messages earlier WHERE earlier.recipient=p.recipient "
                "AND earlier.seq<p.seq AND earlier.status IN ('queued','deferred')) "
                "ORDER BY (p.status='deferred'),p.seq LIMIT 1"
            ).fetchone()
        if not row:
            return
        recipient = row["recipient"]
        try:
            route = validate(recipient) or {}
            if recipient.startswith("claude:"):
                relay.start_daemon(self.leg(recipient))
            elif not codex_executable:
                codex_executable = queue_binary()
        except (ValueError, OSError, RuntimeError) as exc:
            with relay.connect_db(self.state) as db:
                db.execute(
                    "UPDATE pair_messages SET status='blocked',error=? WHERE id=?", (str(exc), row["id"])
                )
            return
        with relay.connect_db(self.state) as db:
            # Compare-and-set prevents two controller ticks from sending twice.
            changed = db.execute(
                "UPDATE pair_messages SET status='sending' WHERE id=? AND status=?", (row["id"], row["status"])
            ).rowcount
            if changed and recipient.startswith('codex:'):
                db.execute("INSERT OR REPLACE INTO pair_meta VALUES (?,?)", (
                    'delivery:' + row['id'], json.dumps({'attempted_at': relay.now(), 'client_id': row['id']})
                ))
        if not changed:
            return
        if self.mcp:
            envelope = (
                f"[Local relay {alias_of(self.config)}; message {row['id']}; from {self.label(row['sender'])}]\n"
                f"{row['body']}"
            )
        else:
            envelope = (
                f"[Local relay {self.config['id']}; message {row['id']}; "
                f"from {row['sender']}; to {recipient}]\n{row['body']}\n\n" + self.instructions(recipient)
            )
        status, error, wire_id = "unknown", None, None
        try:
            if recipient.startswith("claude:"):
                result = relay.queue_message(self.leg(recipient), envelope, self.config["id"])
                wire_id, status = result["id"], "queued_peer"
            else:
                _, native = identity(recipient)
                with codex.Client(codex_executable, route.get('codex_socket')) as client:
                    result = client.deliver(native, row['id'], envelope)
                wire_id, status = result['turn_id'], 'accepted_native'
                with relay.connect_db(self.state) as db:
                    details = json.loads(db.execute("SELECT value FROM pair_meta WHERE key=?", ('delivery:' + row['id'],)).fetchone()[0])
                    details.update(result, accepted_at=relay.now())
                    db.execute("UPDATE pair_meta SET value=? WHERE key=?", (json.dumps(details), 'delivery:' + row['id']))
        except codex.RetryLater as exc:
            status, error = 'deferred', str(exc)
        except codex.Unavailable as exc:
            status, error = 'blocked', str(exc)
        except codex.Unconfirmed as exc:
            error = str(exc)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            error = "Delivery could not be confirmed (" + type(exc).__name__ + "). No automatic retry."
        with relay.connect_db(self.state) as db:
            db.execute(
                "UPDATE pair_messages SET status=?,error=?,wire_id=? WHERE id=? AND status=?",
                (status, error, wire_id, row["id"], "sending"),
            )


def cli(state, args):
    try:
        connection = Connection(state)
    except relay.StateAccessError as exc:
        # Construction cannot insert an outgoing pair message.
        raise RuntimeError(f"No message was queued. {exc}") from exc
    if args.command == "pair-send":
        if args.file and args.message is not None:
            raise ValueError("Use message text or --file, not both")
        body = args.file.read_text(encoding="utf-8") if args.file else args.message
        if body is None:
            if sys.stdin.isatty():
                raise ValueError("Supply message text, --file, or piped stdin")
            body = sys.stdin.read()
        try:
            return connection.send(args.from_session, args.to_session, body, args.reply_to)
        except relay.StateAccessError as exc:
            # A transaction/commit failure must not be turned into permission to
            # resend: the client may not know whether the write was durable.
            raise RuntimeError(
                f"Queueing could not be confirmed. Do not retry automatically; inspect the "
                f"connection's saved messages first. {exc}"
            ) from exc
    return {"connection": connection.snapshot(), "messages": connection.read(args.after)}
