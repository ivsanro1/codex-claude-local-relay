# Changelog

## 0.4.1

- MCP server: bind the Claude session from the parent process's live registry row only. The
  `CLAUDE_CODE_SESSION_ID` cross-check is gone: Claude Code sets that variable when it spawns the server
  and never updates it, so after an in-app `/resume` every tool call failed with "The Claude session named
  in the environment differs from the parent process registry" although the registry already named the
  resumed session. Resumed sessions now keep `send`, `peers` and `status`.

## 0.4.0

- Add `codex-claude-local-relay-mcp` (`python -m codex_claude_local_relay.mcp`): a stdio MCP server with
  `send`, `peers` and `status` tools for controller-managed connections. It binds the calling session from
  evidence only: the parent Claude Code process's live registry row, or the `_meta.threadId` that the Codex
  App Server attaches to every tool call, checked against the runtime that spawned the server. Mailbox writes
  happen in the server process, so agents no longer need a writable state directory or `--add-dir`.
- Register live bridges under `<connections root>/mcp/<pid>.json`; `mcp.bridge_status()` lets a controller
  check that an exact session can reply before pairing it.
- `Connection.create(..., alias=, messaging="mcp")` saves a short alias and delivers compact envelopes:
  `[Local relay <alias>; message <id>; from <title>]` plus the body, with no routing footer on either
  provider and no `PROJECT_RELAY` header on Claude legs. Legacy connections keep every existing string.
- Claude legs of MCP connections accept unthreaded native replies; each leg socket belongs to one connection.
- `codex-relay-session --connections-root DIR` registers the MCP server on the App Server through
  `-c mcp_servers.switchboard.*`, which is not a permission override, so resuming keeps working.

## 0.3.1

- Stop all owned fixture processes before deleting temporary homes, including native background plugin helpers.

- Wait for native thread readiness in the real TUI regression. The model label can render before thread loading completes; the check still fails if the thread never becomes ready.

## 0.3.0

- Deliver Codex peer input into active turns through the owning App Server, with explicit idle starts and no silent native-queue fallback.
- Add a native terminal launcher exposing a private control socket while preserving the terminal lifetime and normal settings.
- Persist acceptance and conversation-input receipts; expose stalled, blocked, ambiguous and legacy-untracked messages without replaying them.
- Test real Codex scheduling against a deterministic local model on every change and daily against baseline/latest CLI versions.

## 0.2.4

- Accept message text on the same line as the routing header, preserving the exact connection and reply IDs.
- Report received messages with missing or foreign routing headers in pair status. Retain their original mailbox records without automatic replay.

## 0.2.3

- Preserve reply addresses across relay restarts, including recovered legacy addresses.
- Check listener reachability and repair missing sockets without peer messages or model calls.
- Add controller maintenance for idle connections and regressions for silent recovery, stale sockets and foreign replacements.

## 0.2.2 — 2026-09-13

- Explain mailbox access failures with the exact state path, SQLite side-file requirements, and normal approved execution or a native Codex directory grant.
- Refuse missing or partial pair state without creating an empty database. Distinguish initialization failures where nothing was queued from uncertain send failures that must not be retried automatically.
- Add Codex routing instructions for sandbox access without bypassing approval denials.

## 0.2.1 — 2026-09-10

- Add a transactional controller preparation hook before delivery selection, allowing application protocols to hold messages until a handshake completes without changing native transports or CLI routing.

## 0.2.0 — 2026-09-10

- Add immutable session-pair mailboxes for Switchboard and other local controllers.
- Notify and relay to Codex through its native UUID-addressed queue; retain authenticated Claude peer transport.
- Reject mismatched sender context, recipient IDs, connection threads, and foreign reply IDs.
- Persist separate delivery statuses, cancel unsent messages on disconnect, and never automatically retry ambiguous sends.

## 0.1.0 — 2026-09-05

Initial experimental release.

- Installable `codex-claude-local-relay` CLI, Python 3.10+, no runtime dependencies.
- Linux peer discovery and enrollment pinned to a live Claude session UUID.
- Persistent threaded messages, delivery receipts, cursors, bounded waits, and Markdown export.
- Automatic reply instructions using Claude's native `SendMessage` tool.
- Per-project private state outside source repositories; explicit multi-mailbox support.
- Sender naming, read-only diagnostics, detached/foreground daemon lifecycle.
- Live compatibility tested with Claude Code 2.1.261, peer protocol 1.
- Verified both Codex-initiated requests and Claude-initiated independent threads.

Limitations: Linux only; internal Claude protocol; one recipient per mailbox;
no automatic wake-up for idle sender models; reply address changes on restart.
