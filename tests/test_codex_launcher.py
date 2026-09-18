import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class LauncherTests(unittest.TestCase):
    def test_native_arguments_lifetime_and_ctrl_c_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix="relay-launch-") as temporary:
            root = Path(temporary)
            native = root / "codex"
            native.write_text(
                "#!"
                + sys.executable
                + "\n"
                + """
import json,os,signal,socket,sys,time
from pathlib import Path
root=Path.cwd()
if sys.argv[1]=='app-server':
    server=socket.socket(socket.AF_UNIX)
    server.bind(sys.argv[-1][len('unix://'):]);server.listen()
    (root/'server.pid').write_text(str(os.getpid()))
    while True:time.sleep(.05)
else:
    (root/'tui.json').write_text(json.dumps(sys.argv[1:]))
    while not (root/'exit').exists():time.sleep(.05)
"""
            )
            native.chmod(0o700)
            sock = root / "control.sock"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "codex_claude_local_relay.runtime",
                    "--codex",
                    str(native),
                    "--socket",
                    str(sock),
                    "--",
                    "resume",
                    "fixture-id",
                    "--add-dir",
                    "/literal path",
                ],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                },
            )
            try:
                deadline = time.monotonic() + 5
                while not (root / "tui.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((root / "tui.json").exists())
                self.assertEqual(
                    json.loads((root / "tui.json").read_text()),
                    [
                        "resume",
                        "fixture-id",
                        "--add-dir",
                        "/literal path",
                        "--remote",
                        "unix://" + str(sock),
                    ],
                )
                process.send_signal(signal.SIGINT)
                time.sleep(0.05)
                self.assertIsNone(
                    process.poll(), "Wrapper must leave Ctrl-C handling to the TUI"
                )
                pid = int((root / "server.pid").read_text())
                (root / "exit").touch()
                _stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertFalse(sock.exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
