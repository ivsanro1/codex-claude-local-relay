"""Direct input to an already loaded Codex runtime, never a second writer.

The public Unix transport uses WebSockets over a private local socket.
No queue/add, thread/resume, config overrides or automatic ambiguous retries.
"""

import json
import os
import stat
import time

from websockets.exceptions import WebSocketException
from websockets.sync.client import unix_connect


class Unavailable(RuntimeError):
    """No input was submitted; delivery can be explicitly retried."""


class Unconfirmed(RuntimeError):
    """Input may have been accepted. Reconcile, never retry automatically."""


class Rejected(Unavailable):
    pass


class RetryLater(Unavailable):
    """The runtime cannot accept input yet; no input was submitted."""


class Client:
    def __init__(self, executable, socket_path, timeout=5):
        if not socket_path:
            raise Unavailable(
                "This Codex session has no live delivery socket. End it normally and resume "
                "it through Switchboard, or launch it with codex-relay-session. "
                "No message was queued."
            )
        self.timeout = timeout
        self.sequence = 0
        try:
            metadata = os.stat(socket_path)
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise Unavailable(
                    "Codex control socket must belong to the current user"
                )
            self.connection = unix_connect(
                str(socket_path),
                uri="ws://localhost",
                compression=None,
                open_timeout=timeout,
                close_timeout=1,
                max_size=8_000_000,
            )
        except (OSError, WebSocketException, TimeoutError) as exc:
            raise Unavailable(
                "Cannot connect to this Codex runtime. No message was queued."
            ) from exc
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "local_relay", "version": "0.3.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._write({"method": "initialized"})
        except Exception:
            self.close()
            raise

    def _write(self, value):
        self.connection.send(json.dumps(value))

    def call(self, method, params, *, mutation=False):
        self.sequence += 1
        identifier = self.sequence
        try:
            self._write({"id": identifier, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                response = json.loads(self.connection.recv(timeout=remaining))
                if not isinstance(response, dict):
                    raise TypeError("Invalid RPC response")
                # Approvals belong to the native client. Never answer requests
                # or grant permissions on behalf of the user.
                if response.get("id") != identifier or "method" in response:
                    self.notification(response)
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    continue
                if "error" in response:
                    error = response["error"]
                    if not isinstance(error, dict):
                        raise TypeError("Invalid RPC error")
                    text = (
                        "Codex rejected "
                        + method
                        + ": "
                        + str(error.get("message", "RPC error"))[:400]
                    )
                    if mutation and error.get("code") not in (-32600, -32601, -32602):
                        raise Unconfirmed(
                            text + ". Receipt is uncertain; do not resend."
                        )
                    raise Rejected(text)
                if not isinstance(response.get("result"), dict):
                    raise TypeError("Invalid RPC result")
                return response["result"]
        except Rejected:
            raise
        except (
            OSError,
            WebSocketException,
            TimeoutError,
            ValueError,
            TypeError,
        ) as exc:
            error = Unconfirmed if mutation else Unavailable
            raise error(
                "Codex live delivery could not confirm "
                + method
                + " ("
                + type(exc).__name__
                + "). "
                + (
                    "Do not resend; inspect delivery status."
                    if mutation
                    else "No input was submitted."
                )
            ) from exc

    def notification(self, value):
        """Observer hook. Production adapters never answer approval/tool requests."""

    def thread(self, native):
        thread = self.call("thread/read", {"threadId": native}).get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("status"), dict):
            raise Unavailable(
                "Codex returned an incompatible thread response. No input was submitted."
            )
        if thread.get("id") != native or thread.get("canAcceptDirectInput") is not True:
            raise Unavailable(
                "The selected Codex thread is not accepting direct input in this runtime. No message was queued."
            )
        status = thread.get("status", {})
        if status.get("type") not in ("active", "idle"):
            raise Unavailable(
                "The selected Codex thread is not loaded and ready. No message was queued."
            )
        return thread

    def deliver(self, native, message_id, text):
        thread = self.thread(native)
        status = thread["status"]
        if status.get("activeFlags"):
            raise RetryLater(
                "Codex is waiting for approval or user input. Delivery will continue after that request is resolved."
            )
        params = {
            "threadId": native,
            "clientUserMessageId": message_id,
            "input": [{"type": "text", "text": text}],
        }
        if status["type"] == "active":
            turns = self.call(
                "thread/turns/list",
                {"threadId": native, "limit": 1, "itemsView": "notLoaded"},
            ).get("data")
            if not isinstance(turns, list) or (
                turns and (not isinstance(turns[0], dict) or not turns[0].get("id"))
            ):
                raise Unavailable(
                    "Codex returned incompatible turn data. No input was submitted."
                )
            if not turns or turns[0].get("status") != "inProgress":
                raise RetryLater(
                    "Codex changed turns before delivery. Waiting to select its current turn; no input was submitted."
                )
            params["expectedTurnId"] = turns[0]["id"]
            method = "turn/steer"
            result = self.call(method, params, mutation=True)
            turn_id = result.get("turnId")
        else:
            method = "turn/start"
            result = self.call(method, params, mutation=True)
            turn = result.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise Unconfirmed(
                "Codex accepted input without a usable turn ID. Do not resend."
            )
        return {"turn_id": turn_id, "method": method}

    def observed(self, native, message_ids, turn_id=None):
        """Bounded history scan; no native state is changed by reconciliation."""
        found = set()
        params = {"threadId": native, "limit": 100, "sortDirection": "desc"}
        if turn_id:
            params["turnId"] = turn_id
        for _ in range(10):
            result = self.call("thread/items/list", params)
            for entry in result.get("data", []):
                item = entry.get("item", entry)
                if item.get("type") != "userMessage":
                    continue
                if item.get("id") in message_ids:
                    found.add(item["id"])
                for part in item.get("content", []):
                    text = part.get("text", "")
                    if part.get("type") == "text" and text.startswith("[Local relay "):
                        for identifier in message_ids:
                            if (
                                "; message " + identifier + ";"
                                in text.split("\n", 1)[0]
                            ):
                                found.add(identifier)
            cursor = result.get("nextCursor")
            if not cursor or found == set(message_ids):
                break
            params["cursor"] = cursor
        return found

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def socket_from_process(pid, proc_root="/proc"):
    """Exact process identity must be checked by the caller before using this."""
    try:
        with open(f"{proc_root}/{pid}/cmdline", "rb") as source:
            args = [p.decode() for p in source.read().split(b"\0") if p]
        index = args.index("--listen")
        address = args[index + 1]
        if "app-server" in args and address.startswith("unix:///"):
            return address[len("unix://") :]
    except (OSError, ValueError, IndexError, UnicodeError):
        pass
    return None
