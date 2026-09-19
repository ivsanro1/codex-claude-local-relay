# Controller-managed session pairs (0.2+)

`codex_claude_local_relay.connections.Connection` is an optional controller API.
A host such as Switchboard creates a private state directory, passes exactly two
participants (`id` is `codex:UUID` or `claude:UUID`, plus `cwd`), and calls `tick`
with a callback that verifies each exact recipient is still live. A controller
must serialize tick/disconnect and call `recover()` once after acquiring exclusive
ownership on startup. The legacy per-project mailbox API is unchanged.

Creation queues a separate notice for each participant in one local transaction.
Native deliveries are independent, not atomic. A pair supports either provider
on either side. Every Claude endpoint has an independent legacy peer mailbox;
its process-authenticated replies are forwarded only when their `PROJECT_RELAY`
thread matches the connection ID and their optional reply ID belongs to that leg.

Codex receives live input through the App Server owning the selected thread.
The controller's validation callback returns `{"codex_socket": "/private/runtime.sock"}`
from that exact writer process. Active turns receive `turn/steer` with the expected
turn ID; idle threads receive `turn/start`. No thread is resumed, no model or
permission is overridden, and no fallback native queue is used. Codex 0.155.0 is
the regression baseline. A detached or incompatible runtime fails visibly.

Launch a native terminal with `codex-relay-session --` or
`codex-relay-session -- resume FULL_UUID`. Its private runtime stays with the
terminal, retains normal Codex configuration, and closes when the terminal exits.
This does not attach to an already-running unexposed terminal: end that terminal
normally before resuming. Never start a second writer to it.

`accepted_native` means the API accepted the input; `input_observed` means the
exact message was subsequently found in conversation input. Neither proves the
model understood or answered. Receipt records, native turn IDs and timestamps
survive controller restart. `delivery_health()` reports unresolved failures and
receipts overdue by 60 seconds, independently of the latest successful message.
`retry_blocked()` explicitly retries only messages known not to have been submitted.
Approval/input waits and pre-submission turn changes defer safely. Ambiguous
responses or crashes are reconciled without resending. Legacy `queued_native`
rows remain untracked and are never replayed by upgrading.

Run `CODEX_RELAY_RUNTIME_TEST=1 python -m unittest discover -s tests -p test_codex_runtime.py -v`
to certify an installed Codex version. This uses the real runtime with an isolated
home and deterministic local HTTP model, without an account or model-provider
request. It tests active sampling, an in-flight tool call, idle wakeup, durable
controller receipts, and the absence of a native queue backlog. Daily CI tests
the pinned baseline and latest npm release. A missing CLI fails this explicit
check; ordinary unit-test runs label it skipped.

Codex receives a generated command of this form:

```bash
codex-claude-local-relay --state /private/connection-directory pair-send \
  --from-session codex:11111111-1111-4111-8111-111111111111 \
  --to-session claude:22222222-2222-4222-8222-222222222222 \
  --file /path/to/message.txt
```

Both IDs must match the immutable pair. `CODEX_THREAD_ID` must match the sender.
An optional `--reply-to` must refer to a message addressed to that sender in this
same pair. An explicit `--state` is required; there is no project-default fallback.
Claude uses its native `SendMessage` to the provided leg address and the exact
connection ID as its thread. Do not substitute another connection's address.

`pair-read --after CURSOR` returns persisted messages and per-endpoint status.
The library's `read(None)` returns the latest 200 messages for a UI; integer
cursors page forward through the complete history. State is durable under the
connection directory; no transcript or credential file is copied into it.

Statuses distinguish controller queueing, direct input acceptance/observation, socket send,
delivery receipt, denied/held, blocked recipient, and unknown delivery. A send
interrupted by a crash is not retried automatically. Disconnect cancels pending
controller messages and stops Claude mailbox daemons, preserving history.
Already queued native messages cannot be recalled. While the controller is
offline, Claude's daemon can retain replies but does not forward them onward.

These checks prevent guessing recipients and reject inconsistent routing data.
They cannot prove a model intended to choose a particular valid connection, or
isolate programs sharing the Linux account. Environment checks are accidental
misrouting protection, not an authentication boundary against that same user.
Models retain other tools and may choose to use them. A universal 100% guarantee
against any wrong-session message would require a stronger execution boundary.

