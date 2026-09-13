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

Codex receives messages using `codex queue --thread FULL_UUID --message TEXT`.
With CLI 0.153.3, a synthetic app-server probe verified direct `thread/queue/add`,
with no name lookup, resume, or model override. The CLI's native queue determines
when a message is processed; enqueue success is not a model response. An older
CLI without this command is unsupported. Native real-session compatibility still
depends on the installed CLIs; fake peers do not establish it after an upgrade.

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

Statuses distinguish controller queueing, native queueing, socket send,
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
