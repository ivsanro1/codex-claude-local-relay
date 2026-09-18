"""Real installed Codex + deterministic local model; no account or model calls.

CODEX_RELAY_RUNTIME_TEST=1 makes missing/incompatible Codex a failure. The fake
HTTP model controls sampling boundaries, while the actual Codex binary owns
threads, long turns, steering, items, and queue storage. Run this in release CI
and against newly installed Codex versions, not just protocol-shaped mocks.
"""

import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from codex_claude_local_relay import relay
from codex_claude_local_relay.codex import Client
from codex_claude_local_relay.connections import Connection


def stop_fixture_group(process):
    """Reap this fixture's runtime and background helpers before deleting its home."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


class FixtureOwner(Client):
    def __init__(self, *args, **kwargs):
        self.requests = []
        super().__init__(*args, **kwargs)

    def notification(self, value):
        if "method" in value and "id" in value:
            self.requests.append(value)


@unittest.skipUnless(
    os.environ.get("CODEX_RELAY_RUNTIME_TEST") == "1",
    "Opt-in installed Codex runtime test",
)
class NativeRuntimeTests(unittest.TestCase):
    def test_native_tui_uses_launcher_runtime_and_exits_cleanly(self):
        binary = shutil.which("codex")
        self.assertIsNotNone(binary, "Codex is required for native relay certification")
        with tempfile.TemporaryDirectory(prefix="relay-tui-") as temporary:
            root = Path(temporary)
            native_home = root / "home"
            native_home.mkdir()
            (native_home / "config.toml").write_text(
                'model="fixture-model"\nmodel_provider="fixture"\n'
                '[model_providers.fixture]\nname="Fixture"\nwire_api="responses"\n'
                'base_url="http://127.0.0.1:1/v1"\nrequires_openai_auth=false\n'
                f'[projects.{json.dumps(str(root))}]\ntrust_level="trusted"\n'
            )
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("CODEX_", "OPENAI_"))
            }
            env.update(CODEX_HOME=str(native_home), TERM="xterm-256color")
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            sock = root / "runtime.sock"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "codex_claude_local_relay.runtime",
                    "--codex",
                    binary,
                    "--socket",
                    str(sock),
                    "--",
                    "--no-alt-screen",
                ],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=root,
                env=env,
                start_new_session=True,
            )
            os.close(slave)
            output = b""
            try:
                deadline = time.monotonic() + 25
                while (
                    b"fixture-model default" not in output
                    and time.monotonic() < deadline
                ):
                    self.assertIsNone(
                        process.poll(), output.decode(errors="replace")[-2000:]
                    )
                    if select.select([master], [], [], 0.1)[0]:
                        chunk = os.read(master, 65536)
                        output += chunk
                        if b"\x1b[6n" in chunk:
                            os.write(master, b"\x1b[1;1R")
                self.assertIn(b"fixture-model default", output)
                with Client(binary, sock) as client:
                    loaded = client.call("thread/loaded/list", {})["data"]
                    # The model label may render before thread/start completes.
                    # Synchronize with native readiness, not terminal painting.
                    deadline = time.monotonic() + 10
                    while not loaded and time.monotonic() < deadline:
                        self.assertIsNone(process.poll())
                        time.sleep(0.05)
                        loaded = client.call("thread/loaded/list", {})["data"]
                    self.assertEqual(len(loaded), 1)
                    self.assertEqual(client.thread(loaded[0])["status"]["type"], "idle")
                os.write(master, b"\x04")
                deadline = time.monotonic() + 8
                while process.poll() is None and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            os.read(master, 65536)
                        except OSError:
                            break
                self.assertEqual(process.wait(timeout=3), 0)
                self.assertFalse(sock.exists())
            finally:
                stop_fixture_group(process)
                os.close(master)

    def test_active_delivery_reaches_model_before_original_turn_completes_and_idle_wakes(
        self,
    ):
        binary = shutil.which("codex")
        self.assertIsNotNone(binary, "Codex is required for native relay certification")
        print(
            "Native relay regression:",
            subprocess.check_output([binary, "--version"], text=True).strip(),
        )
        requests = []
        first_request = threading.Event()
        release_response = threading.Event()
        second_request = threading.Event()

        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append(body.decode())
                number = len(requests)
                if number == 1:
                    first_request.set()
                    release_response.wait(15)
                    output = {
                        "id": "fc_one",
                        "type": "function_call",
                        "call_id": "call_one",
                        "name": "fixture_checkpoint",
                        "arguments": "{}",
                    }
                else:
                    second_request.set()
                    output = {
                        "id": "msg_" + str(number),
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "Fixture complete."}
                        ],
                    }
                events = [
                    {
                        "type": "response.created",
                        "response": {"id": "resp_" + str(number)},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": output,
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": output,
                    },
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_" + str(number),
                            "output": [output],
                            "usage": {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "total_tokens": 15,
                            },
                        },
                    },
                ]
                data = "".join(
                    "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n"
                    for e in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        http = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.addCleanup(release_response.set)
        with tempfile.TemporaryDirectory(prefix="relay-native-") as temporary:
            root = Path(temporary)
            native_home = root / "codex"
            native_home.mkdir()
            # This is the provider's actual isolated test home, not the user's.
            (native_home / "config.toml").write_text(
                'model = "fixture-model"\nmodel_provider = "fixture"\n'
                '[model_providers.fixture]\nname = "Fixture"\nwire_api = "responses"\n'
                f'base_url = "http://127.0.0.1:{http.server_port}/v1"\n'
                "requires_openai_auth = false\n"
            )
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("CODEX_", "OPENAI_"))
            }
            env["CODEX_HOME"] = str(native_home)
            sock = root / "runtime.sock"
            log = (root / "runtime.log").open("wb")
            server = subprocess.Popen(
                [binary, "app-server", "--listen", "unix://" + str(sock)],
                env=env,
                cwd=root,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 10
                while (
                    not sock.exists()
                    and server.poll() is None
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.05)
                self.assertTrue(
                    sock.exists(), (root / "runtime.log").read_text()[-2000:]
                )
                with (
                    patch.dict(os.environ, env, clear=True),
                    FixtureOwner(binary, sock) as owner,
                ):
                    created = owner.call(
                        "thread/start",
                        {
                            "cwd": str(root),
                            "dynamicTools": [
                                {
                                    "type": "function",
                                    "name": "fixture_checkpoint",
                                    "description": "Fixture boundary",
                                    "inputSchema": {"type": "object"},
                                }
                            ],
                        },
                    )
                    native = created["thread"]["id"]
                    started = owner.call(
                        "turn/start",
                        {
                            "threadId": native,
                            "input": [{"type": "text", "text": "Fixture first turn"}],
                        },
                    )
                    original_turn = started["turn"]["id"]
                    self.assertTrue(
                        first_request.wait(10),
                        (root / "runtime.log").read_text()[-2500:],
                    )
                    recipient = "codex:" + native
                    sender = "codex:" + str(uuid.uuid4())
                    pair = Connection.create(
                        root / "pair",
                        [
                            {"id": recipient, "cwd": str(root)},
                            {"id": sender, "cwd": str(root)},
                        ],
                    )
                    with relay.connect_db(pair.state) as db:
                        db.execute(
                            "DELETE FROM pair_messages"
                        )  # Only this disposable fixture's notices.
                    message_id = pair.send(
                        sender, recipient, "ACTIVE_PEER_MARKER", verify_context=False
                    )["id"]
                    route = lambda _: {"codex_socket": str(sock)}
                    pair.tick(route, binary)
                    self.assertEqual(pair.read()[0]["status"], "accepted_native")
                    self.assertEqual(pair.read()[0]["wire_id"], original_turn)
                    with Client(binary, sock) as adapter:
                        self.assertFalse(second_request.is_set())
                        self.assertEqual(
                            adapter.call("thread/queue/list", {"threadId": native})[
                                "data"
                            ],
                            [],
                        )
                    release_response.set()
                    # The native dynamic tool is owned by this fixture client.
                    # Read its server request, reply, and allow the next model step.
                    deadline = time.monotonic() + 10
                    answered = False
                    while time.monotonic() < deadline and not answered:
                        owner.call("thread/read", {"threadId": native})
                        # Patched below by the native-client request capture hook.
                        for request in getattr(owner, "requests", []):
                            if request["method"] == "item/tool/call":
                                tool_message_id = str(uuid.uuid4())
                                tool_text = (
                                    "[Local relay fixture; message "
                                    + tool_message_id
                                    + "; from fixture]\nTOOL_PEER_MARKER"
                                )
                                with Client(binary, sock) as adapter:
                                    during_tool = adapter.deliver(
                                        native, tool_message_id, tool_text
                                    )
                                    self.assertEqual(
                                        during_tool["turn_id"], original_turn
                                    )
                                owner._write(
                                    {
                                        "id": request["id"],
                                        "result": {
                                            "contentItems": [
                                                {
                                                    "type": "inputText",
                                                    "text": "Checkpoint complete",
                                                }
                                            ],
                                            "success": True,
                                        },
                                    }
                                )
                                answered = True
                                break
                        time.sleep(0.05)
                    self.assertTrue(
                        answered, (root / "runtime.log").read_text()[-2500:]
                    )
                    self.assertTrue(second_request.wait(10))
                    self.assertIn("ACTIVE_PEER_MARKER", requests[1])
                    self.assertIn("TOOL_PEER_MARKER", requests[1])
                    with Client(binary, sock) as adapter:
                        deadline = time.monotonic() + 10
                        while (
                            adapter.thread(native)["status"]["type"] != "idle"
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.05)
                        self.assertIn(
                            message_id,
                            adapter.observed(native, {message_id}, original_turn),
                        )
                        self.assertIn(
                            tool_message_id,
                            adapter.observed(native, {tool_message_id}, original_turn),
                        )
                        pair = Connection(
                            pair.state
                        )  # A restarted controller observes, never resends.
                        pair.recover()
                        pair.tick(route, binary)
                        self.assertEqual(pair.read()[0]["status"], "input_observed")
                        idle = adapter.deliver(
                            native, str(uuid.uuid4()), "IDLE_PEER_MARKER"
                        )
                        self.assertEqual(idle["method"], "turn/start")
                        self.assertNotEqual(idle["turn_id"], original_turn)
                        deadline = time.monotonic() + 10
                        while len(requests) < 3 and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertGreaterEqual(len(requests), 3)
                        self.assertIn("IDLE_PEER_MARKER", requests[2])
            finally:
                release_response.set()
                if sys.exc_info()[0]:
                    print((root / "runtime.log").read_text()[-4000:])
                stop_fixture_group(server)
                server.stdin.close()
                log.close()


if __name__ == "__main__":
    unittest.main()
