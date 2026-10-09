"""Public QR origins and media candidates share explicit deployment settings."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from studio import App,Config,public_origin
from http_api import web_api


class PublicConfig(unittest.TestCase):
    def test_blank_optional_deployment_variables_do_not_become_directory_paths(self):
        import studio
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict('os.environ',{'BREADCAST_PUBLIC_URL':'https://203.0.113.12:8080',
                 'BREADCAST_BIND':'127.0.0.1','BREADCAST_OPERATOR_AUTH':'proxy','BREADCAST_FOUNDATION_CONFIG':'',
                 'BREADCAST_ICE_SERVERS':'','BREADCAST_TLS_CERT':'','BREADCAST_TLS_KEY':''},clear=True), \
             patch('sys.argv',['studio.py','serve','--runtime',directory]), \
             patch.object(studio,'App') as app,patch.object(studio.uvicorn,'Server'), \
             patch.object(studio.threading.Thread,'start'):
            studio.main()
            cfg=app.call_args.args[0]
            self.assertIsNone(cfg.foundation_config)
            self.assertEqual(cfg.ice_servers,())
            self.assertIsNone(cfg.certificate)
            self.assertIsNone(cfg.private_key)
            self.assertEqual(cfg.ice_hosts,('203.0.113.12',))

    def test_remote_http_demo_origin_drives_links_qr_and_media_host(self):
        with tempfile.TemporaryDirectory() as directory:
            app=App(Config(Path(directory),'http://203.0.113.12:9080/',webrtc_port=9190))
            try:
                self.assertEqual(app.cfg.public_url,'http://203.0.113.12:9080')
                self.assertEqual(app.cfg.ice_hosts,('203.0.113.12',))
                with TestClient(web_api(app,manage_lifecycle=False),client=("127.0.0.1",12345)) as client:
                    expected=app.join_url()
                    self.assertTrue(expected.startswith('http://203.0.113.12:9080/join?code='))
                    self.assertEqual(client.get('/api/viewer').json()['join_url'],expected)
                    self.assertEqual(client.get('/api/status').json()['join_url'],expected)
                    from unittest.mock import patch
                    import qrcode
                    with patch.object(qrcode.QRCode,'add_data',autospec=True) as add:
                        self.assertEqual(client.get('/api/qr').status_code,200)
                        self.assertEqual(add.call_args.args[1],expected)
                    rotated=client.post('/api/join/rotate',json={}).json()['join_url']
                    self.assertNotEqual(rotated,expected)
                    self.assertTrue(rotated.startswith(app.cfg.public_url+'/join?code='))
                gateway=app.media_config()
                self.assertEqual(gateway['webrtcAdditionalHosts'],['203.0.113.12'])
                self.assertEqual(gateway['webrtcLocalUDPAddress'],':9190')
                self.assertEqual(gateway['webrtcLocalTCPAddress'],':9190')
            finally:app.close()

    def test_https_tunnel_and_direct_media_use_distinct_configured_hosts(self):
        cfg=Config(Path('/tmp'),'https://demo.trycloudflare.com',ice_hosts=('203.0.113.12',))
        self.assertEqual(cfg.ice_hosts,('203.0.113.12',))
        self.assertNotIn('trycloudflare.com',cfg.ice_hosts)

    def test_invalid_origin_and_remote_loopback_media_fail_visibly(self):
        for origin in ('','https://host/path','https://user:password@host','https://host?x=1','https://host:invalid','http://host\n'):
            with self.subTest(origin=origin),self.assertRaises(ValueError):public_origin(origin)
        with self.assertRaisesRegex(ValueError,'loopback ICE'):
            Config(Path('/tmp'),'http://203.0.113.12:8080',ice_hosts=('127.0.0.1',))
