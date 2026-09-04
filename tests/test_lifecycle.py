import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'mailbox'
        self.env = {**os.environ, 'CLAUDE_CONFIG_DIR': str(self.root)}
        self.peer = subprocess.Popen([sys.executable, str(Path(__file__).with_name('fake_peer.py')), str(self.root)],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.close_peer)
        for _ in range(100):
            if (self.root / 'ready.json').exists():
                self.target = json.loads((self.root / 'ready.json').read_text())
                break
            if self.peer.poll() is not None:
                self.fail(self.peer.stderr.read().decode())
            time.sleep(0.02)
        else:
            self.fail('Fixture startup timeout')
        self.addCleanup(lambda: self.cli('stop'))

    def close_peer(self):
        if self.peer.poll() is None:
            self.peer.terminate()
        self.peer.wait(timeout=5)
        self.peer.stderr.close()

    def cli(self, *args, check=True):
        result = subprocess.run([sys.executable, '-m', 'codex_claude_local_relay',
            '--state', str(self.state), '--project', str(self.root), *args],
            env=self.env, capture_output=True, text=True, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        return result

    def await_reply(self, message_id):
        for _ in range(100):
            rows = self.cli('read')['messages']
            replies = [r for r in rows if r['direction'] == 'in' and r['reply_to'] == message_id]
            if replies:
                return replies[0]
            time.sleep(0.03)
        self.fail(f'No reply for {message_id}: {rows}')

    def test_round_trip_restart_and_reject_foreign_process(self):
        first = self.cli('connect', '--session', self.target['sessionId'])
        request = self.cli('send', '--thread', 'plan', 'A question')
        reply = self.await_reply(request['id'])
        self.assertEqual(reply['peer_pid'], self.peer.pid)
        # Claiming the enrolled peer's address cannot override Linux credentials.
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(first['address'][4:])
            client.sendall((json.dumps({'type': 'user', 'msg_id': 'forged',
                'from': 'uds:' + self.target['messagingSocketPath'],
                'message': {'content': 'Not from the enrolled peer'}}) + '\n').encode())
        time.sleep(0.05)
        self.assertNotIn('forged', [r['id'] for r in self.cli('read')['messages']])
        self.cli('stop')
        self.assertFalse(self.cli('status')['running'])
        second = self.cli('start')
        self.assertNotEqual(first['address'], second['address'])
        self.assertIn(reply['id'], [r['id'] for r in self.cli('read')['messages']])
        request2 = self.cli('send', '--thread', 'plan', 'After restart')
        self.await_reply(request2['id'])

    def test_no_retargeting_of_existing_mailbox(self):
        self.cli('connect', '--session', self.target['sessionId'])
        self.cli('stop')
        other = self.cli('--project', str(self.root.parent), 'init', '--session', self.target['sessionId'], check=False)
        self.assertNotEqual(other.returncode, 0)
        self.assertIn('different project/session', other.stderr)


if __name__ == '__main__':
    unittest.main()
