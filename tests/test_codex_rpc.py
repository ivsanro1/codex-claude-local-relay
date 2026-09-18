import json
import tempfile
import threading
import unittest
from pathlib import Path

from websockets.sync.server import unix_serve

from codex_claude_local_relay.codex import Client, Rejected, Unconfirmed


class RpcTests(unittest.TestCase):
    def run_rpc(self, outcome):
        temporary = tempfile.TemporaryDirectory(prefix="relay-rpc-")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "socket"
        calls = []

        def handle(connection):
            for raw in connection:
                request = json.loads(raw)
                calls.append(request["method"])
                if "id" not in request:
                    continue
                if request["method"] == "initialize":
                    connection.send(json.dumps({"id": request["id"], "result": {}}))
                elif outcome == "disconnect":
                    connection.close()
                else:
                    connection.send(json.dumps({"id": request["id"], **outcome}))

        server = unix_serve(handle, str(path), compression=None)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.shutdown)
        return Client("fixture", path, timeout=0.5), calls

    def test_lost_response_is_ambiguous_and_only_sent_once(self):
        client, calls = self.run_rpc("disconnect")
        with client, self.assertRaises(Unconfirmed):
            client.call("turn/steer", {}, mutation=True)
        self.assertEqual(calls.count("turn/steer"), 1)

    def test_validation_rejection_is_distinct_from_internal_failure(self):
        for code, expected in [(-32602, Rejected), (-32603, Unconfirmed)]:
            with self.subTest(code=code):
                client, calls = self.run_rpc(
                    {"error": {"code": code, "message": "fixture error"}}
                )
                with client, self.assertRaises(expected):
                    client.call("turn/steer", {}, mutation=True)
                self.assertEqual(calls.count("turn/steer"), 1)

    def test_malformed_success_after_mutation_is_unconfirmed(self):
        client, _ = self.run_rpc({"result": []})
        with client, self.assertRaises(Unconfirmed):
            client.call("turn/start", {}, mutation=True)


if __name__ == "__main__":
    unittest.main()
