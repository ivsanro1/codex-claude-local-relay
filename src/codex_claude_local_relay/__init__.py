"""Local peer messaging for an existing Claude Code session."""
__version__ = "0.1.0"


def main():
    import sqlite3
    import sys

    if sys.platform != "linux":
        print("codex-claude-local-relay currently supports Linux only (Unix sockets and /proc).", file=sys.stderr)
        return 1
    from .relay import main as run

    try:
        return run()
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"codex-claude-local-relay: {exc}", file=sys.stderr)
        return 1
