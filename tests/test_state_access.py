import argparse
import errno
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_claude_local_relay import relay
from codex_claude_local_relay.connections import Connection, cli

A = "codex:10000000-0000-4000-8000-000000000001"
B = "codex:10000000-0000-4000-8000-000000000002"


class StateAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="relay access ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pair = Connection.create(
            self.root / "pair", [{"id": A, "cwd": str(self.root)}, {"id": B, "cwd": str(self.root)}]
        )
        self.args = argparse.Namespace(
            command="pair-send", from_session=A, to_session=B,
            message="Synthetic test message", file=None, reply_to=None,
        )
        self.environment = patch.dict(os.environ, {"CODEX_THREAD_ID": A[6:]})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_sqlite_access_failure_before_send_is_actionable_and_does_not_queue(self):
        for message in ("unable to open database file", "attempt to write a readonly database"):
            with self.subTest(message=message):
                with patch.object(relay.sqlite3, "connect", side_effect=sqlite3.OperationalError(message)):
                    with self.assertRaisesRegex(RuntimeError, "No message was queued") as caught:
                        cli(self.pair.state, self.args)
                text = str(caught.exception)
                self.assertIn(str(self.pair.state), text)
                self.assertIn("journal/WAL", text)
                self.assertIn("approved execution", text)
                self.assertIn("--add-dir '", text)  # A path containing spaces stays one argument.
                self.assertEqual(len(self.pair.read()), 2)

    def test_filesystem_denial_during_initialization_is_also_actionable(self):
        for number in (errno.EROFS, errno.EACCES, errno.EPERM):
            with self.subTest(errno=number):
                with patch.object(Path, "mkdir", side_effect=OSError(number, "fixture denied")):
                    with self.assertRaisesRegex(RuntimeError, "No message was queued"):
                        cli(self.pair.state, self.args)
                self.assertEqual(len(self.pair.read()), 2)

    @unittest.skipIf(os.geteuid() == 0, "root ignores ordinary file write permissions")
    def test_real_readonly_database_does_not_queue_and_can_send_after_access_is_restored(self):
        database = self.pair.state / "mail.sqlite"
        database.chmod(0o400)
        try:
            with self.assertRaisesRegex(RuntimeError, "No message was queued"):
                cli(self.pair.state, self.args)
        finally:
            # SQLite can create WAL/SHM files inheriting the readonly DB mode.
            # Restoring access means restoring all mailbox files, not just DB.
            for path in self.pair.state.glob("mail.sqlite*"):
                path.chmod(0o600)
        result = cli(self.pair.state, self.args)
        self.assertEqual(result["status"], "queued")
        rows = self.pair.read()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["body"], self.args.message)

    def test_pair_commands_do_not_create_missing_or_partial_state(self):
        missing = self.root / "not created"
        with self.assertRaisesRegex(ValueError, "Have the controller finish creating"):
            cli(missing, self.args)
        self.assertFalse(missing.exists())
        partial = self.root / "partial"
        partial.mkdir(mode=0o700)
        (partial / "connection.json").write_bytes((self.pair.state / "connection.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "missing mail.sqlite"):
            cli(partial, self.args)
        self.assertFalse((partial / "mail.sqlite").exists())

    def test_controller_can_create_future_database_under_existing_parent(self):
        parent = self.root / "future connections"
        parent.mkdir(mode=0o700)
        self.assertEqual(list(parent.iterdir()), [])
        pair = Connection.create(parent / "later", self.pair.config["participants"])
        self.assertEqual(cli(pair.state, self.args)["status"], "queued")
        self.assertEqual(len(pair.read()), 3)

    def test_send_phase_failure_never_claims_no_message_was_queued(self):
        error = relay.StateAccessError(self.pair.state, sqlite3.OperationalError("disk access failed"))
        with patch.object(Connection, "send", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "Queueing could not be confirmed") as caught:
                cli(self.pair.state, self.args)
        self.assertNotIn("No message was queued", str(caught.exception))
        self.assertIn("Do not retry automatically", str(caught.exception))

    def test_other_sqlite_failures_are_not_misdiagnosed_as_permissions(self):
        with patch.object(relay.sqlite3, "connect", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                cli(self.pair.state, self.args)

    def test_instructions_check_access_before_sending_and_do_not_bypass_denials(self):
        instructions = self.pair.instructions(A)
        self.assertIn("Before sending", instructions)
        self.assertIn("normal per-command approval", instructions)
        self.assertIn("Never automatically repeat a queued send", instructions)
        self.assertIn("If approval is denied, stop", instructions)


if __name__ == "__main__":
    unittest.main()
