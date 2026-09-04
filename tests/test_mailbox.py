import json
import hashlib
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_claude_local_relay import relay


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        relay.initialize(self.state)
        self.environment = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.state / '.claude')})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_replies_survive_reopen_and_duplicate_delivery(self):
        frame = {'type': 'user', 'msg_id': 'reply-1', 'message': {
            'content': '<cross-session-message from="uds:/test.sock" from-name="Fable">\n'
            'PROJECT_RELAY {"thread":"game", "reply_to":"request-1"}\n'
            'Confirmed.\n</cross-session-message>'}}
        relay.receive_frame(self.state, frame, 123)
        relay.receive_frame(self.state, frame, 123)
        rows = relay.read_messages(self.state, thread='game')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reply_to'], 'request-1')
        self.assertEqual(rows[0]['peer_pid'], 123)
        self.assertTrue(rows[0]['body'].endswith('Confirmed.'))
        self.assertEqual(relay.read_messages(self.state, after=rows[0]['seq']), [])

    def test_claude_can_initiate_without_pending_request(self):
        relay.receive_frame(self.state, {'type': 'user', 'msg_id': 'new-question',
            'message': {'content': 'PROJECT_RELAY {"thread":"claude-initiated"}\nCan you review my plan?'}}, 123)
        rows = relay.read_messages(self.state, thread='claude-initiated')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['direction'], 'in')
        self.assertIsNone(rows[0]['reply_to'])
        self.assertEqual(rows[0]['status'], 'received')

    def test_receipts_distinguish_held_denied_and_delivered(self):
        with relay.connect_db(self.state) as db:
            relay.record(db, message_id='request-1', direction='out', body='Question',
                         status='sending', thread='game')
        for status in ('held', 'denied', 'delivered'):
            relay.receive_frame(self.state, {'type': 'control', 'action': 'peer_message_status',
                'orig_msg_id': 'request-1', 'status': status}, 123)
            rows = relay.read_messages(self.state)
            self.assertEqual(rows[0]['status'], status)
            self.assertEqual(rows[-1]['direction'], 'receipt')

    def test_auth_and_malformed_frames_are_not_stored(self):
        for frame in ({'type': 'auth', 'token': 'secret'}, [],
                      {'type': 'user', 'message': {'content': []}},
                      {'type': 'user', 'msg_id': {}, 'message': {'content': 'bad'}},
                      {'type': 'control', 'action': 'peer_message_status', 'orig_msg_id': [], 'status': 'held'},
                      {'type': 'control', 'action': 'rename', 'name': 'x'}):
            relay.receive_frame(self.state, frame, 123)
        self.assertEqual(relay.read_messages(self.state), [])

    def test_reject_shared_state_and_symlink(self):
        shared = self.state / 'shared'
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'private directory'):
            relay.initialize(shared)
        link = self.state / 'link'
        link.symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'private directory'):
            relay.initialize(link)

    def test_stale_session_is_not_discovered(self):
        registry = self.state / '.claude/sessions'
        registry.mkdir(parents=True)
        record = {'pid': os.getpid(), 'procStart': 'stale', 'peerProtocol': 1,
                  'messagingSocketPath': '/tmp/test.sock', 'sessionId': 'old'}
        (registry / f'{os.getpid()}.json').write_text(json.dumps(record))
        self.assertEqual(relay.sessions(), [])
        with self.assertRaisesRegex(ValueError, 'not uniquely live'):
            relay.resolve_session('old')

    def test_real_socket_transport_and_session_identity(self):
        path = self.state / 'peer.sock'
        target = {'pid': os.getpid(), 'procStart': relay.proc_start(os.getpid()),
                  'messagingSocketPath': str(path)}
        keys = self.state / '.claude/sessions'
        keys.mkdir(parents=True)
        key = keys / f"{os.getpid()}.{hashlib.sha256(str(path).encode()).hexdigest()}.key"
        key.write_text(json.dumps({'peerToken': 'a' * 32, 'procStart': target['procStart']}))
        key.chmod(0o600)
        received = []
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            server.listen(1)
            server.settimeout(3)
            def receive():
                with server.accept()[0] as conn:
                    conn.settimeout(3)
                    received.extend(json.loads(line) for line in conn.makefile('rb'))
            worker = threading.Thread(target=receive)
            worker.start()
            with patch.object(Path, 'home', return_value=self.state):
                relay.send_wire(target, 'uds:/run/user/1000/cc-socks/123.sock',
                    {'id': 'request-1', 'thread': 'game', 'reply_to': None, 'body': 'Question'})
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(received[0]['type'], 'auth')
        self.assertEqual(received[1]['priority'], 'next')
        self.assertIn('from-name="codex-reviewer"', received[1]['message']['content'])
        self.assertEqual(received[1]['msg_id'], 'request-1')

    def test_export_keeps_messages_across_pages(self):
        (self.state / 'config.json').write_text(json.dumps({'project': '/game', 'session_id': 'abc'}))
        with relay.connect_db(self.state) as db:
            for i in range(205):
                relay.record(db, message_id=str(i), direction='out', body=f'message {i}',
                             status='sent', thread='game')
        document = relay.export_markdown(self.state)
        self.assertIn('message 0\n', document)
        self.assertIn('message 204\n', document)


if __name__ == '__main__':
    unittest.main()
