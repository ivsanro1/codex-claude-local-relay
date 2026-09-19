import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from fake_codex import native_delivery

from codex_claude_local_relay import codex, mcp, relay, runtime
from codex_claude_local_relay.connections import Connection

A = "codex:10000000-0000-4000-8000-000000000001"
B = "codex:10000000-0000-4000-8000-000000000002"
C = "codex:10000000-0000-4000-8000-000000000003"
CLAUDE = "claude:20000000-0000-4000-8000-000000000001"


def rpc(server, method, params=None, identifier=1):
    return server.handle({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}})


def call(server, name, arguments=None, meta=None):
    params = {"name": name, "arguments": arguments or {}}
    if meta is not None:
        params["_meta"] = meta
    result = rpc(server, "tools/call", params)["result"]
    return result["isError"], result["content"][0]["text"]


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-mcp-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "connections"
        self.root.mkdir(mode=0o700)
        self.claude_dir = Path(self.temp.name) / ".claude"
        (self.claude_dir / "sessions").mkdir(parents=True)
        self.environment = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude_dir)}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def register_claude(self, pid=None, session=CLAUDE):
        pid = pid or os.getpid()
        row = {
            "pid": pid,
            "sessionId": session.split(":", 1)[1],
            "cwd": self.temp.name,
            "procStart": relay.proc_start(pid),
            "version": "2.1.261",
            "peerProtocol": 1,
            "messagingSocketPath": str(Path(self.temp.name) / "claude.sock"),
            "name": "fixture",
        }
        (self.claude_dir / "sessions" / f"{pid}.json").write_text(json.dumps(row))
        return row

    def pair(self, first, second, alias="pair-1", messaging="mcp", name=None):
        participants = [
            {"id": first, "cwd": self.temp.name, "title": "First agent"},
            {"id": second, "cwd": self.temp.name, "title": "Second agent"},
        ]
        link = Connection.create(self.root / (name or alias), participants, alias=alias, messaging=messaging)
        self.addCleanup(self.close, link.state)  # Stop any Claude leg daemons the fixture started.
        return link

    @staticmethod
    def close(state):
        try:
            Connection(state).disconnect()
        except (OSError, ValueError):
            pass

    # Connection changes -------------------------------------------------------

    def test_alias_and_messaging_are_saved_and_validated(self):
        link = self.pair(A, B)
        saved = json.loads((self.root / "pair-1" / "connection.json").read_text())
        self.assertEqual((saved["alias"], saved["messaging"], saved["version"]), ("pair-1", "mcp", 1))
        self.assertEqual(link.snapshot()["alias"], "pair-1")
        for bad in ("", "-x", "a" * 41, "with space", 7):
            with self.assertRaises(ValueError):
                Connection.create(self.root / "bad", [{"id": A, "cwd": "."}, {"id": B, "cwd": "."}], alias=bad)
        with self.assertRaises(ValueError):
            Connection.create(self.root / "bad", [{"id": A, "cwd": "."}, {"id": B, "cwd": "."}], messaging="carrier-pigeon")
        legacy = Connection.create(self.root / "legacy", [{"id": A, "cwd": "."}, {"id": B, "cwd": "."}])
        self.assertNotIn("alias", legacy.config)
        self.assertNotIn("messaging", legacy.config)

    def test_mcp_notice_and_envelope_have_no_footer_and_keep_receipt_shape(self):
        link = self.pair(A, B)
        notices = [row["body"] for row in link.read() if row["kind"] == "connection"]
        for body in notices:
            self.assertIn('send(to="pair-1", text=...)', body)
            self.assertNotIn(A, body)
            self.assertNotIn(B, body)
        link.send(A, B, "Hello peer", verify_context=False)
        delivered = []
        with native_delivery() as deliver:
            deliver.side_effect = lambda native, message_id, text: delivered.append((native, message_id, text)) or {
                "turn_id": "fixture-turn",
                "method": "turn/steer",
            }
            for _ in range(3):
                link.tick(lambda key: {"codex_socket": "/fixture.sock"}, "/synthetic/codex")
        message = next(text for _, message_id, text in delivered if "Hello peer" in text)
        first, body = message.split("\n", 1)
        row = next(r for r in link.read() if r["body"] == "Hello peer")
        self.assertEqual(first, f"[Local relay pair-1; message {row['id']}; from First agent]")
        self.assertEqual(body, "Hello peer")
        self.assertEqual(link.instructions(B), "")
        legacy = Connection.create(self.root / "legacy", [{"id": A, "cwd": "."}, {"id": B, "cwd": "."}])
        self.assertIn("pair-send", legacy.instructions(A))
        self.assertIn("PROJECT_RELAY", legacy.instructions(B.replace("codex", "claude")) if False else "PROJECT_RELAY")

    def test_bare_wire_delivers_body_only_and_accepts_unthreaded_reply(self):
        listener = socket.socket(socket.AF_UNIX)
        path = Path(self.temp.name) / "claude.sock"
        listener.bind(str(path))
        listener.listen(1)
        self.addCleanup(listener.close)
        target = self.register_claude()
        target["messagingSocketPath"] = str(path)
        (self.claude_dir / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(target))
        digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        key = self.claude_dir / "sessions" / f"{os.getpid()}.{digest}.key"
        key.write_text(json.dumps({"peerToken": "ab" * 16, "procStart": target["procStart"]}))
        key.chmod(0o600)
        received = []

        def accept():
            connection, _ = listener.accept()
            with connection:
                data = b""
                while chunk := connection.recv(65536):
                    data += chunk
                received.append(data)

        thread = threading.Thread(target=accept)
        thread.start()
        row = {"thread": "pair-1", "id": "msg-1", "reply_to": None, "body": "Bare body"}
        relay.send_wire(target, "uds:/tmp/reply.sock", row, "switchboard-pair-1", bare=True)
        thread.join(5)
        auth, frame = received[0].decode().strip().split("\n")
        content = json.loads(frame)["message"]["content"]
        inner = content.split("\n", 1)[1].rsplit("\n", 1)[0]
        self.assertEqual(inner, "Bare body")
        self.assertNotIn("PROJECT_RELAY", content)
        self.assertNotIn("Relay routing", content)
        self.assertIn('from-name="switchboard-pair-1"', content)
        # Legacy wire keeps its header and footer.
        thread = threading.Thread(target=accept)
        thread.start()
        relay.send_wire(target, "uds:/tmp/reply.sock", row, "switchboard-legacy")
        thread.join(5)
        content = json.loads(received[1].decode().strip().split("\n")[1])["message"]["content"]
        self.assertIn('PROJECT_RELAY {"thread": "pair-1", "message_id": "msg-1"}', content)
        self.assertIn("Relay routing", content)

    # Binding ------------------------------------------------------------------

    def test_claude_binding_uses_parent_registry_and_environment_cross_check(self):
        self.register_claude()
        server = mcp.Server(self.root, "claude", pid=os.getpid())
        self.assertEqual(server.bind(), CLAUDE)
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "30000000-0000-4000-8000-000000000009"}):
            with self.assertRaises(mcp.Unbound):
                mcp.Server(self.root, "claude", pid=os.getpid()).bind()
        # An in-app resume that changes the registry row is followed on the next call.
        server.register()
        self.register_claude(session="claude:20000000-0000-4000-8000-000000000002")
        self.assertEqual(server.bind(), "claude:20000000-0000-4000-8000-000000000002")
        self.assertEqual(mcp.registrations(self.root)[0]["session"], "claude:20000000-0000-4000-8000-000000000002")
        server.unregister()
        (self.claude_dir / "sessions" / f"{os.getpid()}.json").unlink()
        with self.assertRaises(mcp.Unbound):
            mcp.Server(self.root, "claude", pid=os.getpid()).bind()
        loose = Path(self.temp.name) / "loose"
        loose.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            mcp.Server(loose, "claude")

    def test_codex_binding_requires_thread_meta_loaded_on_owning_runtime(self):
        server = mcp.Server(self.root, "codex", runtime_socket=Path(self.temp.name) / "runtime.sock", pid=os.getpid())
        with patch("codex_claude_local_relay.mcp.ancestors", return_value=[os.getpid()]):
            for meta in (None, {}, {"threadId": 5}, {"threadId": "not-a-uuid"}, {"threadId": A[6:].upper()}):
                with self.assertRaises(mcp.Unbound):
                    server.bind(meta)
            with patch("codex_claude_local_relay.mcp.codex.Client") as factory:
                client = factory.return_value.__enter__.return_value
                self.assertEqual(server.bind({"threadId": A[6:]}), A)
                self.assertEqual(server.bind({"threadId": B[6:]}), B)
                client.thread.assert_any_call(A[6:])
                client.thread.assert_any_call(B[6:])
                client.thread.side_effect = codex.Unavailable("not loaded")
                with self.assertRaises(mcp.Unbound):
                    server.bind({"threadId": C[6:]})
            # A discovered parent runtime that differs from the configured socket is refused.
            with patch("codex_claude_local_relay.mcp.codex.socket_from_process", return_value="/other.sock"):
                with self.assertRaises(mcp.Unbound):
                    server.bind({"threadId": A[6:]})

    # Tools --------------------------------------------------------------------

    def test_tools_route_by_alias_and_fail_closed_without_identity(self):
        self.register_claude()
        self.pair(CLAUDE, A)
        server = mcp.Server(self.root, "claude", pid=os.getpid())
        init = rpc(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertEqual(rpc(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"], mcp.PROTOCOL_VERSIONS[0])
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(sorted(t["name"] for t in rpc(server, "tools/list")["result"]["tools"]), ["peers", "send", "status"])
        error, text = call(server, "peers")
        self.assertFalse(error)
        peers = json.loads(text)["connections"]
        self.assertEqual([(p["alias"], p["peer"]["provider"], p["peer"]["title"]) for p in peers], [("pair-1", "codex", "Second agent")])
        error, text = call(server, "send", {"text": "Question for the peer"})
        self.assertFalse(error, text)
        sent = json.loads(text)
        rows = Connection(self.root / "pair-1").read()
        self.assertEqual([(r["sender"], r["recipient"], r["status"]) for r in rows if r["id"] == sent["id"]], [(CLAUDE, A, "queued")])
        error, text = call(server, "status")
        self.assertFalse(error)
        self.assertEqual(json.loads(text)["messages"][-1]["direction"], "sent")
        # A second connection makes `to` mandatory; foreign aliases are refused.
        self.pair(CLAUDE, B, alias="pair-2")
        error, text = call(server, "send", {"text": "Ambiguous"})
        self.assertTrue(error)
        self.assertIn("pair-1", text)
        error, text = call(server, "send", {"text": "Wrong", "to": "pair-9"})
        self.assertTrue(error)
        error, text = call(server, "send", {"text": "Second", "to": "pair-2"})
        self.assertFalse(error, text)
        self.assertEqual(Connection(self.root / "pair-2").read()[-1]["recipient"], B)
        # Disconnected and foreign connections are invisible.
        Connection(self.root / "pair-2").disconnect()
        self.pair(A, B, alias="others")
        self.assertEqual([p["alias"] for p in json.loads(call(server, "peers")[1])["connections"]], ["pair-1"])
        error, text = call(server, "send", {"text": "x", "to": "others"})
        self.assertTrue(error)
        # Unbound sessions never queue anything.
        (self.claude_dir / "sessions" / f"{os.getpid()}.json").unlink()
        unbound = mcp.Server(self.root, "claude", pid=os.getpid())
        error, text = call(unbound, "send", {"text": "x", "to": "pair-1"})
        self.assertTrue(error)
        self.assertIn("nothing was sent", text)
        self.assertEqual(len([r for r in Connection(self.root / "pair-1").read() if r["body"] == "x"]), 0)
        self.assertEqual(rpc(server, "nope")["error"]["code"], -32601)
        self.assertEqual(rpc(server, "tools/call", {"name": "missing"})["error"]["code"], -32602)

    def test_codex_tool_calls_bind_each_call_to_its_thread(self):
        self.pair(A, B)
        self.pair(C, B, alias="pair-3")
        server = mcp.Server(self.root, "codex", runtime_socket="/fixture/runtime.sock", pid=os.getpid())
        with patch("codex_claude_local_relay.mcp.ancestors", return_value=[os.getpid()]), patch(
            "codex_claude_local_relay.mcp.codex.Client"
        ):
            error, text = call(server, "send", {"text": "From A"}, meta={"threadId": A[6:]})
            self.assertFalse(error, text)
            error, text = call(server, "send", {"text": "From C"}, meta={"threadId": C[6:]})
            self.assertFalse(error, text)
            error, text = call(server, "send", {"text": "No identity"})
            self.assertTrue(error)
            self.assertIn("nothing was sent", text)
        self.assertEqual([r["sender"] for r in Connection(self.root / "pair-1").read() if r["kind"] == "message"], [A])
        self.assertEqual([r["sender"] for r in Connection(self.root / "pair-3").read() if r["kind"] == "message"], [C])

    # Registration -------------------------------------------------------------

    def test_registration_and_bridge_status_only_remove_confirmed_dead_files(self):
        self.register_claude()
        server = mcp.Server(self.root, "claude", pid=os.getpid())
        row = server.register()
        self.assertEqual((row["provider"], row["session"]), ("claude", CLAUDE))
        self.assertTrue(mcp.bridge_status(self.root, CLAUDE)["ready"])
        self.assertFalse(mcp.bridge_status(self.root, "claude:20000000-0000-4000-8000-000000000002")["ready"])
        self.assertFalse(mcp.bridge_status(self.root, "bogus")["ready"])
        directory = self.root / "mcp"
        gone = subprocess.run([sys.executable, "-c", "pass"])  # A pid that has exited.
        (directory / "dead.json").write_text(json.dumps({"version": 1, "provider": "codex", "pid": 2**22 - 7, "proc_start": "1", "runtime_socket": "/r.sock"}))
        (directory / "malformed.json").write_text("{not json")
        (directory / "unreadable.json").write_text(json.dumps({"version": 1, "provider": "codex", "pid": "x", "proc_start": "1"}))
        codex_row = {**row, "provider": "codex", "session": None, "runtime_socket": "/fixture/runtime.sock", "pid": os.getpid()}
        (directory / "codex.json").write_text(json.dumps(codex_row))
        self.assertTrue(mcp.bridge_status(self.root, A, {"codex_socket": "/fixture/runtime.sock"})["ready"])
        self.assertFalse(mcp.bridge_status(self.root, A, {"codex_socket": "/other.sock"})["ready"])
        self.assertFalse(mcp.bridge_status(self.root, A)["ready"])
        self.assertFalse((directory / "dead.json").exists())
        self.assertTrue((directory / "malformed.json").exists())
        self.assertTrue((directory / "unreadable.json").exists())
        server.unregister()
        self.assertFalse(mcp.bridge_status(self.root, CLAUDE)["ready"])
        self.assertIsNotNone(gone)

    def test_runtime_overrides_target_the_app_server_without_permission_keys(self):
        overrides = runtime.mcp_overrides(self.root)
        self.assertEqual(overrides[0::2], ["-c", "-c"])
        command = json.loads(overrides[1].split("=", 1)[1])
        args = json.loads(overrides[3].split("=", 1)[1])
        self.assertEqual(command, sys.executable)
        self.assertEqual(args[:2], ["-m", "codex_claude_local_relay.mcp"])
        self.assertIn(str(self.root), args)
        self.assertIn("codex", args)
        for key in ("approval_policy", "sandbox", "permission", "add-dir", "writable"):
            self.assertNotIn(key, " ".join(overrides))
        with self.assertRaises(SystemExit):
            runtime.mcp_overrides(self.root / "missing")

    def test_stdio_process_binds_to_its_parent_and_registers_while_running(self):
        self.register_claude()
        self.pair(CLAUDE, A)
        child = subprocess.Popen(
            [sys.executable, "-m", "codex_claude_local_relay.mcp", "--connections-root", str(self.root), "--provider", "claude"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")},
        )
        self.addCleanup(child.kill)

        def ask(message):
            child.stdin.write((json.dumps(message) + "\n").encode())
            child.stdin.flush()
            return json.loads(child.stdout.readline())

        init = ask({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "switchboard")
        registered = mcp.registrations(self.root)
        self.assertEqual([(r["pid"], r["session"], r["parent_pid"]) for r in registered], [(child.pid, CLAUDE, os.getpid())])
        self.assertTrue(mcp.bridge_status(self.root, CLAUDE)["ready"])
        peers = ask({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "peers", "arguments": {}}})
        self.assertEqual(json.loads(peers["result"]["content"][0]["text"])["connections"][0]["alias"], "pair-1")
        sent = ask({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "send", "arguments": {"text": "via stdio"}}})
        self.assertFalse(sent["result"]["isError"], sent)
        self.assertEqual(Connection(self.root / "pair-1").read()[-1]["body"], "via stdio")
        child.stdin.close()
        child.wait(10)
        self.assertEqual(child.returncode, 0, child.stderr.read().decode())
        self.assertEqual(mcp.registrations(self.root), [])

    def test_serve_reports_parse_errors_and_ignores_notifications(self):
        self.register_claude()
        server = mcp.Server(self.root, "claude", pid=os.getpid())
        out = io.BytesIO()
        server.serve(io.BytesIO(b'not json\n\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n'), out)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([line.get("error", {}).get("code", "ok") for line in lines], [-32700, "ok"])
        self.assertEqual(lines[1]["id"], 9)


if __name__ == "__main__":
    unittest.main()
