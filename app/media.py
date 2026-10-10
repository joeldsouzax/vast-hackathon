"""Local media experiment. Receipt times are NOT calibrated capture timestamps."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from fractions import Fraction
import io
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Callable

import av
from PIL import Image, ImageDraw, ImageFont
from graphics import Graphics
from media_provenance import DecoderTrace
from direction_media import validate_crop, crop_pixels, Mixer
from foundation_records import DirectionSettings, Geometry


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def jpeg(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=80)
    return output.getvalue()


@dataclass(frozen=True)
class Frame:
    sequence: int
    received: float
    data: bytes
    media_s: float
    pts: int
    time_base: str
    native_provenance: dict | None = None
    source_epoch: int | None = None
    received_utc: float | None = None


class Source:
    def __init__(self, cfg, path: str, slot: int, epoch: int, has_audio: bool, on_discontinuity=None):
        self.cfg, self.path, self.slot, self.epoch = cfg, path, slot, epoch
        self.on_discontinuity = on_discontinuity
        self.timeline_revision = 1
        self.trace = DecoderTrace(cfg.fps,cfg.width,cfg.height)
        self.frames: deque[Frame] = deque()
        self.audio: deque[tuple[float, bytes]] = deque()
        self.bytes = 0
        self.sequence = 0
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.processes: list[subprocess.Popen] = []
        self.last_frame = 0.0
        self.error = None
        self.origin = None
        self.has_audio = has_audio
        self.threads = [threading.Thread(target=self._video, daemon=True)]
        for thread in self.threads:
            thread.start()

    def _decoder(self) -> subprocess.Popen:
        cfg = self.cfg
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-copyts",
                   "-rtsp_transport", "tcp", "-timeout", "3000000",
                   "-probesize", "32768", "-analyzeduration", "500000",
                   "-threads", "1", "-i", cfg.rtsp_url(self.path)]
        command += ["-map", "0:v:0", "-map", "0:a:0?", "-filter_threads", "1", "-vf",
                    f"showinfo@native,fps={cfg.fps},scale={cfg.width}:{cfg.height}:force_original_aspect_ratio=decrease,"
                    f"showinfo@scaled,pad={cfg.width}:{cfg.height}:(ow-iw)/2:(oh-ih)/2,showinfo@proxy",
                    "-c:v", "mjpeg", "-threads", "1", "-q:v", "5", "-pix_fmt", "yuvj420p",
                    "-c:a", "pcm_s16le", "-ac", "1", "-ar", "48000",
                    "-af", f"asetnsamples=n={48000 // cfg.fps}:p=1",
                    "-flush_packets", "1", "-f", "nut", "pipe:1"]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        def trace_reader():
            reported=None
            with open(cfg.runtime / f"decode-{self.slot}-{self.epoch}.log", "ab") as log:
                for line in process.stderr:
                    decoded=line.decode(errors="replace")
                    self.trace.accept(decoded)
                    # Keep failures, not one log line per frame. Trace state is bounded in memory.
                    if self.trace.failure!=reported:
                        log.write((str(self.trace.failure)+"\n").encode());reported=self.trace.failure
                    if "showinfo@" not in decoded:log.write(line)
                    if log.tell()>1048576:log.seek(0);log.truncate()
            process.stderr.close()
        trace_thread=threading.Thread(target=trace_reader,daemon=True)
        trace_thread.start()
        with self.lock:
            if self.stop.is_set():
                stop_process(process)
            self.processes.append(process)
        return process

    def _video(self):
        process = self._decoder()
        try:
            with av.open(process.stdout, mode="r", format="nut") as container:
                for packet in container.demux():
                    if self.stop.is_set():
                        break
                    if not packet.size or packet.pts is None:
                        continue
                    received = time.monotonic()
                    received_utc = time.time()
                    media_s = float(packet.pts * packet.time_base)
                    data = bytes(packet)
                    # FFmpeg writes metadata and media on separate pipes. Wait only
                    # in ingestion, outside the source lock and program frame loop.
                    provenance=self.trace.take(self.sequence+1,timeout=.1) if packet.stream.type=='video' else None
                    notification=None
                    with self.lock:
                        if packet.stream.type == "audio":
                            if len(data) == self.cfg.audio_size:
                                self.audio.append((media_s, data))
                                while self.audio and media_s - self.audio[0][0] > self.cfg.buffer_seconds:
                                    self.audio.popleft()
                            continue
                        if packet.stream.type != "video":
                            continue
                        if self.origin is None:
                            self.origin = received - media_s
                        self.sequence += 1
                        if provenance and provenance['timeline_revision']<self.timeline_revision:provenance=None
                        if provenance is None and self.trace.revision!=self.timeline_revision:
                            old_epoch=self.epoch;self.timeline_revision=self.trace.revision;self.epoch+=1
                            notification=(old_epoch,False)
                        if provenance and provenance['timeline_revision']!=self.timeline_revision:
                            old_epoch=self.epoch;self.timeline_revision=provenance['timeline_revision']
                            if provenance['source_discontinuity']:self.epoch+=1
                            notification=(old_epoch,not provenance['source_discontinuity'])
                        self.frames.append(Frame(self.sequence, received, data, media_s,
                                                 packet.pts, str(packet.time_base), provenance,self.epoch,received_utc))
                        self.bytes += len(data)
                        self.last_frame = received
                        while self.frames and (self.bytes > self.cfg.buffer_bytes or
                                               media_s - self.frames[0].media_s > self.cfg.buffer_seconds):
                            self.bytes -= len(self.frames.popleft().data)
                    # Lease SQL and recorder fsync must not hold the source lock
                    # that the continuous program needs for frame selection.
                    if notification and self.on_discontinuity:
                        self.on_discontinuity(self.path,notification[0],mapping_only=notification[1])
        except (OSError, ValueError, av.error.FFmpegError) as error:
            if not self.stop.is_set():
                self.error = str(error)
        finally:
            stop_process(process)
            process.stdout.close()
            if not self.stop.is_set():
                self.error = self.error or "Video decoder stopped; stop and rejoin this camera"

    def at(self, target: float) -> Frame | None:
        with self.lock:
            if self.origin is None:
                return None
            media_target = target - self.origin
            for frame in reversed(self.frames):
                if frame.media_s <= media_target:
                    return frame if media_target - frame.media_s <= 0.5 else None
        return None

    def audio_at(self, target: float) -> bytes:
        with self.lock:
            if self.origin is None:
                return bytes(self.cfg.audio_size)
            media_target = target - self.origin
            for media_s, data in reversed(self.audio):
                if media_s <= media_target:
                    return data if media_target - media_s <= 2 / self.cfg.fps else bytes(self.cfg.audio_size)
        return bytes(self.cfg.audio_size)

    def snapshot(self, seconds: float) -> list[Frame]:
        with self.lock:
            if not self.frames:
                raise ValueError("Camera has no retained decoded frames")
            end = self.frames[-1].media_s + 1 / self.cfg.fps
            frames = [f for f in self.frames if f.media_s >= end - seconds - 0.000001 and f.source_epoch in (None,self.epoch)]
            if len(frames) < self.cfg.fps or frames[-1].media_s - frames[0].media_s < seconds - 2 / self.cfg.fps:
                raise ValueError("The retained buffer is too short for this replay")
            if any(b.media_s - a.media_s > 2 / self.cfg.fps for a, b in zip(frames, frames[1:])):
                raise ValueError("The normalized replay interval contains a gap")
            if any(b.sequence != a.sequence + 1 for a, b in zip(frames, frames[1:])):
                raise ValueError("The replay interval contains a missing decoded frame")
            return frames  # Immutable bytes keep this snapshot pinned during rendering.

    def interval(self, start_s: float, end_s: float) -> list[Frame]:
        """Pin finalized normalized packets by timestamps, including the start's frame."""
        with self.lock:
            if not self.frames or not start_s < end_s:
                raise ValueError("No finalized retained media for this interval")
            frames = tuple(f for f in self.frames if f.source_epoch in (None,self.epoch))
            if not frames:raise ValueError("Current epoch has no retained frames")
            if start_s < frames[0].media_s - 1e-6 or end_s > frames[-1].media_s + 1 / self.cfg.fps + 1e-6:
                raise ValueError("Required media is missing or not finalized")
            before = [f for f in frames if f.media_s <= start_s + 1e-6]
            if not before:
                raise ValueError("The shot start is not retained")
            selected = [f for f in frames if before[-1].media_s <= f.media_s < end_s - 1e-6]
            if not selected or any(b.sequence != a.sequence + 1 or b.media_s - a.media_s > 1.5 / self.cfg.fps
                                   or b.media_s <= a.media_s or b.time_base != a.time_base
                                   for a, b in zip(selected, selected[1:])):
                raise ValueError("Required finalized media contains a timestamp or packet gap")
            if end_s - selected[-1].media_s > 1 / self.cfg.fps + .002:
                raise ValueError("The shot end is missing or unfinalized")
            return selected

    def status(self) -> dict:
        with self.lock:
            return {"epoch": self.epoch, "native_mapping_revision":getattr(self,'timeline_revision',1), "decoded_frames": self.sequence,
                    "last_frame_age_s": round(time.monotonic() - self.last_frame, 3) if self.frames else None,
                    "buffer_seconds": round(self.frames[-1].media_s - self.frames[0].media_s, 2) if self.frames else 0,
                    "current_epoch_buffer_seconds": round(self.frames[-1].media_s-next((f.media_s for f in self.frames if f.source_epoch in (None,self.epoch)),self.frames[-1].media_s),2) if self.frames else 0,
                    "buffer_bytes": self.bytes, "has_audio": self.has_audio,
                    "buffer_ready": self.at(time.monotonic() - self.cfg.delay) is not None,
                    "retained_source_interval_ms": [self.frames[0].media_s * 1000,
                                                     (self.frames[-1].media_s + 1 / self.cfg.fps) * 1000] if self.frames else None,
                    "capture_sync": "unknown", "error": self.error}

    def close(self):
        self.stop.set()
        with self.lock:
            processes = list(self.processes)
        for process in processes:
            stop_process(process)
        for thread in self.threads:
            thread.join(timeout=4)


