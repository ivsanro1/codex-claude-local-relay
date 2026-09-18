import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from codex_claude_local_relay import codex, relay
from codex_claude_local_relay.connections import Connection

A = "codex:10000000-0000-4000-8000-000000000001"
B = "codex:10000000-0000-4000-8000-000000000002"


class ScriptedClient(codex.Client):
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def call(self, method, params, **kwargs):
        self.calls.append((method, params, kwargs))
        result = next(self.replies)
        if isinstance(result, Exception):
            raise result
        return result


def thread(state="active", **fields):
    return {
        "thread": {
            "id": A[6:],
            "canAcceptDirectInput": True,
            "status": {"type": state, "activeFlags": []},
            **fields,
        }
    }


class DeliveryTests(unittest.TestCase):
    def test_active_message_steers_same_turn_without_queue_resume_or_overrides(self):
        client = ScriptedClient(
            [
                thread(),
                {"data": [{"id": "long-turn", "status": "inProgress"}]},
                {"turnId": "long-turn"},
            ]
        )
        result = client.deliver(A[6:], "message-one", "peer correction")
        self.assertEqual(result, {"turn_id": "long-turn", "method": "turn/steer"})
        self.assertEqual(
            [c[0] for c in client.calls],
            ["thread/read", "thread/turns/list", "turn/steer"],
        )
        self.assertEqual(
            client.calls[-1][1],
            {
                "threadId": A[6:],
                "expectedTurnId": "long-turn",
                "clientUserMessageId": "message-one",
                "input": [{"type": "text", "text": "peer correction"}],
            },
        )

    def test_idle_message_starts_one_turn(self):
        client = ScriptedClient([thread("idle"), {"turn": {"id": "new-turn"}}])
        self.assertEqual(client.deliver(A[6:], "id", "text")["method"], "turn/start")
        self.assertEqual(len(client.calls), 2)

    def test_unloaded_foreign_unsteerable_and_approval_blocked_do_not_submit(self):
        for reply in [
            thread("notLoaded"),
            thread(id=B[6:]),
            thread(canAcceptDirectInput=False),
            thread(status={"type": "active", "activeFlags": ["waitingOnApproval"]}),
        ]:
            with self.subTest(reply=reply):
                client = ScriptedClient([reply])
                with self.assertRaises(codex.Unavailable):
                    client.deliver(A[6:], "id", "text")
                self.assertEqual(len(client.calls), 1)

    def test_turn_completion_race_and_rejected_steer_never_start_second_turn(self):
        for replies in [
            [thread(), {"data": []}],
            [thread(), {"data": [{"id": "old", "status": "completed"}]}],
            [
                thread(),
                {"data": [{"id": "old", "status": "inProgress"}]},
                codex.Rejected("Turn changed"),
            ],
        ]:
            client = ScriptedClient(replies)
            with self.assertRaises(codex.Unavailable):
                client.deliver(A[6:], "id", "text")
            self.assertNotIn("turn/start", [c[0] for c in client.calls])

    def test_observation_requires_user_input_and_pages_backwards(self):
        client = ScriptedClient(
            [
                {
                    "data": [
                        {"id": "one", "type": "agentMessage"},
                        {
                            "type": "userMessage",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Quoted [Local relay c; message one;]",
                                }
                            ],
                        },
                    ],
                    "nextCursor": "older",
                },
                {
                    "data": [
                        {
                            "type": "userMessage",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "[Local relay c; message one; from peer]\nbody",
                                }
                            ],
                        }
                    ]
                },
            ]
        )
        self.assertEqual(client.observed(A[6:], {"one"}, "turn"), {"one"})
        self.assertEqual(client.calls[-1][1]["cursor"], "older")

    def test_missing_socket_never_spawns_or_queues(self):
        with patch.object(codex, "unix_connect") as spawn:
            with self.assertRaises(codex.Unavailable):
                codex.Client("/fixture/codex", None)
            spawn.assert_not_called()

    def test_changed_runtime_schema_never_becomes_a_silent_send(self):
        for replies in [[{}], [{"thread": None}], [thread(), {"data": None}]]:
            with self.subTest(replies=replies), self.assertRaises(codex.Unavailable):
                ScriptedClient(replies).deliver(A[6:], "id", "text")
        with self.assertRaises(codex.Unconfirmed):
            ScriptedClient([thread("idle"), {"turn": None}]).deliver(
                A[6:], "id", "text"
            )


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.link = Connection.create(
            root / "pair", [{"id": A, "cwd": str(root)}, {"id": B, "cwd": str(root)}]
        )
        patcher = patch("codex_claude_local_relay.connections.codex.Client")
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.factory.return_value.__enter__.return_value
        self.client.deliver.return_value = {
            "turn_id": "active-turn",
            "method": "turn/steer",
        }
        self.client.observed.return_value = set()
        self.validate = lambda _: {"codex_socket": "/fixture/socket"}

    def test_acceptance_is_not_receipt_and_reconciliation_survives_restart(self):
        self.link.tick(self.validate, "/fixture/codex")
        first = self.link.read()[0]
        self.assertEqual(first["status"], "accepted_native")
        self.assertEqual(first["wire_id"], "active-turn")
        self.client.observed.return_value = {first["id"]}
        restarted = Connection(self.link.state)
        restarted.recover()
        restarted.tick(self.validate, "/fixture/codex")
        self.assertEqual(restarted.read()[0]["status"], "input_observed")
        self.assertEqual(
            self.client.deliver.call_count, 2
        )  # Two distinct initial notices.

    def test_timeout_and_crash_are_reconciled_without_resend(self):
        self.client.deliver.side_effect = codex.Unconfirmed("lost response")
        self.link.tick(self.validate, "/fixture/codex")
        first = self.link.read()[0]
        self.assertEqual(first["status"], "unknown")
        with relay.connect_db(self.link.state) as db:
            db.execute(
                "UPDATE pair_messages SET status='sending' WHERE id=?", (first["id"],)
            )
            db.execute(
                "UPDATE pair_messages SET status='cancelled' WHERE id!=?",
                (first["id"],),
            )
        self.link.recover()
        self.link.retry_blocked()
        self.client.observed.return_value = {first["id"]}
        self.link.tick(self.validate, "/fixture/codex")
        self.link.tick(self.validate, "/fixture/codex")
        self.assertEqual(self.client.deliver.call_count, 1)
        self.assertEqual(self.link.read()[0]["status"], "input_observed")

    def test_stalled_receipt_and_legacy_queue_are_visible_even_after_new_success(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with relay.connect_db(self.link.state) as db:
            db.execute(
                "UPDATE pair_messages SET status='accepted_native',at=? WHERE seq=1",
                (old,),
            )
            db.execute("UPDATE pair_messages SET status='input_observed' WHERE seq=2")
        health = self.link.snapshot()["delivery"]
        self.assertEqual(health["state"], "attention")
        self.assertEqual(health["issue_count"], 1)
        self.assertGreaterEqual(health["issues"][0]["age_seconds"], 300)
        with relay.connect_db(self.link.state) as db:
            db.execute("UPDATE pair_messages SET status='queued_native' WHERE seq=1")
        self.assertIn(
            "untracked", self.link.snapshot()["delivery"]["issues"][0]["error"]
        )

    def test_disconnect_cannot_wake_or_resend(self):
        self.link.disconnect()
        self.link.tick(self.validate, "/fixture/codex")
        self.factory.assert_not_called()
        with self.assertRaises(ValueError):
            self.link.retry_blocked()

    def test_unconfirmed_backlog_cannot_starve_newer_receipts(self):
        with relay.connect_db(self.link.state) as db:
            db.execute("DELETE FROM pair_messages")
            for n in range(101):
                identifier = self.link._insert(db, B, A, str(n))
                db.execute(
                    "UPDATE pair_messages SET status='unknown' WHERE id=?",
                    (identifier,),
                )
                db.execute(
                    "INSERT INTO pair_meta VALUES (?,?)",
                    ("delivery:" + identifier, "{}"),
                )
        self.client.observed.side_effect = lambda native, ids, turn: ids & {identifier}
        self.link.reconcile_codex(self.validate, "/fixture/codex")
        self.link.reconcile_codex(self.validate, "/fixture/codex")
        self.assertEqual(self.link.read()[-1]["status"], "input_observed")
        self.client.deliver.assert_not_called()

    def test_retry_only_requeues_definitely_unsent_messages(self):
        with relay.connect_db(self.link.state) as db:
            db.execute("UPDATE pair_messages SET status='blocked' WHERE seq=1")
            db.execute("UPDATE pair_messages SET status='unknown' WHERE seq=2")
        self.link.retry_blocked()
        self.assertEqual([r["status"] for r in self.link.read()], ["queued", "unknown"])

    def test_waiting_recipient_does_not_starve_peer_or_reorder_its_own_messages(self):
        self.client.deliver.side_effect = [
            codex.RetryLater("approval pending"),
            {"turn_id": "b", "method": "turn/start"},
            {"turn_id": "a", "method": "turn/steer"},
        ]
        self.link.send(B, A, "Later message to A", verify_context=False)
        self.link.tick(self.validate, "/fixture/codex")
        self.assertEqual(self.link.read()[0]["status"], "deferred")
        self.link.tick(self.validate, "/fixture/codex")
        self.link.tick(self.validate, "/fixture/codex")
        calls = self.client.deliver.call_args_list
        self.assertEqual([c.args[0] for c in calls], [A[6:], B[6:], A[6:]])
        self.assertEqual(calls[0].args[1], calls[2].args[1])
        self.assertEqual(self.link.read()[2]["status"], "queued")


if __name__ == "__main__":
    unittest.main()
