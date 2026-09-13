import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_claude_local_relay import relay
from codex_claude_local_relay.connections import Connection, identity

A = "codex:10000000-0000-4000-8000-000000000001"
B = "codex:10000000-0000-4000-8000-000000000002"
C = "codex:10000000-0000-4000-8000-000000000003"


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relay-pair-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.participants = [{"id": A, "cwd": str(self.root)}, {"id": B, "cwd": str(self.root)}]
        self.link = Connection.create(self.root / "pair", self.participants)

    def test_full_identity_no_names_prefixes_or_same_session(self):
        for bad in (
            "codex:reviewer",
            "claude:10000000",
            "10000000-0000-4000-8000-000000000001",
            "other:" + A[6:],
        ):
            with self.assertRaises(ValueError):
                identity(bad)
        with self.assertRaises(ValueError):
            Connection.create(self.root / "same", [self.participants[0]] * 2)
        with self.assertRaises(ValueError):
            Connection.create(self.root / "pair", [self.participants[0], {"id": C, "cwd": str(self.root)}])
        self.assertEqual(Connection(self.root / "pair").ids, [A, B])

    def test_wrong_sender_peer_context_and_reply_are_refused(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": A[6:]}):
            for sender, recipient in ((A, C), (C, B), (B, A)):
                with self.assertRaises(ValueError):
                    self.link.send(sender, recipient, "Do not misroute")
            with self.assertRaises(ValueError):
                self.link.send(A, B, "Wrong reply", "foreign-message")
            result = self.link.send(A, B, "Exact route")
            self.assertEqual(result["recipient"], B)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
            with self.assertRaises(ValueError):
                self.link.send(A, B, "No context")
        self.assertEqual(len(self.link.read()), 3)  # Two notices, one valid message.

    def test_both_notices_and_messages_use_uuid_queue_without_model_or_resume(self):
        checked = []
        with patch(
            "codex_claude_local_relay.connections.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            self.link.tick(checked.append, "/synthetic/codex")
            self.link.tick(checked.append, "/synthetic/codex")
            self.link.tick(checked.append, "/synthetic/codex")
        self.assertEqual(checked, [A, B])
        self.assertEqual(run.call_count, 2)
        for call, expected in zip(run.call_args_list, (A, B)):
            argv = call.args[0]
            self.assertEqual(argv[:4], ["/synthetic/codex", "queue", "--thread", expected[6:]])
            self.assertNotIn("--model", argv)
            self.assertNotIn("resume", argv)
        self.assertTrue(all(r["status"] == "queued_native" for r in self.link.read()))

    def test_disconnected_pair_rejects_send_and_cancels_pending(self):
        self.link.disconnect()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": A[6:]}):
            with self.assertRaises(ValueError):
                self.link.send(A, B, "Disconnected")
        self.assertFalse(self.link.enabled())
        self.assertTrue(all(r["status"] == "cancelled" for r in self.link.read()))

    def test_missing_recipient_is_blocked_and_ambiguous_send_is_never_retried(self):
        with patch("codex_claude_local_relay.connections.subprocess.run") as run:
            self.link.tick(
                lambda _: (_ for _ in ()).throw(ValueError("Recipient not live")), "/synthetic/codex"
            )
            run.assert_not_called()
            run.side_effect = subprocess.TimeoutExpired("synthetic", 20)
            self.link.tick(lambda _: None, "/synthetic/codex")
            self.link.tick(lambda _: None, "/synthetic/codex")
            self.assertEqual(run.call_count, 1)
        self.assertEqual([r["status"] for r in self.link.read()], ["blocked", "unknown"])
        with relay.connect_db(self.link.state) as db:
            db.execute("UPDATE pair_messages SET status='sending' WHERE status='unknown'")
        self.link.recover()
        self.assertEqual(self.link.read()[-1]["status"], "unknown")

    def test_pair_config_contains_ids_not_credentials(self):
        config = json.loads((self.link.state / "connection.json").read_text())
        self.assertEqual(config["participants"], self.participants)
        self.assertEqual(self.link.state.stat().st_mode & 0o777, 0o700)

    def test_controller_prepares_messages_in_delivery_selection_transaction(self):
        class Gated(Connection):
            def prepare(self, db):
                self_transaction = db.in_transaction
                if not self_transaction:
                    raise AssertionError("Preparation must be transactional")
                db.execute("UPDATE pair_messages SET status='waiting_handshake' WHERE kind='message'")

        link = Gated(self.link.state)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": A[6:]}):
            # The ordinary CLI class writes to the same queue as the controller.
            pending = self.link.send(A, B, "Wait for the application handshake")
        with patch(
            "codex_claude_local_relay.connections.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            for _ in range(4):
                link.tick(lambda _: None, "/synthetic/codex")
            self.assertEqual(run.call_count, 2)  # Only the controller's notices.
        self.assertEqual(next(r for r in link.read() if r["id"] == pending["id"])["status"], "waiting_handshake")


if __name__ == "__main__":
    unittest.main()