## MCP messaging

`codex_claude_local_relay.mcp` serves `send`, `peers` and `status` over MCP stdio for
connections created with `Connection.create(..., alias="pair-1", messaging="mcp")`.
The alias (1–40 characters of `[A-Za-z0-9_.-]`) is what models and users see; full
session UUIDs stay in `connection.json` and controller diagnostics.

Binding is evidence-only and fails closed:

- **Claude**: the server walks its parent processes and uses the live
  `<CLAUDE_CONFIG_DIR>/sessions/<pid>.json` row (process-start identity checked). It is
  resolved on every call, so an in-app resume that changes the session is followed. When
  `CLAUDE_CODE_SESSION_ID` is present it must agree with the registry.
- **Codex**: every `tools/call` carries `_meta.threadId`, inserted by the App Server
  (Codex 0.155.0 `core/src/mcp_tool_call.rs`). The server validates the UUID and asks the
  App Server that owns it (found through `--listen unix://` in an ancestor's command line,
  or `--runtime-socket` from the controller when they agree) to confirm the thread is
  loaded. Calls without that metadata queue nothing.

On startup the server writes `<root>/mcp/<pid>.json` (`provider`, `pid`, `proc_start`,
`parent_pid`, `session` for Claude, `runtime_socket` for Codex) and removes it on exit.
`mcp.bridge_status(root, "claude:UUID", {"pid": CLAUDE_PID})` or
`mcp.bridge_status(root, "codex:UUID", {"codex_socket": PATH})` tells a controller whether
that exact session can reply; only files whose process is confirmed gone are deleted. For
Claude the answer follows the live registry row of the bridge's parent process, so an in-app
resume that changes the session is reflected before any tool call; the recorded `session`
is informational. A bridge that started before Claude Code registered its session records
its parent only, and matches through the `pid` route until the session appears.

Envelopes on MCP connections are `[Local relay <alias>; message <id>; from <title>]`
followed by the body. There is no per-message footer for either provider and no
`PROJECT_RELAY` header on Claude legs; the tool descriptions and the initial notice
explain how to reply. A Claude leg still accepts a native `SendMessage` reply, threaded
or not, because its socket belongs to exactly one connection. Connections without the
`messaging` field keep the previous CLI route, header and footers unchanged.

`codex-relay-session --connections-root DIR -- [codex args]` passes
`-c mcp_servers.switchboard.command=…` and `.args=[…]` to the App Server. These keys are
not permission overrides, so `resume` and in-app `/resume` keep working; do not add
`--add-dir` for the mailbox any more.

## Sandbox access

The sender needs write access to the state directory, including SQLite WAL and
journal files. Changing only the database file's Unix mode does not make a path
writable inside a read-only sandbox mount. Controllers should create a private
parent relay directory before launching Codex, then pass `--add-dir PARENT` on
each native start and resume so later-created connection databases are covered.
This native option retains other sandbox and approval settings; read-only
configurations and already running sessions still need their normal approved
execution path. Do not change global agent settings or restart a live session
without the user's authorization.

Pair commands require both `connection.json` and `mail.sqlite`; only the
controller creates them. A missing or partially created pair reports what is
missing without constructing a replacement. An access failure during
initialization explicitly reports that no message was queued. Resolve access
before sending once. A failure during the send transaction reports uncertain
queueing and must be inspected before another attempt. A queued or ambiguous
send must never be repeated automatically, and an approval denial means stop.

Permission to share task material is separate from filesystem access. The host
application should show and record the user's sharing scope at pairing time,
including any model-provider processing, while preserving existing permissions
and the selected workflow's sharing barriers.

## Controller restart recovery

Call `Connection.maintain()` during startup and local maintenance ticks even when
there are no outgoing messages. This restores enabled Claude listeners at their
persisted reply address; it creates no peer messages and consumes no model tokens.
`recover()` continues to mark ambiguous deliveries unconfirmed, without retrying
them. Maintenance must not be implemented as model prompts or peer keepalives.
