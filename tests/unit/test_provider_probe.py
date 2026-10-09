"""Required verifier checks: no accidental calls, no secrets, bounded transport."""
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from provider_probe import MAX_JSON_BYTES, NoRedirect, ProbeFailure, run_probe


class Reply(io.BytesIO):
    def __init__(self, url, value, *, raw=None):
        super().__init__(raw if raw is not None else json.dumps(value).encode())
        self.url, self.status = url, 200

    def geturl(self):
        return self.url


class ProviderProbe(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.username = 'assigned-team-identity'
        self.password = 'literal $(printf password-private)'
        self.token = 'provider-token-private'

    def tearDown(self):
        self.directory.cleanup()

    def config(self):
        (self.root / 'assigned.config').write_text(
            'INGRESS_URL=https://assigned.test\n'
            'USERNAME=' + self.username + '\n'
            "PASSWORD='" + self.password + "'\n")

    def test_missing_or_ambiguous_config_never_calls_network(self):
        opener = Mock(side_effect=AssertionError('No request is permitted'))
        for directory in (self.root / 'missing', self.root):
            report = run_probe(directory, open_url=opener)
            self.assertEqual(report['gates']['G01'], 'blocked')
            self.assertEqual(report['steps'], [])
        self.config()
        (self.root / 'other.config').write_text('USERNAME=other\n')
        self.assertEqual(run_probe(self.root, open_url=opener)['gates']['G01'], 'blocked')
        opener.assert_not_called()

    def test_verified_identity_and_errors_never_report_credentials(self):
        self.config()
        calls = []
        wrong_identity = False
        def opened(request, *, timeout):
            path = request.full_url.removeprefix('https://assigned.test')
            calls.append(path)
            self.assertLessEqual(timeout, 5)
            if path == '/health':
                return Reply(request.full_url, {'status': 'ok'})
            if path == '/api/v1/auth/login':
                self.assertEqual(json.loads(request.data), {'username': self.username, 'password': self.password})
                return Reply(request.full_url, {'access_token': self.token, 'token_type': 'bearer', 'username': self.username})
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + self.token)
            if path == '/api/v1/auth/me':
                return Reply(request.full_url, {'username': 'foreign-team' if wrong_identity else self.username,
                    'email': self.password})
            self.assertEqual(path, '/api/v1/config')
            return Reply(request.full_url, {'backend_version': 'vss-1.2.3', 'vdb_collection': 'private-index',
                'jwt_secret': self.token, self.password: self.username})
        report = run_probe(self.root, open_url=opened)
        self.assertEqual(report['outcome'], 'verified')
        self.assertEqual(report['provider_version'], 'vss-1.2.3')
        self.assertEqual(report['gates']['G01'], 'verified')
        self.assertTrue(all(report['gates'][f'G{i:02}'] == 'unverified' for i in range(2, 10)))
        self.assertEqual(calls, ['/health', '/api/v1/auth/login', '/api/v1/auth/me', '/api/v1/config'])
        self.assertEqual(len(report['configuration_sha256']), 64)
        self.assertTrue(report['identity_matches'])
        serialized = json.dumps(report)
        for secret in (self.username, self.password, self.token, 'private-index', 'jwt_secret'):
            self.assertNotIn(secret, serialized)
        calls.clear()
        wrong_identity = True
        report = run_probe(self.root, open_url=opened)
        self.assertEqual(report['failure_reason'], 'identity_mismatch')
        self.assertNotIn('/api/v1/config', calls)
        def failed(request, **kwargs):
            raise urllib.error.URLError(self.password + self.token)
        serialized = json.dumps(run_probe(self.root, open_url=failed))
        self.assertNotIn(self.password, serialized)
        self.assertNotIn(self.token, serialized)

    def test_redirect_size_and_elapsed_limits_stop_requests(self):
        self.config()
        handler = NoRedirect()
        with self.assertRaises(ProbeFailure):
            handler.redirect_request(None, None, 302, 'redirect', {}, 'https://foreign.test')
        def redirected(request, **kwargs):
            return Reply('https://foreign.test', {'status': 'ok'})
        self.assertEqual(run_probe(self.root, open_url=redirected)['failure_reason'], 'redirect_rejected')
        def huge(request, **kwargs):
            return Reply(request.full_url, {}, raw=b' ' * (MAX_JSON_BYTES + 1))
        self.assertEqual(run_probe(self.root, open_url=huge)['failure_reason'], 'response_too_large')
        def stalled(request, **kwargs):
            time.sleep(1)
            raise AssertionError('Deadline must interrupt this call')
        with patch('provider_probe.CALL_SECONDS', 0.02), patch('provider_probe.TOTAL_SECONDS', 0.05):
            report = run_probe(self.root, open_url=stalled)
        self.assertEqual(report['failure_reason'], 'timeout')
        self.assertLess(report['elapsed_run_seconds'], 0.5)
        self.assertEqual(len(report['steps']), 1)


if __name__ == '__main__':
    unittest.main()
