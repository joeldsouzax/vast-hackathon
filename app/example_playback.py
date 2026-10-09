"""Operator-started file playback through the existing media controller."""
from __future__ import annotations

import subprocess
import threading
import time
import uuid

from media import stop_process
from server_videos import StageError, load_config, stage


class ExamplePlayback:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.state = 'stopped'
        self.reason = ''
        self.cancel = threading.Event()
        self.thread = None
        self.assets = {}

    def status(self):
        try:
            configured = bool(load_config(self.app.cfg.server_videos_config).videos)
        except StageError:
            configured = False
        with self.lock:
            return {'state': self.state, 'configured': configured, 'reason': self.reason,
                'count': len(self.assets)}

    def start(self):
        with self.lock:
            if self.app.stop.is_set():
                raise PermissionError('This event has ended')
            if self.state in ('starting', 'playing', 'stopping'):
                return self.status()
            load_config(self.app.cfg.server_videos_config)
            self.state = 'starting'
            self.reason = ''
            self.cancel = threading.Event()
            with self.app.control.lock:
                authority = self.app.control.revision
            self.thread = threading.Thread(target=self._run, args=(authority, self.cancel), daemon=True)
            self.thread.start()
            return self.status()

    def stop(self):
        with self.lock:
            if self.state not in ('starting', 'playing', 'stopping'):
                return self.status()
            self.state = 'stopping'
            self.cancel.set()
            return self.status()

    def close(self):
        self.cancel.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=20)

    def asset_for(self, source_path):
        with self.lock:
            return self.assets.get(source_path)

    def _action(self, op, args, authority=None):
        with self.app.control.lock:
            if authority is not None and self.app.control.revision != authority:
                raise StageError('Control changed while the video was starting; press Start video again')
            expected = self.app.control.expected(args)
            record = self.app.control.submit({'id': uuid.uuid4().hex, 'op': op,
                'args': args, 'expected': expected})
            if record['state'] in ('Rejected', 'Failed', 'Expired'):
                raise StageError('The program controller rejected video playback')

    def _run(self, authority, cancel):
        cfg = self.app.cfg
        processes = []
        leases = []
        failure = None
        deadline = time.monotonic() + 120
        def cancelled():
            return cancel.is_set() or self.app.stop.is_set() or time.monotonic() >= deadline
        try:
            videos = stage(cfg.server_videos_config, cfg.runtime, cancelled=cancelled)
            if cancelled():
                return
            for video in videos:
                if cancelled():
                    return
                lease = self.app.leases.reserve(uuid.uuid4().hex)
                leases.append(lease)
                with self.lock:
                    self.assets[lease['source_path']] = video
                # Loop the registered file so Start video can keep both sources
                # available for director mixing without waiting for another Start.
                command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
                    '-re', '-stream_loop', '-1', '-i', video['path'], '-map', '0:v:0', '-map', '0:a:0?',
                    '-filter_threads', '1', '-vf',
                    f'fps={cfg.fps},scale={cfg.width}:{cfg.height}:force_original_aspect_ratio=decrease,'
                    f'pad={cfg.width}:{cfg.height}:(ow-iw)/2:(oh-ih)/2',
                    '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast',
                    '-tune', 'zerolatency', '-profile:v', 'baseline', '-pix_fmt', 'yuv420p',
                    '-bf', '0', '-g', str(cfg.fps), '-c:a', 'libopus', '-ac', '1',
                    '-f', 'rtsp', '-rtsp_transport', 'tcp',
                    cfg.rtsp_url(lease['source_path'], lease['token'])]
                with (cfg.runtime / ('server-video-' + lease['lease_id'] + '.log')).open('ab') as log:
                    processes.append(subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log))
            ready_deadline = time.monotonic() + 30
            while not cancelled():
                if any(p.poll() is not None for p in processes):
                    raise StageError('Video publisher stopped before its playback buffer was ready')
                if all(self.app.get_source(l['slot']) and
                    self.app.get_source(l['slot']).status()['buffer_ready'] for l in leases):
                    break
                if time.monotonic() >= ready_deadline:
                    raise StageError('Video playback buffer did not become ready')
                cancel.wait(.1)
            if cancelled():
                return
            # Only a human button press authorizes Start. Late startup cannot
            # overwrite a human control change made while bytes were loading.
            with self.lock, self.app.control.lock:
                if cancel.is_set():
                    return
                self._action('live', {'slot': leases[0]['slot'], 'independent': True}, authority)
                self._action('audio', {'slot': leases[0]['slot'],
                    'muted': not videos[0]['metadata'].get('audio_present', False)})
                # Start includes permission for automatic crew work. The same
                # controller lock keeps a later human takeover authoritative.
                if getattr(cfg, 'crew_mode', 'automatic') == 'automatic':
                    self._action('resume', {})
                self.state = 'playing'
            for video in videos:
                self.app.video_analysis.submit(video)
            # Keep looping until Stop video / end. A publisher exit is a fault;
            # natural EOF is not expected while -stream_loop -1 is set.
            while not cancel.wait(.2) and not self.app.stop.is_set():
                if any(p.poll() is not None for p in processes):
                    raise StageError('Video playback failed; inspect the private server logs')
        except StageError as error:
            if not cancel.is_set() and not self.app.stop.is_set():
                failure = str(error)
        except Exception:
            if not cancel.is_set() and not self.app.stop.is_set():
                failure = 'Video playback could not start; check the configured file or S3 access'
        finally:
            if not self.app.stop.is_set():
                try:
                    current = self.app.program.status()
                    if current.get('primary_source_path') in self.assets and current['requested'] == 'LIVE':
                        self._action('holding', {})
                except Exception:
                    pass
            for process in processes:
                stop_process(process)
            for lease in leases:
                if self.app.stop.is_set():
                    break
                try:
                    self.app.release(lease['lease_id'])
                except Exception:
                    failure = failure or 'Video stopped; camera slot removal is pending'
            with self.lock:
                self.assets.clear()
                self.reason = failure or ''
                self.state = 'failed' if failure else 'stopped'
