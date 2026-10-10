"""Basic checks for the operator button and cancelled startup."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from http_api import web_api
from studio import App, Config


class VideoButton(unittest.TestCase):
    def test_operator_only_and_no_browser_supplied_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);secret=root/'token';secret.write_text('x'*32)
            app=App(Config(root,'https://example.test',operator_auth='token',operator_token_file=secret))
            try:
                with TestClient(web_api(app,manage_lifecycle=False),client=('203.0.113.1',1)) as client:
                    self.assertEqual(client.post('/api/examples/start',json={}).status_code,403)
                    headers={'Authorization':'Bearer '+'x'*32}
                    self.assertEqual(client.post('/api/examples/start',json={'path':'/etc/passwd'},headers=headers).status_code,409)
                    self.assertEqual(client.post('/api/examples/start',json={},headers=headers).status_code,409)
                    self.assertFalse(app.examples.status()['configured'])
                    self.assertEqual(client.post('/api/examples/stop',json={},headers=headers).json()['state'],'stopped')
            finally:app.close()

    def test_stop_during_loading_never_starts_media(self):
        with tempfile.TemporaryDirectory() as folder:
            reference=Path(__file__).resolve().parents[2]/'config/server-videos.reference.json'
            app=App(Config(Path(folder),'http://localhost',server_videos_config=reference))
            entered=threading.Event();release=threading.Event()
            def loading(*args,**kwargs):
                entered.set();release.wait(2);return []
            try:
                with patch('example_playback.stage',side_effect=loading),patch('example_playback.subprocess.Popen') as launch:
                    self.assertEqual(app.examples.start()['state'],'starting')
                    self.assertTrue(entered.wait(1))
                    self.assertEqual(app.examples.stop()['state'],'stopping')
                    release.set();app.examples.thread.join(2)
                    self.assertEqual(app.examples.status()['state'],'stopped')
                    launch.assert_not_called()
                    self.assertFalse(app.control.program_started)
            finally:release.set();app.close()
