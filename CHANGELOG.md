# Changelog

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