@dataclass
class Replay:
    id: str
    slot: int
    epoch: int
    frames: list[bytes]
    duration: float
    speed: float
    path: Path
    report: dict


def compile_single_camera(cfg, replay_id: str, source: Source, seconds: float,
                          speed: float, zoom: float):
    # Compatible one-camera request translated into the shared ReplayPlan and worker.
    from replay import Resolved, digest
    if not math.isfinite(seconds) or not 1 <= seconds <= 6:
        raise ValueError("Replay source duration must be 1–6 seconds")
    if speed not in (0.5, 1.0, 2.0) or zoom not in (1.0, 1.5):
        raise ValueError("Use the supplied speed and crop presets")
    frames = tuple(source.snapshot(seconds))
    start, end = frames[0].media_s * 1000, (frames[-1].media_s + 1 / cfg.fps) * 1000
    duration = (end - start) / speed
    if duration > min(12000, float(os.environ.get('BREADCAST_REPLAY_MAX_DURATION_MS', '12000'))) + .001:
        raise ValueError("Replay exceeds 12 seconds")
    crop = [(1 - 1 / zoom) / 2, (1 - 1 / zoom) / 2, 1 / zoom, 1 / zoom]
    shot = {"source_id": f"camera-{source.slot}", "source_epoch": source.epoch,
            "event_start_ms": None, "event_end_ms": None, "source_start_ms": start, "source_end_ms": end,
            "speed": speed, "crop_normalized": crop, "edit": "continuous", "evidence_ids": [],
            "reason": "Operator selected the recent single-camera interval; action and capture time are unknown."}
    plan = {"schema_version": "1.1", "plan_id": replay_id, "event_id": "local-studio", "context_revision": 1,
            "scene_id": None, "scene_revision": None, "fixture": False, "timing_mode": "source_only",
            "source_mapping_revisions": {}, "evidence_ids": [], "evidence_revisions": {}, "shots": [shot],
            "transition": "cut", "expected_duration_ms": duration, "audio_policy": "mute_source_and_live_audio",
            "replay_marker": True, "score_overlay": "hidden",
            "output": {"width": cfg.width, "height": cfg.height, "fps_num": cfg.fps, "fps_den": 1}}
    resolved_shot = {**shot, "mapping_revision": None, "output_start_ms": 0, "output_end_ms": duration,
                     "combined_alignment_bound_ms": None,
                     "retained_media": {"source_path": source.path, "sequence": [frames[0].sequence, frames[-1].sequence + 1],
                                        "pts": [frames[0].pts, frames[-1].pts], "time_base": frames[0].time_base,
                                        "sha256": hashlib.sha256(b"".join(f.data for f in frames)).hexdigest()},
                     "orientation_transform": "FFmpeg autorotate, fit and pad to program dimensions before crop"}
    return Resolved(plan, [resolved_shot], [frames], duration, digest(plan))


