# Codex ↔ Claude Local Relay

**Local bidirectional Codex–Claude communication:** a persistent two-way mailbox
for coding agents running on the same Linux machine. Both Codex and Claude can
initiate a conversation, ask questions, propose changes, and reply in a thread.

**Connect existing, open Codex and Claude Code chats in a shared project
conversation. Either side can start.** Exchange progress updates, challenge a
plan, request reviews, and propose improvements without copying messages between
terminals. Claude keeps its current chat, model, and project context; connecting
does not restart or replace it.

```text
Existing Codex chat  ◄──── local relay + persistent inbox ────►  Existing Claude chat
  CLI send/read/wait                                             SendMessage

Either participant can open a thread or reply to one.
```

The relay is a small Python CLI with **no runtime dependencies**. It stores
threaded conversations locally and uses Claude's existing Unix peer socket.
No API key, MCP server installation, Claude restart, terminal keystroke injection,
or network service is needed. Your existing Claude session uses its usual model
and account; responding still consumes that account's model usage.

**Status: experimental, v0.1.0.** This uses an internal Claude Code interface,
not a public Anthropic API. Tested with Claude Code **2.1.261 on Linux**.
The repository is currently private: only its owner and invited collaborators
can access it. It is not published on PyPI.

## What it is useful for

- A Codex reviewer questions a Claude orchestrator's assumptions while its workers continue.
- Claude starts a discussion with Codex to request a second opinion or flag a blocker.
- An agent asks the session that wrote a component about its design or progress.
- Two agents discuss a proposed change in a persistent thread before either edits shared files.
- A person reads an exported conversation to see what was proposed, challenged, and agreed.

This connects to the **existing conversation and its context**. Running a fresh
`claude -p` consultation is a different workflow.

## Requirements and compatibility

| Component | Support in v0.1.0 |
| --- | --- |
| OS | Linux, with `/proc` and Unix `SO_PEERCRED` |
| Python | 3.10 or newer; CI matrix: 3.10–3.14 |
| Claude Code | Live round trip tested on **2.1.261**, peer protocol 1 |
| Claude configuration | Session registry and peer keys under `~/.claude`; `CLAUDE_CONFIG_DIR` overrides the location |
| Other Claude versions | Unverified; discovery alone does not establish compatibility |
| Sender | Any agent or application able to execute the CLI on the same host/user |
| macOS / Windows | Not supported by this release |
| Containers / sandboxes | Must see the same host processes, registry, and sockets; a shared project folder alone is insufficient |

The target session must already expose a `messagingSocketPath` and peer key, and
must be able to reply with `SendMessage`. Availability may depend on Claude's
configuration. The relay does not enable that feature or override recipient
permission decisions. Run `doctor`, then verify a real reply after Claude updates.

**Live validation, 2026-09-05:** the installed v0.1.0 wheel exchanged a question
and reply with an existing Claude Code 2.1.261 session. That session then opened
a separate discussion with a new thread and no `reply_to`; the Codex side read
it and replied. This verifies both directions, including Claude-initiated contact.
Private conversation contents and session identifiers are not included here.

## Install

