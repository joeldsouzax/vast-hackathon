"""Required S01 checks: operator boundary, own leases, and public path routing."""
import io
from pathlib import Path
import tempfile
import unittest
import urllib.response
from unittest.mock import patch

from fastapi.testclient import TestClient
from http_api import web_api
from studio import App, Config


class DeploymentAccess(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.secret = self.root / 'operator-token'
        self.secret.write_text('test-operator-' + 'a' * 32)
        self.app = App(Config(self.root, 'https://example.test', operator_auth='token',
            operator_token_file=self.secret, public_path_prefix='/app'))
        self.client = TestClient(web_api(self.app, manage_lifecycle=False), client=('203.0.113.7', 12000))
        self.headers = {'Authorization': 'Bearer ' + self.secret.read_text()}

    def tearDown(self):
        self.client.close()
        self.app.close()
        self.directory.cleanup()

    def test_operator_routes_deny_anonymous_and_wrong_credentials(self):
        for path in ('/api/status', '/api/actions/missing', '/api/replay-context', '/api/search/missing'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get('/app' + path).status_code, 403)
        for path in ('/api/actions', '/api/setup', '/api/event/end', '/api/join/rotate', '/api/search', '/internal/context', '/unknown'):
            for headers in ({}, {'Authorization': 'Bearer wrong'}):
                with self.subTest(path=path, headers=bool(headers)):
                    self.assertEqual(self.client.post('/app' + path, json={}, headers=headers).status_code, 403)
        self.assertEqual(self.client.get('/app/api/status', headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get('/app/api/viewer').status_code, 200)
        self.assertNotIn('operator_token', self.client.get('/app/api/status', headers=self.headers).text)

    def test_only_own_lease_or_operator_can_release(self):
        with patch.object(self.app.cfg, 'gateway', return_value={}), patch.object(self.app.leases, 'gateway', return_value={}):
            first = self.client.post('/app/api/leases', json={'code': self.app.join_code, 'client': '1'*32}).json()
            second = self.client.post('/app/api/leases', json={'code': self.app.join_code, 'client': '2'*32}).json()
            path = '/app/api/lease/' + second['lease_id'] + '/release'
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
            self.assertEqual(self.client.post(path, json={}, headers={'Authorization': 'Bearer ' + first['token']}).status_code, 403)
            self.assertEqual(len(self.app.leases.rows()), 2)
            self.assertEqual(self.client.post(path, json={}, headers={'Authorization': 'Bearer ' + second['token']}).status_code, 200)
            self.assertEqual(len(self.app.leases.rows()), 1)
            path = '/app/api/lease/' + first['lease_id'] + '/release'
            self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 403)
            path = '/app/api/cameras/' + first['lease_id'] + '/remove'
            self.assertEqual(self.client.post(path, json={}).status_code, 403)
            self.assertEqual(self.client.post(path, json={}, headers=self.headers).status_code, 200)

    def test_prefix_assets_qr_and_media_session_location(self):
        self.assertEqual(self.client.get('/app', follow_redirects=False).headers['location'], '/app/')
        page = self.client.get('/app/operator').text
        self.assertIn('name="breadcast-prefix" content="/app"', page)
        self.assertIn('src="/app/common.js"', page)
        self.assertNotIn('src="/common.js"', page)
        self.assertIn('content="token"', page)
        self.assertTrue(self.app.join_url().startswith('https://example.test/app/join?code='))
        source = 'camera/' + 'a' * 32
        upstream_path = '/' + source + '/whip/' + 'b' * 36
        def response(request, **kwargs):
            self.assertEqual(request.full_url, 'http://127.0.0.1:8889/' + source + '/whip')
            self.assertEqual(request.get_header('Authorization'), 'Bearer phone-token')
            return urllib.response.addinfourl(io.BytesIO(b'sdp'),
                {'Location': upstream_path, 'Content-Type': 'application/sdp'}, request.full_url, 201)
        with patch('http_api.urllib.request.urlopen', side_effect=response):
            result = self.client.post('/app/media/' + source + '/whip', content=b'sdp',
                headers={'Authorization': 'Bearer phone-token', 'Content-Type': 'application/sdp'})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.headers['location'], '/app/media' + upstream_path)
        self.assertEqual(result.content, b'sdp')

    def test_missing_or_invalid_secret_fails_closed(self):
        for file in (None, Path('relative-token'), self.root / 'missing'):
            with self.subTest(file=str(file)), self.assertRaises(ValueError):
                Config(self.root, 'https://example.test', operator_auth='token', operator_token_file=file)
        self.secret.write_text('short')
        with self.assertRaises(ValueError):
            Config(self.root, 'https://example.test', operator_auth='token', operator_token_file=self.secret)

    def test_local_and_proxy_do_not_trust_forwarded_identity(self):
        self.app.cfg.operator_auth = 'local'
        self.assertEqual(self.client.get('/app/api/status', headers={'X-Forwarded-For': '127.0.0.1'}).status_code, 403)
        self.app.cfg.operator_auth = 'proxy'
        self.assertEqual(self.client.get('/app/api/status', headers={'X-Authenticated-User': 'operator'}).status_code, 403)
        with TestClient(web_api(self.app, manage_lifecycle=False), client=('127.0.0.1', 12000)) as local:
            self.assertEqual(local.get('/app/api/status').status_code, 200)
            with patch.object(self.app.cfg, 'gateway', return_value={}), patch.object(self.app.leases, 'gateway', return_value={}):
                lease=self.app.leases.reserve('3'*32)
                self.assertEqual(local.post('/app/api/lease/'+lease['lease_id']+'/release',json={}).status_code,403)


if __name__ == '__main__':
    unittest.main()
