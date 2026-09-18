from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def native_delivery(receipts=True):
    with patch("codex_claude_local_relay.connections.codex.Client") as factory:
        client = factory.return_value.__enter__.return_value
        client.deliver.return_value = {
            "turn_id": "fixture-turn",
            "method": "turn/steer",
        }
        client.observed.side_effect = lambda native, ids, turn=None: (
            set(ids) if receipts else set()
        )
        yield client.deliver
