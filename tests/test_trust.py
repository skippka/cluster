import contextlib
import io
import json
from pathlib import Path
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from cluster.controller import ClusterController
from cluster.trust import NodeSession, TrustStore, parse_invitation
from cluster.protocol import api_request
from cluster.sharing import DEFAULT_POLICY, allows_job
from cluster.worker import run_worker


class InvitationTests(unittest.TestCase):
    def test_single_use_even_when_redeemed_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TrustStore(directory)
            secret = store.invite()
            results = []
            def attempt():
                try:
                    results.append(store.enroll(secret, 'Laptop'))
                except ValueError:
                    pass
            threads = [threading.Thread(target=attempt) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(results), 1)
            self.assertEqual(store.authenticate(results[0]['token']), results[0]['node_id'])
            self.assertNotIn(secret, str(store.db.execute('SELECT * FROM invites').fetchall()))
            self.assertTrue(store.revoke(results[0]['node_id']))
            self.assertIsNone(store.authenticate(results[0]['token']))
            store.close()

    def test_expiry_and_revocation_survive_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [100]
            store = TrustStore(directory, clock=lambda: now[0])
            secret = store.invite(seconds=10)
            now[0] = 111
            with self.assertRaises(ValueError):
                store.enroll(secret, 'Laptop')
            device = store.enroll(store.invite(), 'Laptop')
            store.revoke(device['node_id'])
            store.close()
            store = TrustStore(directory)
            self.assertIsNone(store.authenticate(device['token']))
            store.close()

    def test_owner_must_explicitly_enable_shell_and_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sharing.json'
            path.write_text(json.dumps(DEFAULT_POLICY))
            self.assertTrue(allows_job(directory, 'corpus'))
            self.assertFalse(allows_job(directory, 'command'))
            self.assertFalse(allows_job(directory, 'docker'))
            path.write_text(json.dumps({**DEFAULT_POLICY, 'allowed_jobs': ['command']}))
            self.assertTrue(allows_job(directory, 'command'))
            path.write_text('{bad')
            self.assertFalse(allows_job(directory, 'command'))


class SecureApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = ClusterController(0, '', self.root / 'controller', secure=True)
        self.app.start()

    def tearDown(self):
        self.app.stop()
        self.temp.cleanup()

    def pair(self, name):
        invite = self.app.issue_invitation('127.0.0.1')
        return NodeSession.pair(invite, name, self.root / name)

    def test_real_tls_pair_reconnect_no_shared_key_and_revocation(self):
        first, second = self.pair('First'), self.pair('Second')
        self.assertNotEqual(first.token, second.token)
        self.assertTrue(first.request('GET', '/health')['ok'])
        saved = NodeSession.load(self.root / 'First')
        self.assertTrue(saved.request('GET', '/health')['ok'])
        self.assertTrue(self.app.revoke_device(first.node_id))
        with self.assertRaises(urllib.error.HTTPError) as denied:
            first.request('GET', '/health')
        self.assertEqual(denied.exception.code, 401)
        self.assertTrue(second.request('GET', '/health')['ok'])

    def test_pin_mismatch_stops_before_invite_or_token_is_sent(self):
        invite = self.app.issue_invitation('127.0.0.1')
        server, fingerprint, code = parse_invitation(invite)
        wrong = NodeSession(server, '0' * 64)
        with self.assertRaises(ssl.SSLError):
            wrong.request('POST', '/pair', {'invite': code, 'name': 'Attacker'})
        self.assertEqual(self.app.pair_attempts, {})
        paired = NodeSession.pair(invite, 'Correct', self.root / 'Correct')
        self.assertTrue(paired.request('GET', '/health')['ok'])

    def test_device_cannot_fetch_or_report_as_another_device(self):
        first, second = self.pair('First'), self.pair('Second')
        with self.assertRaises(urllib.error.HTTPError) as denied:
            first.request('GET', '/jobs/next/' + second.node_id)
        self.assertEqual(denied.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            first.request('POST', '/nodes/heartbeat', {'node_id': second.node_id})
        self.assertEqual(denied.exception.code, 403)

    def test_pairing_code_cannot_be_used_twice_over_https(self):
        invite = self.app.issue_invitation('127.0.0.1')
        NodeSession.pair(invite, 'First', self.root / 'First')
        with self.assertRaises(urllib.error.HTTPError) as denied:
            NodeSession.pair(invite, 'Second', self.root / 'Second')
        self.assertEqual(denied.exception.code, 400)

    def test_real_paired_worker_processes_batch(self):
        session = self.pair('Worker')
        worker_dir = self.root / 'Worker'
        (worker_dir / 'sharing.json').write_text(json.dumps({**DEFAULT_POLICY, 'reserve_ram_gb': 0, 'max_host_cpu': 100, 'minimum_battery': 0}))
        corpus = self.root / 'input'
        corpus.mkdir()
        (corpus / 'a.txt').write_text('one two three')
        batch, _, _ = self.app.swarm.submit(corpus)
        stop = threading.Event()
        thread = threading.Thread(target=run_worker, args=(session.server, session, session.name, worker_dir, stop, 0.05, False))
        try:
            with contextlib.redirect_stdout(io.StringIO()), patch('cluster.worker.docker_available', return_value=False):
                thread.start()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if self.app.swarm.batches()[0]['counts'].get('done') == 1:
                        break
                    time.sleep(0.05)
                stop.set()
                thread.join(timeout=12)
            _, report = self.app.swarm.export(batch)
            self.assertTrue(report['complete'])
            self.assertEqual(report['words'], 3)
            self.assertIsNone(self.app.create_job('command', 1, .25, .1, 'echo not-allowed'))
        finally:
            stop.set()
            if thread.ident:
                thread.join(timeout=12)


if __name__ == '__main__':
    unittest.main()