def render_replay(cfg, replay_id: str, source: Source, seconds: float,
                  speed: float, zoom: float) -> Replay:
    from replay import render_plan
    return render_plan(cfg, replay_id, compile_single_camera(cfg, replay_id, source, seconds, speed, zoom))


class Program:
    """One controller thread selects frames. One encoder persists across changes."""
    def __init__(self, cfg, source_getter: Callable, log: Callable):
        self.cfg, self.source_getter, self.log = cfg, source_getter, log
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.requested = "HOLDING"
        self.actual = "HOLDING"
        self.actual_target = {"kind": "holding"}
        self.applied_revision = 0
        self.applied_commands = {}
        self.slot = 1
        self.audio_slot = 1
        self.primary_source_path = None
        self.audio_source_path = None
        self.audio_muted = False
        self.audio_epoch = None
        self.replay_revision = None
        self.replay: Replay | None = None
        self.replay_index = 0
        self.replay_transitions = None
        self.replay_ticket = None
        self.replay_guard=lambda ticket:True
        self.revision = 0
        self.frames_written = 0
        self.last_delay = None
        self.last_media_age = None
        self.frame = None
        self.error = None
        self.last_ack = 0.0
        self.encoder = None
        self.font = ImageFont.truetype(os.environ["BREADCAST_FONT"], 22)
        # Prepared brand artwork is loaded once; playback never waits on design work.
        with Image.open(Path(__file__).parent / "web" / "brand" / "holding.png") as slate:
            self.holding = slate.convert("RGB").resize((cfg.width, cfg.height))
        self.graphics = Graphics(cfg.width, cfg.height)
        self.graphics_applied = {"visible": [], "covers_camera": False}
        self.graphics_error = None
        self.framing = None
        self.cue = None
        self.cue_receipts = deque(maxlen=256)
        self.direction_settings=DirectionSettings()
        self.mixer=Mixer(self.direction_settings)
        self.event_mapping_reader=lambda _:None
        self.score_effective_ms=None
        # Live-to-live camera transition. Program target switches immediately;
        # only the pixels blend from the outgoing camera.
        self.transition_style=os.environ.get('BREADCAST_TRANSITION','dissolve')
        if self.transition_style not in ('dissolve','wipe','cut'):self.transition_style='dissolve'
        try:transition_ms=float(os.environ.get('BREADCAST_TRANSITION_MS','600'))
        except ValueError:transition_ms=600.0
        self.transition_frames=max(0,min(round(transition_ms/1000*cfg.fps),3*cfg.fps))
        self.transition=None
        self.last_live=None
        (cfg.runtime / "graphics-package.json").write_text(json.dumps(self.graphics.manifest(), indent=2) + "\n")
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _transition_image(self, image, source, target):
        """Blend from the previous live camera after a camera change. Caller holds the lock."""
        previous=self.last_live
        self.last_live={'path':source.path,'slot':source.slot,'image':image}
        if self.transition_style=='cut' or not self.transition_frames:return image
        if previous and previous['path']!=source.path:
            self.transition={'from_path':previous['path'],'from_slot':previous['slot'],
                'still':previous['image'],'start':self.frames_written}
        state=self.transition
        if not state:return image
        step=self.frames_written-state['start']
        if step>=self.transition_frames:
            self.transition=None
            return image
        # Keep the outgoing camera moving when it is still live; else hold its last frame.
        outgoing=self.source_getter(state['from_slot'])
        frame=outgoing.at(target) if outgoing and outgoing.path==state['from_path'] else None
        if frame:
            try:
                state['still']=Image.open(io.BytesIO(frame.data)).convert('RGB').resize(image.size)
            except Exception:
                pass
        old=state['still'] if state['still'].size==image.size else state['still'].resize(image.size)
        progress=(step+1)/(self.transition_frames+1)
        eased=progress*progress*(3-2*progress)
        if self.transition_style=='wipe':
            edge=round(image.width*eased)
            result=old.copy()
            if edge>0:result.paste(image.crop((0,0,edge,image.height)),(0,0))
            if 0<edge<image.width:
                ImageDraw.Draw(result).rectangle((max(0,edge-3),0,min(image.width-1,edge+2),image.height),fill='#EFA845')
            return result
        return Image.blend(old,image,eased)

    def start(self):
        self.thread.start()

    def command(self, action: str, revision: int, slot: int = 1,
                replay: Replay | None = None, independent: bool = False, graphics: dict | None = None, prepared=None,
                muted: bool = False, rect=None, geometry_revision=None, replay_transition=None) -> dict:
        # Bind text outside the media lock. A stale revision still prevents applying it.
        prepared = prepared if prepared is not None else (self.graphics.prepare(graphics) if action == "graphics" else None)
        transitions=None
        if action=='replay' and replay_transition:
            if replay_transition not in ('toast-wipe','ribbon-sweep','crumb-burst','iris-reveal'):
                raise ValueError('Unknown replay transition')
            transitions=tuple(self.graphics.prepare({'op':'cue','preset':replay_transition,
                'title':label,'subtitle':'','duration_s':.6}) for label in ('REPLAY','LIVE'))
        with self.lock:
            if revision != self.revision:
                raise ValueError("Program revision changed; refresh and retry")
            if action == "live":
                returning=self.requested=='REPLAY'
                source = self.source_getter(slot)
                if (slot != self.slot or (source and self.primary_source_path not in (None, source.path))) and not independent:
                    raise ValueError("Capture sync is unknown; acknowledge an independent view change")
                self.slot = slot
                self.primary_source_path = source.path if source else None
                self.requested, self.replay = "LIVE", None
                self.replay_ticket=None
                self.graphics.clear_cover()
                if returning and self.replay_transitions:self.graphics.apply(self.replay_transitions[1],time.monotonic())
                self.replay_transitions=None
                self.framing=None
                self.cancel_commentary('Picture changed')
            elif action == "holding":
                self.requested, self.replay = "HOLDING", None
                self.replay_ticket=None
                self.graphics.clear_cover()
                self.framing=None
                self.cancel_commentary('Holding selected')
            elif action == "audio":
                if type(muted) is not bool:
                    raise ValueError("Microphone mute must be a boolean")
                self.audio_slot = slot
                source = self.source_getter(slot)
                self.audio_source_path = source.path if source else None
                self.audio_epoch = source.epoch if source else None
                self.audio_muted = muted
            elif action == "replay" and replay:
                if not replay.frames or (replay.report.get('plan',{}).get('input_kind')!='archive' and not replay.path.is_file()) or not replay.report.get("decode_passed"):
                    raise ValueError("Replay is not validated and ready")
                self.requested, self.replay = "REPLAY", replay
                self.replay_index = 0
                self.replay_transitions=transitions
                self.replay_revision = self.revision + 1
                self.graphics.clear_cover()
                if transitions:self.graphics.apply(transitions[0],time.monotonic())
                self.framing=None
                self.cancel_commentary('Replay session changed')
            elif action in ('crop','reset_crop'):
                source=self.source_getter(self.slot)
                frame=source.at(time.monotonic()-self.cfg.delay) if source else None
                if action!='reset_crop' and (self.requested!='LIVE' or not frame or source.path!=self.primary_source_path):
                    raise ValueError('Framing requires a ready selected live source')
                if action=='reset_crop':self.framing=None
                else:
                    native=frame.native_provenance
                    if not native or not native.get('geometry'):raise ValueError('Live geometry is unknown')
                    if geometry_revision is not None and geometry_revision!=native['timeline_revision']:
                        raise ValueError('Reviewed live geometry revision changed')
                    geometry=Geometry.model_validate(native['geometry'])
                    crop=validate_crop(rect,geometry,self.cfg.width,self.cfg.height,self.direction_settings.max_magnification)
                    self.framing={'rect':crop,'geometry':geometry,'source_path':source.path,'epoch':source.epoch,
                        'geometry_revision':native['timeline_revision']}
                self.cancel_commentary('Framing changed')
            elif action == "graphics":
                if self.requested == "REPLAY" and prepared.op == "cue" and prepared.spec['slot'] in ('screen', 'stinger'):
                    raise ValueError("Return to live or hold before showing a full-screen graphic or transition")
                self.graphics.apply(prepared, time.monotonic())
                self.graphics_error = None
            else:
                raise ValueError("Unknown or unavailable program action")
            self.revision += 1
            self.log("command", action=action, revision=self.revision, slot=self.slot,
                     replay_id=replay.id if replay else None, independent_view=independent,
                     graphics=graphics if action == "graphics" else None)
            return self.status()

    def status(self) -> dict:
        with self.lock:
            return {"requested": self.requested, "actual": self.actual, "revision": self.revision,
                    "primary_slot": self.slot, "audio_slot": self.audio_slot,
                    "audio_muted": self.audio_muted,
                    "framing": {**self.framing,'rect':self.framing['rect'].model_dump(),
                        'geometry':self.framing['geometry'].model_dump()} if self.framing else None,
                    "commentary": {'cue_id':self.cue.id,'session_id':self.cue.session_id,
                        'expires_at':self.cue.expires_at} if self.cue else None,
                    "primary_source_path": self.primary_source_path, "audio_source_path": self.audio_source_path,
                    "replay_id": self.replay.id if self.replay else None,
                    "encoder_pid": self.encoder.pid if self.encoder else None,
                    "frames_written": self.frames_written, "error": self.error,
                    "decoded_frame_age_s": self.last_delay,
                    "normalized_media_age_s": self.last_media_age,
                    "actual_target": self.actual_target, "applied_revision": self.applied_revision,
                    "applied_commands": dict(self.applied_commands),
                    "graphics": {**self.graphics.public_state(), "applied": self.graphics_applied,
                                 "error": self.graphics_error},
                    "clock": "normalized PTS anchored to first decode; capture sync unknown",
                    "ack": "frame submitted to persistent encoder; viewer delivery checked separately"}

    def schedule_commentary(self, cue):
        with self.lock:
            if self.cue:raise ValueError('One commentary cue is already active')
            if not cue.valid() or self.frames_written>=cue.end_frame:raise ValueError('Commentary window expired')
            start=max(cue.start_frame,self.frames_written+2)
            duration_frames=math.ceil(len(cue.pcm)/self.cfg.audio_size)
            if cue.pcm and (start+duration_frames>cue.end_frame or
                    (start-self.frames_written+duration_frames)/self.cfg.fps>cue.expires_at-time.time()):
                raise ValueError('Decoded speech does not fit its original window')
            cue.start_frame=start
            cue.offset=0
            self.cue=cue

    def _cue_receipt(self, cue, channel, state, first=None, last=None, reason=None):
        delivered=cue.delivered.setdefault(channel,{})
        if delivered.get('terminal'):return
        if first is not None and 'first' not in delivered:delivered['first']=first
        if last is not None:delivered['last']=last
        if state in ('completed','interrupted','canceled','expired'):delivered['terminal']=True
        self.cue_receipts.append({'cue':cue,'channel':channel,'state':state,**delivered,'reason':reason})

    def cancel_commentary(self, reason):
        with self.lock:
            cue=self.cue
            if not cue:return
            cue.canceled=True
            cue.cancel_reason=reason
            for channel in ('speech','caption'):
                if channel=='speech' and not cue.pcm or channel=='caption' and cue.caption is None:continue
                if channel=='speech' and cue.audio_inflight:continue
                if channel=='caption' and cue.caption_inflight:continue
                delivered=cue.delivered.get(channel,{})
                self._cue_receipt(cue,channel,'interrupted' if 'first' in delivered else 'canceled',reason=reason)
            self.cue=None

    def _run(self):
        cfg = self.cfg
        audio_read, audio_write = os.pipe()
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
                   "-thread_queue_size", "128", "-probesize", "32", "-analyzeduration", "1",
                   "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-video_size", f"{cfg.width}x{cfg.height}", "-framerate", str(cfg.fps), "-i", "pipe:0",
                   # Both formats are explicit. Avoid startup probing that waits
                   # for audio while the controller blocks on a full video pipe.
                   "-thread_queue_size", "128", "-probesize", "32", "-analyzeduration", "1",
                   "-f", "s16le", "-ar", "48000", "-ac", "1",
                   "-i", f"pipe:{audio_read}", "-c:v", "libx264", "-threads", "1",
                   "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline",
                   "-pix_fmt", "yuv420p", "-g", str(cfg.fps), "-bf", "0",
                   "-c:a", "libopus", "-b:a", "64k", "-f", "rtsp", "-rtsp_transport", "tcp",
                   cfg.rtsp_url("program", cfg.program_token)]
        if cfg.program_proof:
            proof=str(cfg.program_proof.resolve())
            if any(c in proof for c in "|[]'\\"):
                raise ValueError("Proof path contains unsupported muxer characters")
            command=command[:-5]+["-map","0:v:0","-map","1:a:0","-flags","+global_header","-f","tee",
                f"[f=rtsp:rtsp_transport=tcp]{cfg.rtsp_url('program',cfg.program_token)}|[f=matroska:flush_packets=1:onfail=ignore]{proof}"]
        audio_queue: queue.Queue = queue.Queue(maxsize=cfg.fps * 2)
        log = open(cfg.runtime / "program-encoder.log", "ab")
        try:
            self.encoder = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=log,
                                            pass_fds=(audio_read,), bufsize=0)
        finally:
            log.close()
            os.close(audio_read)

        def write_audio():
            cue=None;voice=b'';offset=0;program_frame=0
            try:
                with os.fdopen(audio_write, "wb", buffering=0) as stream:
                    while not self.stop.is_set() or not audio_queue.empty():
                        try:
                            ambient, cue, voice_offset, program_frame = audio_queue.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        with self.lock:
                            voice=b''
                            if cue and self.cue is cue and cue.valid():
                                if cue.offset<len(cue.pcm) and voice_offset!=cue.offset:
                                    self.cancel_commentary('Speech sample sequence is discontinuous')
                                else:voice=cue.pcm[voice_offset:voice_offset+cfg.audio_size]
                                cue.audio_inflight=bool(voice)
                            data=self.mixer.mix(ambient,voice,voice_offset//2,len(cue.pcm)//2 if cue else 0)
                        offset = 0
                        while offset < len(data):
                            offset += stream.write(data[offset:])
                        if voice:
                            with self.lock:
                                first=program_frame*(48000//cfg.fps)
                                last=first+len(voice)//2
                                delivered=cue.delivered.get('speech',{})
                                if 'first' not in delivered:self._cue_receipt(cue,'speech','started',first,last)
                                else:delivered['last']=last
                                cue.offset+=len(voice)
                                cue.audio_inflight=False
                                if cue.canceled:self._cue_receipt(cue,'speech','interrupted',last=last,reason=cue.cancel_reason)
                                elif cue.offset==len(cue.pcm):self._cue_receipt(cue,'speech','completed',last=last)
            except (BrokenPipeError, OSError):
                if cue and voice:
                    with self.lock:
                        written=min(offset,len(voice))//2
                        cue.audio_inflight=False
                        first=program_frame*(48000//cfg.fps)
                        if written:
                            if 'first' not in cue.delivered.get('speech',{}):self._cue_receipt(cue,'speech','started',first,first+written)
                            self._cue_receipt(cue,'speech','interrupted',last=first+written,reason='Encoder audio pipe failed')
                        else:self._cue_receipt(cue,'speech','canceled',reason='Encoder audio pipe failed before submission')
                self.stop.set()

        audio_thread = threading.Thread(target=write_audio, daemon=True)
        audio_thread.start()
        mapping_batch = []
        next_frame = time.monotonic()
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if now < next_frame:
                    self.stop.wait(next_frame - now)
                now = time.monotonic()
                target = now - cfg.delay
                audio = bytes(cfg.audio_size)
                with self.lock:
                    self.last_delay = None
                    self.last_media_age = None
                    audio_source = self.source_getter(self.audio_slot)
                    if self.audio_source_path is None and audio_source:
                        # Resolve the existing default microphone once. A later owner
                        # of that slot cannot inherit this selection.
                        self.audio_source_path = audio_source.path
                        self.audio_epoch=audio_source.epoch
                        self.log("default_audio_bound", source_path=audio_source.path, slot=self.audio_slot)
                    if self.requested == "REPLAY" and self.replay:
                        revoked=self.replay_ticket is not None and not self.replay_guard(self.replay_ticket)
                        if revoked or self.replay_index >= len(self.replay.frames):
                            self.requested, self.replay = "LIVE", None
                            self.replay_ticket=None
                            self.framing=None
                            self.cancel_commentary('Archive dependency unavailable' if revoked else 'Replay finished; return to live')
                            if not revoked and self.replay_transitions:self.graphics.apply(self.replay_transitions[1],now)
                            self.replay_transitions=None
                            self.revision += 1
                            self.log("replay_finished", revision=self.revision)
                        else:
                            image = Image.open(io.BytesIO(self.replay.frames[self.replay_index])).convert("RGB")
                            actual_target = {"kind": "replay", "id": self.replay.id,
                                             "output_frame": self.replay_index, "command_revision": self.replay_revision}
                            if not ('stinger' in self.graphics.active or 'stinger' in self.graphics.retiring):
                                self.replay_index += 1
                            shot = next((s for s in self.replay.report.get("source_map", [])
                                         if s["output_frame_start"] <= actual_target['output_frame'] < s["output_frame_end"]), None)
                            speed = shot["speed"] if shot else self.replay.speed
                            actual, label = "REPLAY", f"REPLAY  {speed:g}x"
                            if shot:
                                actual_target.update(source_id=shot["source_id"], speed=speed, edit=shot["edit"])
                                native_frame=next((f for f in shot.get('native_frames',[]) if f['output_frame']==actual_target['output_frame']),None)
                                actual_target.update(source_path=shot['retained_media']['source_path'],epoch=shot['source_epoch'],
                                    native=native_frame['native'] if native_frame else None,
                                    event_ms=shot['event_start_ms']+(actual_target['output_frame']-self.cfg.fps*shot['output_start_ms']/1000)*1000/self.cfg.fps*speed if shot.get('event_start_ms') is not None else None)
                                if shot.get('source'):
                                    actual_target['archive_source']=shot['source']
                                    mapping_rate=(shot['event_end_ms']-shot['event_start_ms'])/(shot['source_end_ms']-shot['source_start_ms']) if shot.get('event_start_ms') is not None else None
                                    if mapping_rate and native_frame:
                                        actual_target['event_ms']=shot['event_start_ms']+(native_frame['native']['native_pts']*float(Fraction(shot['source']['time_base']))*1000-shot['source_start_ms'])*mapping_rate
                                if shot["edit"] == "repeat":
                                    label += "  ALTERNATE ANGLE"
                                label = ""  # The checked asset already contains this shot's marker and speed.
                    if self.requested != "REPLAY":
                        source = self.source_getter(self.slot)
                        frame = source.at(target) if source and self.requested == "LIVE" and source.path == self.primary_source_path else None
                        if frame:
                            image = Image.open(io.BytesIO(frame.data)).convert("RGB")
                            actual, label = "LIVE", f"LIVE  /  CAMERA {self.slot}"
                            self.last_delay = round(now - frame.received, 3)
                            self.last_media_age = round(now - (source.origin + frame.media_s), 3)
                            actual_target = {"kind": "camera", "slot": source.slot, "source_path": source.path,
                                             "epoch": frame.source_epoch or source.epoch, "sequence": frame.sequence,
                                             "proxy_pts":frame.pts,"proxy_time_base":frame.time_base,
                                             "received_utc":frame.received_utc,
                                             "native":frame.native_provenance}
                            mapping=self.event_mapping_reader(source.path)
                            if mapping and mapping.source.epoch==source.epoch and frame.native_provenance:
                                native=frame.native_provenance
                                pts=round(native['native_pts']*float(Fraction(native['native_time_base'])/Fraction(mapping.source.time_base)))
                                try:actual_target['event_ms']=mapping.event_ms(pts)-mapping.uncertainty_ms
                                except ValueError:pass
                            if self.framing:
                                native=frame.native_provenance or {}
                                if (self.framing['source_path']!=source.path or self.framing['epoch']!=source.epoch or
                                    self.framing['geometry_revision']!=native.get('timeline_revision') or
                                    self.framing['geometry'].model_dump()!=native.get('geometry')):
                                    self.framing=None
                                else:image=crop_pixels(image,self.framing['rect'],self.framing['geometry'])
                            actual_target['framing']=self.framing['rect'].model_dump() if self.framing else None
                            image=self._transition_image(image,source,target)
                            audio_source = self.source_getter(self.audio_slot)
                            if not self.audio_muted and audio_source and audio_source.path == self.audio_source_path and audio_source.epoch==self.audio_epoch:
                                audio = audio_source.audio_at(target)
                        else:
                            image, actual, label = self.holding.copy(), "HOLDING", ""
                            actual_target = {"kind": "holding"}
                            self.framing=None
                            self.transition=None;self.last_live=None
                    else:
                        self.transition=None;self.last_live=None
                    try:
                        if self.graphics.event_package:
                            event_ms=actual_target.get('event_ms')
                            self.graphics.official_eligible=(self.score_effective_ms is not None and event_ms is not None and self.score_effective_ms<=event_ms)
                        before_graphics = {slot: cue.id for slot, cue in self.graphics.active.items()}
                        image, applied_graphics = self.graphics.compose(image, now, actual)
                        if before_graphics != {slot: cue.id for slot, cue in self.graphics.active.items()}:
                            self.revision += 1
                            self.log("graphics_expired", revision=self.revision,
                                     remaining=[cue.summary() for cue in self.graphics.active.values()])
                    except Exception as error:
                        # A graphics fault must not stop the encoder or the underlying media.
                        if not self.graphics_error:
                            self.log("graphics_failure", error=str(error))
                        self.graphics_error = str(error)
                        if self.graphics.active: self.revision += 1
                        self.graphics.active.clear(); self.graphics.retiring.clear()
                        applied_graphics = {"visible": [], "covers_camera": False, "error": str(error)}
                    # Full-screen cards mute live ambient sound throughout their entry and exit.
                    if any(cue["slot"] in ("screen","stinger") for cue in applied_graphics["visible"]):
                        audio = bytes(cfg.audio_size)
                        self.cancel_commentary('Full-screen graphic suppresses commentary')
                    cue=self.cue
                    caption_visible=False;voice_offset=0
                    if cue:
                        changed=any(actual_target.get(key)!=value for key,value in cue.target.items())
                        changed=changed or actual_target.get('kind') in ('camera','replay') and not actual_target.get('native')
                        speech_done=bool(cue.pcm and cue.delivered.get('speech',{}).get('terminal') and cue.offset==len(cue.pcm))
                        if changed or not cue.valid() or self.frames_written>=cue.end_frame or speech_done:
                            if (self.frames_written>=cue.end_frame or speech_done) and cue.valid() and not changed:
                                if cue.caption is not None:
                                    delivered=cue.delivered.get('caption',{})
                                    self._cue_receipt(cue,'caption','completed' if 'first' in delivered else 'expired')
                                # Speech audio acknowledgement is independent of video acknowledgement.
                                if cue.pcm and not cue.delivered.get('speech',{}).get('terminal'):
                                    self.cancel_commentary('Speech missed its original window')
                                else:self.cue=None
                            else:self.cancel_commentary('Cue dependencies changed or expired')
                            cue=None
                        elif self.frames_written>=cue.start_frame:
                            voice_offset=(self.frames_written-cue.start_frame)*cfg.audio_size
                            occupied=any(c['slot'] in ('screen','stinger','lower','ticker','banner') for c in applied_graphics['visible'])
                            if cue.caption is not None and not occupied:
                                image=Image.alpha_composite(image.convert('RGBA'),cue.caption).convert('RGB')
                                caption_visible=True
                                cue.caption_inflight=True
                    draw = ImageDraw.Draw(image)
                    if label and (actual == "REPLAY" or not applied_graphics["covers_camera"]):
                        box = draw.textbbox((0, 0), label, font=self.font)
                        draw.rounded_rectangle((12, 12, box[2] + 36, 49), radius=7, fill="#253028")
                        draw.text((24, 15), label, font=self.font, fill="#EFA845" if actual == "REPLAY" else "#F7F4EC")
                    if actual=='REPLAY' and self.replay:
                        progress=(actual_target['output_frame']+1)/len(self.replay.frames)
                        draw.rectangle((14,51,174,54),fill='#253028')
                        draw.rectangle((14,51,14+round(160*progress),54),fill='#EFA845')
                    self.frame = jpeg(image)
                    rendered_revision = self.revision
                audio_queue.put((audio,cue if cue and self.frames_written>=cue.start_frame else None,voice_offset,self.frames_written), timeout=2)
                raw = image.tobytes()
                # Raw pipe writes can be partial. Complete every frame.
                offset = 0
                while offset < len(raw):
                    offset += self.encoder.stdin.write(raw[offset:])
                with self.lock:
                    if caption_visible:
                        delivered=cue.delivered.get('caption',{})
                        if 'first' not in delivered:self._cue_receipt(cue,'caption','started',self.frames_written,self.frames_written+1)
                        else:delivered['last']=self.frames_written+1
                        cue.caption_inflight=False
                        if cue.canceled:self._cue_receipt(cue,'caption','interrupted',reason=cue.cancel_reason)
                    previous = self.actual_target
                    target_changed = any(previous.get(k) != actual_target.get(k)
                                         for k in ("kind", "id", "source_path", "epoch"))
                    graphics_changed = [(c['cue_id'], c.get('exiting', False)) for c in applied_graphics['visible']] != \
                        [(c['cue_id'], c.get('exiting', False)) for c in self.graphics_applied['visible']]
                    if actual != self.actual or target_changed or graphics_changed or rendered_revision != self.applied_revision:
                        self.log("encoder_frame_state", state=actual, revision=rendered_revision,
                                 target=actual_target, graphics=applied_graphics, program_frame=self.frames_written)
                    self.actual = actual
                    self.actual_target = actual_target
                    self.applied_revision = rendered_revision
                    if str(rendered_revision) not in self.applied_commands:
                        self.applied_commands[str(rendered_revision)] = {"monotonic_s": time.monotonic(), "target": actual_target}
                        if len(self.applied_commands) > 512:
                            self.applied_commands.pop(next(iter(self.applied_commands)))
                    self.graphics_applied = applied_graphics
                    mapping_batch.append({"program_frame":self.frames_written,"program_ms":round(self.frames_written*1000/cfg.fps),
                        "target":actual_target,"program_revision":rendered_revision})
                    if len(mapping_batch)>=cfg.fps:
                        self.log("program_source_map",frames=mapping_batch,ack="encoder submission; not viewer delivery")
                        mapping_batch=[]
                    self.frames_written += 1
                    self.last_ack = time.monotonic()
                next_frame += 1 / cfg.fps
                if now - next_frame > 0.5:
                    self.log("encoder_schedule_late", late_s=round(now - next_frame, 3))
                    next_frame = now
        except (OSError, queue.Full) as error:
            self.error = str(error)
            self.log("encoder_failure", error=self.error)
            with self.lock:
                if self.cue:self.cue.caption_inflight=False
                self.cancel_commentary('Encoder frame pipe failed')
        finally:
            self.stop.set()
            self.encoder.stdin.close()
            audio_thread.join(timeout=3)
            try:self.encoder.wait(timeout=3)
            except subprocess.TimeoutExpired:stop_process(self.encoder)

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        if self.thread.is_alive():
            stop_process(self.encoder)
            self.thread.join(timeout=3)