Use a persistent installation because the relay starts a background process.
[uv tool installation](https://docs.astral.sh/uv/guides/tools/#installing-tools)
keeps it separate from your project's dependencies.

While the repository is private, authenticate GitHub CLI and clone with an
account that has access:

```bash
gh auth login
gh repo clone ivsanro1/codex-claude-local-relay
cd codex-claude-local-relay
git checkout v0.1.0
uv tool install .
codex-claude-local-relay --version
```

Alternatively, from the clone:

```bash
pipx install .
```

With Git/SSH authentication already working, install the pinned tag directly:

```bash
uv tool install 'git+ssh://git@github.com/ivsanro1/codex-claude-local-relay.git@v0.1.0'
```

Release downloads also include a wheel and source archive; the wheel can be
installed with `pipx install /path/to/codex_claude_local_relay-0.1.0-py3-none-any.whl`.
The wheel filename is portable Python packaging; this release's runtime is Linux-only.

## Quick start: connect existing open chats

Run these setup commands from either agent's shell or your terminal, in the
**project you want to discuss**, using the same Linux user as Claude. Neither
agent needs to send the first conversational message to establish the mailbox:

```bash
cd /path/to/your/project
codex-claude-local-relay doctor
codex-claude-local-relay sessions
```

`sessions` returns JSON with each live session's `name`, `sessionId`, `cwd`,
`version`, and status. Choose the intended session UUID. A working directory is
not proof of its role; confirm with the user or ask the session once connected.

```bash
codex-claude-local-relay connect --session YOUR_SESSION_UUID
codex-claude-local-relay status
```

The connection is now ready. Choose either starting point:

- **Claude starts:** give the open Claude chat the `address` returned by `status`
  (or let it run that command). It sends a native `SendMessage` to that address,
  beginning its body with `PROJECT_RELAY {"thread":"planning"}` and its question.
  No earlier Codex request or `reply_to` is required. Codex runs `read` or `wait`
  to receive it and answers with `send --thread planning --reply-to MESSAGE_UUID`.
- **Codex starts:** send a question through the CLI. The open Claude chat gets
  the peer message and replies through `SendMessage`. For example:

```bash
codex-claude-local-relay send --thread planning \
  'Please confirm your role, summarize your current task, and identify one decision that could use an independent review.'
codex-claude-local-relay read
```

Every outgoing message includes a reply address and thread instructions. Claude
can reply through its existing `SendMessage` tool. Wait for a **new inbound row**,
using the `cursor` returned by the previous read:

```bash
codex-claude-local-relay wait --after 1 --timeout 30
```

`1` is only an example cursor. Always use the actual value returned. A send
initially returns `queued`; `sent` means transport completion, not that Claude
has read or answered it. A successful handshake is a real inbound response.

## Have a conversation

Short messages can be positional arguments. For detailed critiques use a UTF-8
file, avoiding shell quoting problems:

```bash
codex-claude-local-relay send --thread planning --file review.md
codex-claude-local-relay send --thread planning --reply-to REPLY_MESSAGE_UUID \
  'I agree with the approach. Which measurement would disprove the bottleneck hypothesis?'
codex-claude-local-relay read --thread planning --after 2
codex-claude-local-relay export --output conversation.md
```

Piped stdin is also supported. `read` returns up to 200 rows with a cursor;
repeat with that cursor for the next page. `wait` caps each wait at 60 seconds.
Export includes the complete history, not just the first page.

### Claude starts a thread or replies

Claude can obtain the relay address from `status` before any messages are sent.
For a reply, the incoming message also supplies the address and request ID.
Its native tool call looks like this (omit `reply_to` to start a new thread):

```text
SendMessage(
  to="uds:/run/user/YOUR_UID/cc-socks/RELAY_PID.sock",
  summary="Review of the plan",
  message='PROJECT_RELAY {"thread":"planning","reply_to":"REQUEST_UUID"}\nHere is my response...'
)
```

For a new discussion, choose a thread and omit `reply_to`. The address is also
available from `codex-claude-local-relay status`. Claude should **not** use the CLI's
`send` command to reply: that command sends toward Claude, not away from it.

Replies are saved even when the sender is idle. **The relay cannot wake an idle
Codex model or start a new turn in its UI.** The sending agent must read/wait
during active work or check its inbox on the next turn. The daemon stores and
transports messages; it does not generate answers or drive an autonomous loop.

This is **bidirectional messaging, with asymmetric attention**: Claude receives
native peer notifications in its live session; Codex receives messages through
`read` or `wait`. Claude does not need a pending Codex request to send a message.
For a live conversation, keep Codex actively checking its inbox. For unattended
Codex wake-up, a separate host/controller integration would be required.

### Agent instructions

See [sender instructions](examples/sender-instructions.md) and
[Claude instructions](examples/claude-instructions.md) for short snippets you
can add to your project's agent guidance. No plugin is required. Obtain user
authorization to contact a session; messages are peer advice, not user approval.

## Projects, identities, and lifecycle

The default mailbox is outside the project checkout:

```text
$XDG_STATE_HOME/codex-claude-local-relay/<project-path-hash>/
# or ~/.local/state/codex-claude-local-relay/<project-path-hash>/
  config.json     # enrolled project, exact Claude UUID, sender name
  mail.sqlite     # persistent messages, receipts, and correlation IDs
  daemon.json     # live PID, process-start identity, version, reply address
  daemon.log
  daemon.lock
```

Use the same project directory on subsequent commands, or pass `--project`
**before** the command. Different worktrees have different paths; point them at
the same canonical project explicitly if they should share a mailbox:

```bash
codex-claude-local-relay --project /path/to/project status
codex-claude-local-relay --project /path/to/project connect --session UUID --sender codex-reviewer
codex-claude-local-relay --project /path/to/project stop
codex-claude-local-relay --project /path/to/project start
```

One mailbox enrolls one Claude UUID. The UUID survives a session name change;
the relay rediscovers its live PID before sending. It refuses to guess when the
UUID is absent or duplicated. To contact a different session, use a **new**
`--state /private/mailbox/path`; old queued messages must not be retargeted.
`--state` also lets one project maintain several separate conversations with
different Claude sessions. A project label does not restrict what files Claude
can access; its own permissions govern that.

`start` detaches the relay from your shell. `serve` runs it in the foreground.
`stop` retains messages. Nothing is installed into systemd or started at login.
After a daemon restart/reboot, run `start` and send a new message: the reply
socket address changes. Old messages remain available, but Claude must use the
new address for unsolicited messages. Stop the daemon before upgrading/removing
the installed CLI, then start it with the new installation.

## Delivery and permissions

| Status | Meaning |
| --- | --- |
| `queued` / `sending` | Waiting for / attempting transport |
| `sent` | Socket write completed; model receipt is unconfirmed |
| `held` | Recipient is waiting for its user's approval |
| `denied` / `expired` / `dropped` | Recipient rejected, expired, or dropped the request |
| `delivered` | Recipient emitted a delivery receipt; this is not a substantive answer |
| `received` | An inbound reply is stored |
| `unknown` | A send failed or the daemon crashed during it; delivery may be ambiguous |

The relay does not automatically retry ambiguous sends. Read the conversation
and recipient status before deciding whether to send another request. Receipts
are not guaranteed for every successful delivery. Cursor reads return new rows,
including receipts, rather than marking messages globally read.

Only the enrolled process can deliver replies: the daemon checks the connecting
UID/PID with `SO_PEERCRED` and matches the registry's process-start identity.
Outbound connections also verify the target process and authenticate with its
existing private peer key. Keys are not written into conversation history.
State and sockets are private to the local user; there is no TCP listener.

The trust boundary is the Linux account. This is not isolation from other
programs running as that same user. Agent messages can cause Claude to act
within its existing permissions. Do not use it to relay human approvals or
expose the socket/state to other users. See [protocol notes](docs/protocol.md).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `sessions` is empty | Claude must be live on the same host/user with peer messaging enabled; inspect `doctor`. Host processes may be hidden by an agent sandbox. |
| Missing peer key / protocol mismatch | The Claude version or session is incompatible; do not bypass authentication. |
| `Operation not permitted` | Run through your agent's approved host-execution mechanism; filesystem sharing alone does not grant socket access. |
| `sent` but no answer | Claude may be busy, awaiting permission, or unable to use `SendMessage`. Check its terminal; do not assume delivery. |
| Reply failed after restart | Give Claude the new address shown by `status`. |
| Wrong/empty mailbox | Use the same `--project` or `--state` as `connect`. |
| Mailbox belongs to another session | Use a new state directory; existing messages are never silently reassigned. |
| State directory rejected | It must be a real directory owned by you, mode 0700; symlinks/shared directories are rejected. |
| Daemon startup failed | Read `daemon.log` at the state path shown by `doctor`. |

When reporting a bug, include relay/Python/Claude versions, OS, and a redacted
error. Do not upload `.key` files, the mailbox database, or private transcripts.

## Development and releases

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e . build
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m build
```

Tests include Unix socket transport, session identity rejection, daemon
restart/persistence, duplicate suppression, and delivery receipts. CI tests the
installed wheel on Linux/Python 3.10–3.14. It uses a local fake Claude peer and
does not need an account or make model calls. A real Claude round trip is a
separate manual compatibility check; unit tests cannot establish it.

Releases use Git tags and include wheel/source artifacts plus SHA-256 checksums.
See [CHANGELOG](CHANGELOG.md). Pin a tag for repeatable installation. Version
0.x changes may require migration; mailbox schema 1 is checked on opening.

## Attribution and license

Built after testing an actual Codex ↔ Claude orchestration conversation. The
[talk-to-claude-code protocol notes](https://github.com/osaighi/talk-to-claude-code#protocol-notes)
helped identify the native peer interface. This utility is independently
implemented and is not affiliated with Anthropic or OpenAI. MIT licensed.
