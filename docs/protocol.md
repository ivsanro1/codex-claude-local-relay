# Protocol and implementation notes

This is an experimental adapter for Claude Code's internal peer protocol 1,
verified against 2.1.261. No claim of compatibility with all versions is made.

## Discovery and identity

Read `<CLAUDE_CONFIG_DIR>/sessions/<pid>.json` (default `~/.claude`). Keep records
whose `procStart` matches field 22 of `/proc/<pid>/stat`, whose `peerProtocol` is
1, and which expose `messagingSocketPath`. Enrollment uses the exact session UUID.
Discovery reads registry metadata only, not conversations, model prompts, or tokens.

The relay binds `<Claude socket directory>/<relay pid>.sock`. The directory must
be owned by the current UID and not writable by other users/groups. The reply
socket is mode 0600. It is not registered as a pretend Claude session.

Before an outbound write, verify the target socket type/owner, read the private
`<pid>.<sha256(canonical socket path)>.key` peer key, validate its process-start
identity, then check the connected socket's PID and UID using `SO_PEERCRED`.

## Frames

Each connection carries newline-delimited JSON. Authentication is followed by
one peer message. The receiver's model sees the native cross-session wrapper:

```json
{"type":"auth","token":"LOCAL_PEER_TOKEN"}
{"type":"user","msgV":1,"msg_id":"REQUEST_UUID","from":"uds:/path/to/relay.sock","priority":"next","message":{"role":"user","content":"<cross-session-message from=\"uds:/path/to/relay.sock\" from-name=\"codex-reviewer\">\nPROJECT_RELAY {\"thread\":\"review\",\"message_id\":\"REQUEST_UUID\"}\nPeer message and reply instructions\n</cross-session-message>"}}
```

The relay never sends control operations to rename, stop, interrupt, or approve
actions in Claude. It honors hold/deny/drop receipts instead of trying another
delivery path. `sent` means only socket transport completion.

Native replies arrive through a new Unix connection from the enrolled Claude
process. Check its UID, PID, and current registry process-start identity. Parse
only message bodies and recognized delivery receipts; do not store auth frames.
`PROJECT_RELAY` provides application-level thread and reply correlation. Missing
metadata yields an unthreaded message rather than a discarded answer.

## Storage and failure behavior

SQLite WAL stores the outbox, inbox, receipts, message UUIDs, sequence cursors,
and statuses. Each operation commits and closes its connection. A daemon-level
file lock prevents two senders from consuming one mailbox. Configuration and
daemon metadata are replaced atomically. Message IDs suppress duplicate replies.

The daemon marks an outbound message `sending` before transport. A crash in that
interval is ambiguous, so the next daemon marks it `unknown`; it never retries
automatically. Queued messages remain queued until processed. A mailbox cannot
be reassigned to a different project/Claude UUID, preventing old requests from
being sent to a newly selected recipient.

There is no exactly-once delivery guarantee, model wake-up service, remote
transport, or authority propagation. The trust boundary is one Linux account.
Messages and exports may contain private project data; the application does not
upload them. The protocol may change independently of this package.
