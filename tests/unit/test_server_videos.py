"""Required S3 staging contracts; fake S3 is never reported as live access."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from server_videos import StageError, load_config, main, stage

META={'codec':'h264','format':'mov,mp4','duration_s':2.0,'duration_basis':'video_stream',
    'video_duration_s':2.0,'audio_present':True,'time_base':'1/15360','width':640,'height':360,
    'rotation_deg':None,'sample_aspect_ratio':'1:1'}


class FakeS3:
    def __init__(self, payload=b'known video bytes'):
        self.payload=payload;self.etag='"opaque-multipart-etag-2"';self.version='v1'
        self.head_calls=[];self.get_calls=[];self.change=None;self.body=None
    def head_object(self,**values):
        self.head_calls.append(values)
        return {'ContentLength':len(self.payload),'ETag':self.etag,'VersionId':self.version}
    def get_object(self,**values):
        self.get_calls.append(values);self.body=io.BytesIO(self.payload)
        response={'ContentLength':len(self.payload),'ETag':self.etag,'VersionId':self.version,'Body':self.body}
        if self.change:response.update(self.change)
        return response


class ServerVideos(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.config=self.root/'videos.json';self.runtime=self.root/'runtime'
    def tearDown(self):self.temp.cleanup()
    def configure(self,**updates):
        value={'schema_version':1,'videos':[{'id':'first','name':'First example','uri':'s3://sample-bucket/example%20one.mp4'}],
            'disk_reserve_bytes':0}
        value.update(updates);self.config.write_text(json.dumps(value))
    def assert_no_published_video(self):
        root=self.runtime/'server-videos'
        self.assertFalse(list(root.glob('.partial-*')))
        self.assertFalse(list((root/'manifests').glob('*.json')))
        self.assertFalse([path for path in root.iterdir() if path.is_file()])

    def test_missing_config_blocks_without_s3_or_storage_io(self):
        with patch('server_videos.s3_client') as client:
            with self.assertRaises(StageError):stage(None,self.runtime)
            client.assert_not_called()
        self.assertFalse(self.runtime.exists())
        with patch.dict('os.environ',{},clear=True),patch('server_videos.s3_client') as client:
            report=self.root/'blocked.json'
            self.assertEqual(main(['--runtime',str(self.runtime),'--report',str(report)]),1)
            client.assert_not_called()
            summary=json.loads(report.read_text())
            self.assertFalse(summary['passed']);self.assertIn('elapsed_s',summary)
            self.assertRegex(summary['run_id'],r'^[0-9a-f]{32}$')

    def test_valid_bytes_are_hashed_and_immutable_cache_is_verified(self):
        self.configure();client=FakeS3()
        with patch('server_videos.probe_video',return_value=META):
            first=stage(self.config,self.runtime,client)[0]
            second=stage(self.config,self.runtime,client)[0]
        self.assertEqual(first['sha256'],hashlib.sha256(client.payload).hexdigest())
        self.assertEqual(Path(first['path']).read_bytes(),client.payload)
        self.assertEqual(first['source_kind'],'server_video');self.assertFalse(first['fixture']);self.assertFalse(first['live_sensor'])
        self.assertEqual(first['remote']['version_id'],'v1');self.assertIsNone(first['metadata']['rotation_deg'])
        self.assertFalse(first['cached']);self.assertTrue(second['cached'])
        self.assertEqual(len(client.head_calls),2);self.assertEqual(len(client.get_calls),1)
        self.assertEqual(client.get_calls[0],{'Bucket':'sample-bucket','Key':'example one.mp4','VersionId':'v1','IfMatch':client.etag})
        self.assertTrue(client.body.closed)
        with patch('server_videos.probe_video',return_value=META):
            Path(first['path']).write_bytes(b'altered video bytes')
            with self.assertRaises(StageError):stage(self.config,self.runtime,client)
        self.assertEqual(len(client.get_calls),1)

    def test_size_hash_or_object_change_never_publishes_partial_media(self):
        cases=('oversize','hash','etag','version','requested_version','length','truncated','unreadable')
        for case in cases:
            with self.subTest(case=case),tempfile.TemporaryDirectory() as folder:
                self.runtime=Path(folder)/'runtime';client=FakeS3();self.configure()
                if case=='oversize':self.configure(max_file_bytes=2)
                if case=='hash':
                    self.configure(videos=[{'id':'first','name':'First','uri':'s3://sample-bucket/video.mp4','expected_sha256':'0'*64}])
                if case=='etag':client.change={'ETag':'"changed"'}
                if case=='version':client.change={'VersionId':'changed'}
                if case=='requested_version':
                    self.configure(videos=[{'id':'first','name':'First','uri':'s3://sample-bucket/video.mp4','version_id':'different-version'}])
                if case=='length':client.change={'ContentLength':len(client.payload)+1}
                if case=='truncated':client.change={'Body':io.BytesIO(b'cut')}
                with patch('server_videos.probe_video',side_effect=StageError('Unreadable video') if case=='unreadable' else None,return_value=META):
                    with self.assertRaises(StageError):stage(self.config,self.runtime,client)
                self.assert_no_published_video()
                if case in ('oversize','requested_version'):self.assertFalse(client.get_calls)

    def test_strict_uri_config_and_disk_bounds(self):
        for uri in ('s3://user:secret@sample-bucket/video.mp4','s3://sample-bucket/video.mp4?token=secret',
            's3://sample-bucket/video.mp4#fragment','s3://sample-bucket/video.mp4?','https://sample-bucket/video.mp4'):
            with self.subTest(uri=uri):
                self.configure(videos=[{'id':'first','name':'First','uri':uri}])
                with self.assertRaises(StageError):load_config(self.config)
        for values in ({'max_file_bytes':True},{'max_file_bytes':0},{'disk_reserve_bytes':-1},{'schema_version':True}):
            self.configure(**values)
            with self.assertRaises(StageError):load_config(self.config)
        self.configure()
        with patch('server_videos.shutil.disk_usage',return_value=type('Disk',(),{'free':0})()),patch('server_videos.probe_video',return_value=META):
            with self.assertRaises(StageError):stage(self.config,self.runtime,FakeS3())
        self.assert_no_published_video()

    def test_config_relative_and_absolute_local_files_skip_s3(self):
        original=self.root/'example.mp4';original.write_bytes(b'explicit local video bytes')
        for uri in ('file:example.mp4',original.as_uri()):
            with self.subTest(uri=uri),tempfile.TemporaryDirectory() as folder:
                self.runtime=Path(folder)/'runtime'
                self.configure(videos=[{'id':'local','name':'Local server video','uri':uri}])
                with patch('server_videos.s3_client') as client,patch('server_videos.probe_video',return_value=META):
                    first=stage(self.config,self.runtime)[0];second=stage(self.config,self.runtime)[0]
                    client.assert_not_called()
                self.assertEqual(first['remote']['kind'],'local_file')
                self.assertEqual(Path(first['path']).read_bytes(),original.read_bytes())
                self.assertTrue(second['cached']);self.assertTrue(first['metadata']['audio_present'])

    def test_cancellation_removes_partial_file_and_prevents_ready_manifest(self):
        self.configure();client=FakeS3(payload=b'video'*300000)
        canceled=False
        class CancelingBody(io.BytesIO):
            def read(self,size):
                nonlocal canceled
                data=super().read(size);canceled=True;return data
        client.change={'Body':CancelingBody(client.payload)}
        with patch('server_videos.probe_video',return_value=META):
            with self.assertRaisesRegex(StageError,'canceled'):
                stage(self.config,self.runtime,client,cancelled=lambda:canceled)
        self.assert_no_published_video()
        self.assertTrue(client.change['Body'].closed)
