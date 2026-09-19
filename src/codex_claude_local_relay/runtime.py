"""Run the native Codex TUI with a private, externally steerable runtime.

Both children stay in the terminal's process group and lifetime. Switchboard's
existing persistent tmux service therefore preserves them across app updates.
"""

import argparse
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def mcp_overrides(connections_root):
    """App Server config overrides that register the bundled MCP server; no permission keys."""
    root = Path(connections_root).absolute()
    if not root.is_dir():
        raise SystemExit(f"Connections root does not exist: {root}")
    command = [sys.executable, "-m", "codex_claude_local_relay.mcp", "--connections-root", str(root), "--provider", "codex"]
    return [
        "-c", "mcp_servers.switchboard.command=" + json.dumps(command[0]),
        "-c", "mcp_servers.switchboard.args=" + json.dumps(command[1:]),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--socket", type=Path)
    parser.add_argument(
        "--connections-root",
        type=Path,
        help="Give every thread in this runtime the Switchboard MCP messaging tools for this directory",
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    extra = args.args[1:] if args.args[:1] == ["--"] else args.args
    overrides = mcp_overrides(args.connections_root) if args.connections_root else []
    with tempfile.TemporaryDirectory(prefix="codex-relay-") as temporary:
        socket = args.socket or Path(temporary) / "runtime.sock"
        parent = socket.parent.stat()
        if (
            parent.st_uid != os.getuid()
            or parent.st_mode & 0o077
            or socket.parent.is_symlink()
        ):
            parser.error("Socket directory must be private and owned by this user")
        if socket.exists() or socket.is_symlink():
            parser.error(
                "Socket already exists; refusing to replace an existing runtime"
            )
        if len(os.fsencode(socket)) > 103:
            parser.error(
                "Socket path is too long; choose a shorter private state directory"
            )
        children = []

        def stop(signum, frame):
            raise KeyboardInterrupt

        previous = {
            sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP)
        }
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: None)
        try:
            # Keep diagnostics local to this invocation, away from TUI rendering.
            with open(Path(temporary) / "runtime.log", "wb") as log:
                server = subprocess.Popen(
                    [args.codex, "app-server", *overrides, "--listen", "unix://" + str(socket)],
                    stdin=subprocess.PIPE,
                    stdout=log,
                    stderr=log,
                )
                children.append(server)
                deadline = time.monotonic() + 15
                while not socket.exists():
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(
                            "Codex runtime did not start. Check Codex installation and configuration."
                        )
                    time.sleep(0.05)
                if not stat.S_ISSOCK(socket.stat().st_mode):
                    raise RuntimeError("Codex did not create a Unix control socket")
                tui = subprocess.Popen(
                    [args.codex, *extra, "--remote", "unix://" + str(socket)]
                )
                children.append(tui)
                while tui.poll() is None:
                    if server.poll() is not None:
                        raise RuntimeError(
                            "Codex runtime exited; closing its disconnected terminal client."
                        )
                    time.sleep(0.1)
                raise SystemExit(tui.returncode)
        except KeyboardInterrupt:
            raise SystemExit(130) from None
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                if child.stdin:
                    child.stdin.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if socket.exists() and stat.S_ISSOCK(socket.stat().st_mode):
                socket.unlink()


if __name__ == "__main__":
    main()
