import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from fake_codex import native_delivery

from codex_claude_local_relay.connections import Connection
from codex_claude_local_relay import relay


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
        self.assertEqual(first['address'], second['address'])
        self.assertIn(reply['id'], [r['id'] for r in self.cli('read')['messages']])
        request2 = self.cli('send', '--thread', 'plan', 'After restart')
        self.await_reply(request2['id'])

    def test_idle_pair_recovers_without_messages_and_claude_can_speak_first(self):
        codex = 'codex:10000000-0000-4000-8000-000000000001'
        claude = 'claude:' + self.target['sessionId']
        with patch.dict(os.environ, self.env):
            connection = Connection.create(self.root / 'pair', [
                {'id': codex, 'cwd': str(self.root)}, {'id': claude, 'cwd': str(self.root)}])
            self.addCleanup(connection.disconnect)
            leg = connection.leg(claude)
            first = relay.daemon_status(leg)
            # A completed pair has no outgoing work. No notice is sent to any model.
            with relay.connect_db(connection.state) as db:
                db.execute("UPDATE pair_messages SET status='sent'")
            before = connection.read()
            relay.stop_daemon(leg)
            reopened = Connection(connection.state)
            reopened.recover()
            with native_delivery() as native:
                for _ in range(5):
                    reopened.maintain()
                    reopened.tick(lambda _: None, '/synthetic/codex')
                native.assert_not_called()
            self.assertEqual(reopened.read(), before)
            self.assertEqual(relay.read_messages(leg), [])
            self.assertEqual(relay.daemon_status(leg)['address'], first['address'])
            # Only the fixture is prompted here: its independent send uses the old route.
            (self.root / 'initiate.json').write_text(json.dumps({
                'address': first['address'], 'thread': connection.config['id']}))
            with native_delivery() as native:
                for _ in range(100):
                    reopened.tick(lambda _: None, '/synthetic/codex')
                    if len(reopened.read()) > len(before):
                        break
                    time.sleep(.03)
                self.assertEqual(native.call_count, 1)
                self.assertEqual(native.call_args.args[0], codex[6:])

    def test_missing_socket_is_unhealthy_and_repaired_without_sending(self):
        first = self.cli('connect', '--session', self.target['sessionId'])
        Path(first['address'][4:]).unlink()
        status = self.cli('status')
        self.assertTrue(status['process_running'])
        self.assertFalse(status['running'])
        repaired = self.cli('start')
        self.assertEqual(repaired['address'], first['address'])
        self.assertNotEqual(repaired['pid'], first['pid'])
        self.assertTrue(repaired['running'])
        self.assertEqual(self.cli('read')['messages'], [])

    def test_crashed_listener_reuses_recorded_socket_and_rejects_replacements(self):
        first = self.cli('connect', '--session', self.target['sessionId'])
        os.kill(first['pid'], 9)
        time.sleep(.1)
        repaired = self.cli('start')
        self.assertEqual(repaired['address'], first['address'])
        self.assertTrue(repaired['running'])
        self.cli('stop')
        path = Path(first['address'][4:])
        path.write_text('Foreign replacement')
        result = self.cli('start', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), 'Foreign replacement')

    def test_legacy_address_can_be_pinned_without_a_notice(self):
        self.cli('init', '--session', self.target['sessionId'])
        old = 'uds:' + str(self.root / '654321.sock')
        with patch.dict(os.environ, self.env):
            relay.pin_address(self.state, old)
        first = self.cli('start')
        self.cli('stop')
        self.assertEqual(self.cli('start')['address'], old)
        self.assertEqual(first['address'], old)
        self.assertEqual(self.cli('read')['messages'], [])

    def test_no_retargeting_of_existing_mailbox(self):
        self.cli('connect', '--session', self.target['sessionId'])
        self.cli('stop')
        other = self.cli('--project', str(self.root.parent), 'init', '--session', self.target['sessionId'], check=False)
        self.assertNotEqual(other.returncode, 0)
        self.assertIn('different project/session', other.stderr)

    def test_pair_forwards_only_enrolled_claude_to_exact_codex(self):
        codex = 'codex:10000000-0000-4000-8000-000000000001'
        claude = 'claude:' + self.target['sessionId']
        with patch.dict(os.environ, self.env):
            connection = Connection.create(self.root / 'pair', [
                {'id': codex, 'cwd': str(self.root)}, {'id': claude, 'cwd': str(self.root)}])
            self.addCleanup(connection.disconnect)
            with native_delivery() as native:
                for _ in range(150):
                    connection.tick(lambda key: self.assertIn(key, (codex, claude)), '/synthetic/codex')
                    rows = connection.read()
                    if any(r['sender'] == claude and r['status'] == 'accepted_native' for r in rows):
                        break
                    time.sleep(0.03)
                else:
                    self.fail('Claude reply did not reach the exact Codex queue: ' + repr(rows))
                self.assertTrue(all(call.args[0] == codex[6:] for call in native.call_args_list))
                count = len(rows)
                # Same authenticated Claude, but an address/envelope copied from
                # another connection: never forward it through this pair.
                with relay.connect_db(connection.leg(claude)) as db:
                    relay.record(db, message_id='wrong-connection', direction='in', body='Foreign thread',
                                 status='received', thread='different-connection')
                    relay.record(db, message_id='wrong-reply', direction='in', body='Foreign reply',
                                 status='received', thread=connection.config['id'], reply_to='foreign-message')
                connection.tick(lambda _: None, '/synthetic/codex')
                self.assertEqual(len(connection.read()), count)


if __name__ == '__main__':
    unittest.main()
