"""Coordinator-owned VAST processing for registered server videos."""
from __future__ import annotations

import copy
import json
import queue
import threading
import time

from vss_client import VssClient, VssFailure, binding_key, configuration


class VideoAnalysis:
    def __init__(self, app):
        self.app=app
        self.lock=threading.RLock()
        self.jobs=queue.Queue(maxsize=2)
        self.current={'state':'idle','reason':'','summary':'','origin':None,
            'live_roles_verified':False,'yolo_sidecar_received':False}
        self.active=set()
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.started=False

    def status(self):
        with self.lock:return copy.deepcopy(self.current)

    def submit(self, video):
        try:values=configuration()
        except VssFailure as error:
            with self.lock:self.current.update(state='unavailable',reason=str(error),failure=error.public())
            return
        key=binding_key(values,video)
        with self.lock:
            if key in self.active:return
            self.active.add(key)
            try:self.jobs.put_nowait((key,copy.deepcopy(video),values))
            except queue.Full:
                self.active.remove(key)
                self.current.update(state='busy',reason='Video analysis queue is full')
                return
            if not self.started:
                self.started=True;self.thread.start()

    def close(self):
        self.stop.set()
        if self.started:self.thread.join(timeout=20)

    def _saved(self,key):
        with self.app.foundation.lock:
            row=self.app.foundation.db.execute(
                "SELECT body FROM records WHERE kind='video_analysis' AND id=? ORDER BY revision DESC LIMIT 1",(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self,key,record):
        if self.stop.is_set() or self.app.stop.is_set():raise VssFailure('VAST work cancelled')
        with self.app.foundation.transaction():
            row=self.app.foundation.db.execute(
                "SELECT MAX(revision) FROM records WHERE kind='video_analysis' AND id=?",(key,)).fetchone()
            revision=(row[0] or 0)+1
            self.app.foundation._put('video_analysis',key,revision,record)

    def _state(self,state,reason='',**fields):
        with self.lock:self.current.update(state=state,reason=reason,**fields)

    def _ready(self,record,cached=False):
        result=record['result']
        self._state('ready',summary=result['summary'],origin='provider',
            video_id=record['video_id'],sha256=record['sha256'],cached=cached,
            segment_count=result['segment_count'],model=result.get('model'),
            detection_segments=len(result['detections']),
            detection_counts=[item for segment in result['detections'] for item in segment['counts']][:512],
            yolo_sidecar_received=result['yolo_sidecar_received'],
            timing_verified=False,tracking_verified=False)

    def _run(self):
        while not self.stop.is_set() and not self.app.stop.is_set():
            try:key,video,values=self.jobs.get(timeout=.2)
            except queue.Empty:continue
            try:self._process(key,video,values)
            except VssFailure as error:self._state('failed',str(error),failure=error.public())
            except Exception:self._state('failed','VAST processing failed; playback remains available')
            finally:
                with self.lock:self.active.discard(key)
                self.jobs.task_done()

    def _process(self,key,video,values):
        deadline=time.monotonic()+300
        def cancelled():return self.stop.is_set() or self.app.stop.is_set() or time.monotonic()>=deadline
        record=self._saved(key) or {'video_id':video['id'],'sha256':video['sha256'],
            'source_kind':'server_video','state':'queued','fixture':False}
        if record['state'] in ('submitting','submission_unknown'):
            self._state('submission_unknown','Previous upload has an uncertain outcome; automatic duplicate upload is blocked')
            return
        client=VssClient(values,cancelled=cancelled)
        try:
            self._state('verifying','Verifying the configured VAST tenant',summary='',origin=None,yolo_sidecar_received=False,failure=None)
            client.verify()
            if record['state']=='ready' and record.get('configuration_sha256')==client.config_sha256:
                client.verify_original(video,record['receipt'])
                self._ready(record,cached=True);return
            record['configuration_sha256']=client.config_sha256
            record['provider_version']=client.version
            if 'receipt' not in record:
                client.prepare_upload(video)
                record['state']='submitting';self._save(key,record)
                self._state('uploading','Uploading the registered video privately')
                try:record['receipt']=client.upload(video)
                except Exception:
                    record['state']='submission_unknown';self._save(key,record)
                    raise VssFailure('Upload outcome is uncertain; no automatic retry will create a duplicate') from None
                record['receipt']['sha256']=video['sha256']
                record['state']='submitted';self._save(key,record)
            self._state('verifying_original','Checking the stored original bytes')
            record['original']=client.verify_original(video,record['receipt'])
            record['state']='indexing';self._save(key,record)
            self._state('indexing','VAST is processing the video')
            while not cancelled():
                indexed=client.indexed_parent(record['receipt'])
                if indexed:
                    record['indexed']=indexed;break
                self.stop.wait(2)
            else:
                record['state']='indexing_delayed';self._save(key,record)
                self._state('indexing_delayed','VAST indexing did not complete within the archive deadline; submission is preserved')
                return
            self._state('inspecting','Reading video reasoning and detection evidence')
            result=client.inspect(record['receipt'])
            # Recheck the original after asynchronous processing. A matching
            # filename or an indexed card is insufficient source proof.
            client.verify_original(video,record['receipt'])
            if cancelled():return
            record.update(state='ready',result=result,requests=client.calls,
                elapsed_seconds=round(300-(deadline-time.monotonic()),6));self._save(key,record)
            self._ready(record)
        finally:client.close()
