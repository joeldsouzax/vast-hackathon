"""Bounded local ReplayPlan compiler. No provider decisions or capture-time guesses."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat


EVENT_ID = 'local-studio'
SPEEDS = (0.5, 1.0, 2.0)


def number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return value


def integer(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f'{name} must be a positive integer')
    return value


def text(value, name, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{name} must contain 1–{limit} characters')
    return value


def keys(data, allowed, required=()):
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise ValueError('Missing or unsupported contract fields')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Mapping:
    source_id: str
    source_epoch: int
    source_path: str
    revision: int
    origin_pts: int
    time_base: str
    offset_ms: float
    rate_correction: float
    uncertainty_ms: float
    calibration_interval_ms: tuple[float, float]
    markers: tuple[dict, ...]
    measured_residual_ms: float
    timestamp_basis: str
    native_mapping_revision: int = 1

    def event(self, source_ms):
        return self.offset_ms + (source_ms - self.origin_pts * float(Fraction(self.time_base)) * 1000) * self.rate_correction

    def source(self, event_ms):
        return (event_ms - self.offset_ms) / self.rate_correction + self.origin_pts * float(Fraction(self.time_base)) * 1000

    def check(self, start, end):
        if not self.calibration_interval_ms[0] <= start < end <= self.calibration_interval_ms[1]:
            raise ValueError('Timing mapping is stale or outside its calibration interval')


@dataclass
class Resolved:
    plan: dict
    shots: list[dict]
    pins: list[tuple]
    expected_ms: float
    plan_hash: str
    deadline: float | None = None
    release_callback: object = None
    scratch_bytes: int = 268435456
    monotonic_deadline: float | None = None

    def release(self):
        self.pins.clear()
        if self.release_callback:
            callback,self.release_callback=self.release_callback,None
            callback()


class ReplayContext:
    """One event, versioned calibration and operator/fixture evidence. Lock excludes edits."""
    def __init__(self, cfg, source_getter):
        self.cfg, self.source_getter = cfg, source_getter
        self.lock = threading.RLock()
        self.mappings = {}
        self.evidence = {}
        self.tolerance_ms = float(os.environ.get('BREADCAST_REPLAY_TOLERANCE_MS', '150'))
        self.max_shots = int(os.environ.get('BREADCAST_REPLAY_MAX_SHOTS', '6'))
        self.max_duration_ms = float(os.environ.get('BREADCAST_REPLAY_MAX_DURATION_MS', '12000'))
        if not 0 < self.tolerance_ms <= 1000 or not 1 <= self.max_shots <= 12 or not 0 < self.max_duration_ms <= 12000:
            raise ValueError('Invalid replay limits')

    def source(self, source_id, epoch):
        if not isinstance(source_id, str) or not source_id.startswith('camera-') or source_id not in {f'camera-{i}' for i in range(1, 6)}:
            raise ValueError('Source must belong to this event and one of its five camera slots')
        integer(epoch, 'Source epoch')
        source = self.source_getter(int(source_id[7:]))
        if source is None or source.epoch != epoch:
            raise ValueError('Source epoch is unavailable; reconnect needs new calibration and evidence')
        return source

    def calibrate(self, data):
        keys(data, ('source_id', 'source_epoch', 'markers', 'uncertainty_ms'), ('source_id', 'source_epoch', 'markers', 'uncertainty_ms'))
        source = self.source(data['source_id'], data['source_epoch'])
        markers = data['markers']
        if not isinstance(markers, list) or not 3 <= len(markers) <= 16:
            raise ValueError('Use 3–16 shared visible markers: endpoints and at least one independent check')
        markers = json.loads(json.dumps(markers, allow_nan=False))
        with source.lock:
            frames = tuple(source.frames)
        if not frames:
            raise ValueError('Calibration requires retained media')
        for marker in markers:
            keys(marker, ('pts', 'event_ms', 'label'), ('pts', 'event_ms', 'label'))
            if type(marker['pts']) is not int or marker['pts'] not in {f.pts for f in frames}:
                raise ValueError('Marker PTS must identify a retained frame')
            number(marker['event_ms'], 'Marker event time')
            text(marker['label'], 'Shared visible marker label', 120)
        selected=[f for f in frames if f.pts in {m['pts'] for m in markers}]
        native_revisions={f.native_provenance.get('timeline_revision',1) if getattr(f,'native_provenance',None) else 1 for f in selected}
        if len(native_revisions)!=1 or native_revisions!={getattr(source,'timeline_revision',1)}:
            raise ValueError('Calibration markers cross a native decoder mapping change')
        markers.sort(key=lambda m: m['pts'])
        base = frames[0].time_base
        if any(f.time_base != base for f in frames):
            raise ValueError('Time base changed; start a new epoch')
        tick_ms = float(Fraction(base)) * 1000
        first, last = markers[0], markers[-1]
        span = (last['pts'] - first['pts']) * tick_ms
        if span < 1000:
            raise ValueError('Calibration markers must span at least one second')
        rate = (last['event_ms'] - first['event_ms']) / span
        if not 0.98 <= rate <= 1.02 or any(b['event_ms'] <= a['event_ms'] for a, b in zip(markers, markers[1:])):
            raise ValueError('Invalid calibration order or excessive source drift')
        residual = max(abs(m['event_ms'] - (first['event_ms'] + (m['pts'] - first['pts']) * tick_ms * rate)) for m in markers)
        bound = number(data['uncertainty_ms'], 'Marker uncertainty')
        if not 0 < bound <= 1000:
            raise ValueError('Supply a positive measured marker uncertainty bound')
        with self.lock:
            key = (data['source_id'], source.epoch)
            revision = len(self.mappings.get(key, [])) + 1
            if revision > 64:
                raise ValueError('Calibration revision limit reached; start a new studio run')
            mapping = Mapping(data['source_id'], source.epoch, source.path, revision, first['pts'], base, first['event_ms'],
                              rate, bound + residual, (first['event_ms'], last['event_ms']), tuple(markers), residual,
                              'normalized proxy PTS; native decoder mappings retain separate revisions',getattr(source,'timeline_revision',1))
            self.mappings.setdefault(key, []).append(mapping)
            self.save()
            return asdict(mapping)

    def mapping(self, source_id, epoch, revision):
        integer(revision, 'Mapping revision')
        candidates = self.mappings.get((source_id, epoch), [])
        if revision > len(candidates):
            raise ValueError('Capture synchronization is unknown; calibrate this source epoch')
        mapping = candidates[revision - 1]
        current=self.source(source_id,epoch)
        if getattr(current,'timeline_revision',1)!=getattr(mapping,'native_mapping_revision',1):
            raise ValueError('Native decoder mapping changed; calibrate again')
        if current.path != mapping.source_path:
            raise ValueError('Timing mapping belongs to a replaced camera lease; calibrate the new source')
        return mapping

    def register_evidence(self, data):
        allowed = ('evidence_id', 'revision', 'event_id', 'scene_id', 'scene_revision', 'source_id', 'source_epoch',
                   'mapping_revision', 'event_start_ms', 'event_end_ms', 'action', 'claim_kind', 'subject_visible',
                   'quality', 'adds', 'origin', 'status', 'expires_at')
        keys(data, allowed, allowed)
        text(data['evidence_id'], 'Evidence ID', 120)
        text(data['scene_id'], 'Scene ID', 120)
        integer(data['revision'], 'Evidence revision'); integer(data['scene_revision'], 'Scene revision')
        if data['revision'] > 64:
            raise ValueError('Evidence revision limit reached; start a new studio run')
        if data['event_id'] != EVENT_ID or data['origin'] not in ('operator', 'fixture'):
            raise ValueError('Local evidence must belong to local-studio and declare operator or fixture origin')
        if data['claim_kind'] != 'observation':
            raise ValueError('Local visual evidence cannot confirm identity or official results')
        if data['status'] not in ('active', 'retracted') or type(data['subject_visible']) is not bool:
            raise ValueError('Invalid evidence status or visibility')
        if data['quality'] not in ('usable', 'obscured', 'blurred', 'motion', 'missing'):
            raise ValueError('Invalid view quality')
        text(data['action'], 'Visible action'); text(data['adds'], 'View contribution')
        start, end = number(data['event_start_ms'], 'Evidence start'), number(data['event_end_ms'], 'Evidence end')
        expiry = number(data['expires_at'], 'Evidence expiry')
        if expiry <= time.time() or expiry > time.time() + 3600:
            raise ValueError('Evidence expiry must be within the next hour')
        with self.lock:
            source = self.source(data['source_id'], data['source_epoch'])
            mapping = self.mapping(data['source_id'], source.epoch, data['mapping_revision'])
            mapping.check(start, end)
            source.interval(mapping.source(start) / 1000, mapping.source(end) / 1000)
            old = self.evidence.get(data['evidence_id'])
            if not old and len(self.evidence) >= 256:
                raise ValueError('Evidence record limit reached; start a new studio run')
            if data['revision'] != (old['revision'] + 1 if old else 1):
                raise ValueError('Evidence revision must advance by one')
            if old and any(data[k] != old[k] for k in ('event_id', 'scene_id', 'source_id', 'source_epoch')):
                raise ValueError('An evidence ID cannot change source or scene ownership')
            self.evidence[data['evidence_id']] = json.loads(json.dumps(data, allow_nan=False))
            folder = self.cfg.runtime / 'replay-evidence'
            folder.mkdir(exist_ok=True)
            (folder / f"{digest(data['evidence_id'])}-{data['revision']}.json").write_text(json.dumps(data, indent=2) + '\n')
            self.save()
            return self.evidence[data['evidence_id']].copy()

    def save(self):
        """Local artifact only. VAST durability and restart recovery remain separate work."""
        temporary = self.cfg.runtime / 'replay-context.new'
        temporary.write_text(json.dumps(self.summary(), indent=2) + '\n')
        temporary.replace(self.cfg.runtime / 'replay-context.json')

    def accept_segmentor_response(self, response, deadline):
        """Adapter boundary: a model response can validate a plan, never start airtime."""
        number(deadline, 'Segmentor deadline')
        if time.monotonic() >= deadline:
            raise ValueError('Segmentor timeout: skip the replay and keep live playback')
        if isinstance(response, str):
            if len(response.encode()) > 65536:
                raise ValueError('Segmentor response exceeds the bounded context limit')
            try:
                response = json.loads(response)
            except json.JSONDecodeError as error:
                raise ValueError('Malformed segmentor response: skip the replay') from error
        resolved = self.resolve(response)
        if time.monotonic() >= deadline:
            resolved.release()
            raise ValueError('Segmentor timeout: skip the replay and keep live playback')
        return resolved

    def eligible(self, plan):
        if plan['expires_at'] <= time.time():
            raise ValueError('Replay plan expired')
        for eid, revision in plan['evidence_revisions'].items():
            evidence = self.evidence.get(eid)
            if not evidence or evidence['revision'] != revision or evidence['status'] != 'active' or evidence['expires_at'] <= time.time():
                raise ValueError('Replay evidence is stale, expired, or retracted')
            if evidence['scene_id'] != plan['scene_id'] or evidence['scene_revision'] != plan['scene_revision']:
                raise ValueError('Replay scene revision does not match its evidence')

    def resolve(self, raw):
        allowed = ('schema_version', 'plan_id', 'event_id', 'context_revision', 'scene_id', 'scene_revision', 'fixture',
                   'expires_at', 'source_mapping_revisions', 'evidence_ids', 'evidence_revisions', 'shots', 'transition',
                   'expected_duration_ms', 'audio_policy', 'replay_marker', 'score_overlay', 'output', 'selection_reason')
        keys(raw, allowed, tuple(k for k in allowed if k not in ('expected_duration_ms', 'selection_reason')))
        plan = json.loads(json.dumps(raw, allow_nan=False))
        if plan['schema_version'] != '1.1' or plan['event_id'] != EVENT_ID or plan['context_revision'] != 1:
            raise ValueError('Unsupported ReplayPlan version, event, or context')
        integer(plan['context_revision'], 'Context revision')
        text(plan['plan_id'], 'Plan ID', 120); text(plan['scene_id'], 'Scene ID', 120)
        integer(plan['scene_revision'], 'Scene revision')
        number(plan['expires_at'], 'Plan expiry')
        if type(plan['fixture']) is not bool:
            raise ValueError('Fixture flag must be a boolean')
        if plan['transition'] != 'cut' or plan['audio_policy'] != 'mute_source_and_live_audio' or plan['replay_marker'] is not True or plan['score_overlay'] != 'hidden':
            raise ValueError('Only hard cuts with muted audio, REPLAY marker, and hidden score/clock are allowed')
        if plan['output'] != {'width': self.cfg.width, 'height': self.cfg.height, 'fps_num': self.cfg.fps, 'fps_den': 1}:
            raise ValueError('Replay output must match the program format')
        if not isinstance(plan['shots'], list) or not 1 <= len(plan['shots']) <= self.max_shots:
            raise ValueError('Replay shot count exceeds the configured limit')
        if not isinstance(plan['evidence_revisions'], dict) or not isinstance(plan['source_mapping_revisions'], dict) or not isinstance(plan['evidence_ids'], list):
            raise ValueError('Invalid evidence or mapping references')
        if set(plan['evidence_ids']) != set(plan['evidence_revisions']) or len(set(plan['evidence_ids'])) != len(plan['evidence_ids']):
            raise ValueError('Evidence IDs and revisions must match exactly')
        resolved, pins, total, used_evidence, used_mappings = [], [], 0.0, set(), {}
        with self.lock:
            self.eligible(plan)
            previous = None
            mappings = []
            for index, shot in enumerate(plan['shots']):
                fields = ('source_id', 'source_epoch', 'event_start_ms', 'event_end_ms', 'speed', 'crop_normalized', 'evidence_ids', 'reason', 'edit')
                keys(shot, fields, fields)
                source = self.source(shot['source_id'], shot['source_epoch'])
                key = f"{shot['source_id']}:{source.epoch}"
                mapping = self.mapping(shot['source_id'], source.epoch, plan['source_mapping_revisions'].get(key))
                used_mappings[key] = mapping.revision
                start, end = number(shot['event_start_ms'], 'Shot start'), number(shot['event_end_ms'], 'Shot end')
                mapping.check(start, end)
                speed = number(shot['speed'], 'Speed')
                if speed not in SPEEDS or end - start > 6000 or end - start < 200:
                    raise ValueError('Use speed presets and a shot of 0.2–6 event seconds')
                crop = shot['crop_normalized']
                if not isinstance(crop, list) or len(crop) != 4:
                    raise ValueError('Crop must be [left, top, width, height] in oriented frame coordinates')
                x, y, w, h = [number(v, 'Crop coordinate') for v in crop]
                if x < 0 or y < 0 or w < 0.2 or h < 0.2 or x + w > 1.000000001 or y + h > 1.000000001 or abs(w - h) > 0.000001:
                    raise ValueError('Static crop must stay inside the oriented frame and preserve output aspect ratio')
                text(shot['reason'], 'Editorial reason')
                if shot['edit'] not in ('continuous', 'repeat') or (index == 0 and shot['edit'] != 'continuous'):
                    raise ValueError('First shot must establish continuous action; later shots can explicitly repeat it')
                if previous:
                    if shot['edit'] == 'continuous' and start != previous['event_end_ms']:
                        raise ValueError('Continuous cuts must meet at the same event timestamp; action cannot be omitted or repeated')
                    if shot['edit'] == 'repeat' and not any(start >= s['event_start_ms'] and end <= s['event_end_ms'] and shot['source_id'] != s['source_id'] for s in plan['shots'][:index]):
                        raise ValueError('Repeat must show a previously shown interval from a different camera')
                ids = shot['evidence_ids']
                if not isinstance(ids, list) or not ids or len(ids) > 16 or len(set(ids)) != len(ids):
                    raise ValueError('Each shot needs bounded source-linked evidence')
                coverage = []
                for eid in ids:
                    if eid not in plan['evidence_revisions']:
                        raise ValueError('Shot evidence is missing from plan revisions')
                    ev = self.evidence[eid]
                    if (ev['source_id'], ev['source_epoch'], ev['mapping_revision']) != (shot['source_id'], source.epoch, mapping.revision):
                        raise ValueError('Evidence source, epoch, or mapping does not match the shot')
                    if ev['quality'] != 'usable' or not ev['subject_visible'] or (ev['origin'] == 'fixture' and not plan['fixture']):
                        raise ValueError('View is unsupported, obscured, or uses unlabeled fixture evidence')
                    coverage.append((ev['event_start_ms'], ev['event_end_ms']))
                cursor = start
                for a, b in sorted(coverage):
                    if a <= cursor:
                        cursor = max(cursor, b)
                if cursor < end:
                    raise ValueError('Visual evidence does not cover the chosen shot boundaries')
                used_evidence.update(ids)
                source_start, source_end = mapping.source(start) / 1000, mapping.source(end) / 1000
                frames = tuple(source.interval(source_start, source_end))
                pins.append(frames)
                duration = (source_end - source_start) * 1000 / speed
                resolved.append({**shot, 'mapping_revision': mapping.revision, 'source_start_ms': source_start * 1000,
                                 'source_end_ms': source_end * 1000, 'output_start_ms': total, 'output_end_ms': total + duration,
                                 'retained_media': {'source_path': source.path, 'sequence': [frames[0].sequence, frames[-1].sequence + 1],
                                                    'pts': [frames[0].pts, frames[-1].pts], 'time_base': frames[0].time_base,
                                                    'sha256': hashlib.sha256(b''.join(f.data for f in frames)).hexdigest()},
                                 'orientation_transform': 'FFmpeg autorotate, fit and pad to program dimensions before crop'})
                total += duration
                mappings.append(mapping)
                previous = shot
            if used_evidence != set(plan['evidence_ids']) or used_mappings != plan['source_mapping_revisions']:
                raise ValueError('Plan must reference exactly the evidence and mappings used by its shots')
            # All selected angles must be valid throughout their selected intervals. Frame quantization
            # contributes a conservative one-frame bound per source, in addition to marker uncertainty.
            cross_bounds = []
            for i, a in enumerate(resolved):
                for j, b in enumerate(resolved[:i]):
                    if a['source_id'] != b['source_id']:
                        uncertainty = mappings[i].uncertainty_ms + mappings[j].uncertainty_ms + 2000 / self.cfg.fps
                        cross_bounds.append(uncertainty)
                        if uncertainty > self.tolerance_ms:
                            raise ValueError('Combined alignment uncertainty exceeds the replay tolerance')
            if total > self.max_duration_ms or total < 200:
                raise ValueError('Replay duration exceeds the configured limit')
            if 'expected_duration_ms' in plan and abs(number(plan['expected_duration_ms'], 'Expected duration') - total) > 0.001:
                raise ValueError('Stored duration does not match the source durations divided by speed')
            plan['expected_duration_ms'] = total
            plan_hash = digest(plan)
            for shot in resolved:
                shot['combined_alignment_bound_ms'] = max(cross_bounds, default=None)
            return Resolved(plan, resolved, pins, total, plan_hash)

    def window(self, data):
        keys(data, ('source_id', 'source_epoch', 'source_start_ms', 'source_end_ms'), ('source_id', 'source_epoch', 'source_start_ms', 'source_end_ms'))
        source = self.source(data['source_id'], data['source_epoch'])
        start, end = number(data['source_start_ms'], 'Window start'), number(data['source_end_ms'], 'Window end')
        if not 0 < end - start <= 10000:
            raise ValueError('Visual context windows are limited to ten seconds')
        frames = source.interval(start / 1000, end / 1000)
        # A bounded timestamped sequence includes lead-in/action/aftermath chosen by the requester.
        # Return actual images, never source names as substitutes for visual evidence.
        import base64
        count = min(24, len(frames))
        indices = sorted({round(i * (len(frames) - 1) / max(1, count - 1)) for i in range(count)})
        with self.lock:
            mappings = self.mappings.get((data['source_id'], source.epoch), [])
            mapping = mappings[-1] if mappings else None
            if mapping and mapping.source_path != source.path:
                mapping = None
            calibrated = mapping is not None and mapping.calibration_interval_ms[0] <= mapping.event(start) < mapping.event(end) <= mapping.calibration_interval_ms[1]
            def event_time(frame):
                if mapping:
                    value = mapping.event(frame.media_s * 1000)
                    if mapping.calibration_interval_ms[0] <= value <= mapping.calibration_interval_ms[1]:
                        return value
                return None
            return {'event_id': EVENT_ID, 'source_id': data['source_id'], 'source_epoch': source.epoch,
                    'source_interval_ms': [start, end], 'coverage': 'finalized immutable normalized frames; original chunks not used',
                    'mapping': asdict(mapping) if mapping else None, 'synchronization': 'calibrated interval only' if calibrated else 'unknown for the requested window',
                    'evidence_ids': [e['evidence_id'] for e in self.evidence.values()
                                     if mapping and e['source_id'] == data['source_id'] and e['source_epoch'] == source.epoch
                                     and e['mapping_revision'] == mapping.revision and e['status'] == 'active' and e['expires_at'] > time.time()],
                    'frames': [{'pts': frames[i].pts, 'time_base': frames[i].time_base, 'source_ms': frames[i].media_s * 1000,
                                'event_ms': event_time(frames[i]),
                                'jpeg_base64': base64.b64encode(frames[i].data).decode()} for i in indices]}

    def summary(self):
        with self.lock:
            return {'event_id': EVENT_ID, 'limits': {'tolerance_ms': self.tolerance_ms, 'max_shots': self.max_shots,
                    'max_duration_ms': self.max_duration_ms}, 'mappings': [asdict(m) for revisions in self.mappings.values() for m in revisions],
                    'evidence': list(self.evidence.values()), 'providers': 'blocked: no verified VAST, Cosmos, YOLO, search, or W&B/CoreWeave access'}

    def select_fixture(self, data):
        """Test-only evidence selection. Provider integration must replace this at the adapter boundary."""
        keys(data, ('fixture', 'scene_id', 'scene_revision', 'event_start_ms', 'event_end_ms', 'repeat'),
             ('fixture', 'scene_id', 'scene_revision', 'event_start_ms', 'event_end_ms', 'repeat'))
        if data['fixture'] is not True or type(data['repeat']) is not bool:
            raise ValueError('Only explicitly labeled fixture selection is available; live AI providers are blocked')
        start, end = number(data['event_start_ms'], 'Scene start'), number(data['event_end_ms'], 'Scene end')
        if not 200 <= end - start <= 6000:
            raise ValueError('Fixture scene window must be 0.2–6 seconds')
        with self.lock:
            candidates, rejected = [], []
            for ev in self.evidence.values():
                if ev['scene_id'] != data['scene_id'] or ev['scene_revision'] != data['scene_revision']:
                    continue
                try:
                    self.source(ev['source_id'], ev['source_epoch'])
                    mapping = self.mapping(ev['source_id'], ev['source_epoch'], ev['mapping_revision'])
                    mapping.check(start, end)
                    if ev['origin'] != 'fixture' or ev['status'] != 'active' or ev['expires_at'] <= time.time() or not ev['subject_visible'] or ev['quality'] != 'usable' or ev['event_start_ms'] > start or ev['event_end_ms'] < end:
                        raise ValueError('No usable fixture evidence for the complete action')
                    candidates.append(ev)
                except ValueError as error:
                    rejected.append(f"{ev['source_id']}: {error}")
            if not candidates:
                raise ValueError('Skip: no source supports the action; ' + '; '.join(rejected))
            candidates.sort(key=lambda e: (e['source_id'], e['evidence_id']))
            first = candidates[0]
            alternative = next((e for e in candidates[1:] if e['source_id'] != first['source_id'] and e['adds'] != first['adds']), None)
            def shot(ev, a, b, edit):
                return {'source_id': ev['source_id'], 'source_epoch': ev['source_epoch'], 'event_start_ms': a, 'event_end_ms': b,
                        'speed': 1, 'crop_normalized': [0, 0, 1, 1], 'evidence_ids': [ev['evidence_id']], 'edit': edit,
                        'reason': f"Fixture observation: {ev['action']}. This view adds: {ev['adds']}. Boundaries retain the supplied action window."}
            shots = [shot(first, start, end, 'continuous')]
            if alternative:
                if data['repeat']:
                    shots.append(shot(alternative, start, end, 'repeat'))
                else:
                    cut = (start + end) / 2
                    shots = [shot(first, start, cut, 'continuous'), shot(alternative, cut, end, 'continuous')]
            used = {eid for s in shots for eid in s['evidence_ids']}
            selected = [e for e in candidates if e['evidence_id'] in used]
            plan = {'schema_version': '1.1', 'plan_id': f'fixture-{time.time_ns()}', 'event_id': EVENT_ID, 'context_revision': 1,
                    'scene_id': data['scene_id'], 'scene_revision': data['scene_revision'], 'fixture': True,
                    'expires_at': min(e['expires_at'] for e in selected), 'source_mapping_revisions': {f"{e['source_id']}:{e['source_epoch']}": e['mapping_revision'] for e in selected},
                    'evidence_ids': sorted(used), 'evidence_revisions': {e['evidence_id']: e['revision'] for e in selected},
                    'shots': shots, 'transition': 'cut', 'audio_policy': 'mute_source_and_live_audio', 'replay_marker': True,
                    'score_overlay': 'hidden', 'output': {'width': self.cfg.width, 'height': self.cfg.height, 'fps_num': self.cfg.fps, 'fps_den': 1},
                    'selection_reason': ('Fixture alternate view adds supported information. ' if alternative else 'Fallback: only one view adds supported information. ') + '; '.join(rejected)}
            try:
                resolved = self.resolve(plan)
            except ValueError as error:
                if not alternative:
                    raise
                plan['selection_reason'] = f'Fallback to one camera: {error}'
                plan['shots'] = [shot(first, start, end, 'continuous')]
                plan['evidence_ids'] = [first['evidence_id']]
                plan['evidence_revisions'] = {first['evidence_id']: first['revision']}
                plan['source_mapping_revisions'] = {f"{first['source_id']}:{first['source_epoch']}": first['mapping_revision']}
                resolved = self.resolve(plan)
            try:
                return resolved.plan
            finally:
                resolved.release()


def render_plan(cfg, replay_id, resolved, cancelled=None):
    """Select frames by source timestamps, encode one constant-format cuts-only timeline."""
    from media import Replay, jpeg
    started = time.monotonic()
    path = cfg.runtime / 'replays' / f'{replay_id}.mp4'
    path.parent.mkdir(exist_ok=True)
    font = ImageFont.truetype(os.environ['BREADCAST_FONT'], max(12, cfg.width // 30))
    source_map = []
    boundary_images = {}
    def budget(maximum=30):
        if cancelled and cancelled.is_set():raise ValueError('Replay rendering canceled')
        remaining=maximum if resolved.deadline is None else min(maximum,resolved.deadline-time.time())
        if resolved.monotonic_deadline is not None:remaining=min(remaining,resolved.monotonic_deadline-time.monotonic())
        if remaining<=0:raise TimeoutError('deadline_missed')
        return remaining
    def run(command, *, stdin=None, maximum=30):
        # Polling keeps cancellation bounded and always reaps the owned process.
        from media import stop_process
        process=subprocess.Popen(command,stdin=stdin,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        end=time.monotonic()+budget(maximum)
        try:
            while True:
                budget(maximum)
                remaining=end-time.monotonic()
                if remaining<=0:raise TimeoutError('Replay subprocess timeout')
                try:
                    stdout,stderr=process.communicate(timeout=min(.1,remaining))
                    if process.returncode:raise subprocess.CalledProcessError(process.returncode,command,stdout,stderr)
                    return stdout
                except subprocess.TimeoutExpired:pass
        finally:
            stop_process(process)
    def images():
        output_index = 0
        for shot, frames in zip(resolved.shots, resolved.pins):
            ticks = [f.media_s for f in frames]
            first_index = output_index
            native_frames=[]
            # Each output timestamp belongs to exactly one half-open shot interval.
            # Round boundaries upward; reset source time from the ideal output start,
            # not from the rounded cut. This avoids repeating action at fractional cuts.
            last_index = math.ceil(shot['output_end_ms'] * cfg.fps / 1000 - 1e-9)
            x, y, w, h = shot['crop_normalized']
            crop = (round(x * cfg.width), round(y * cfg.height), round((x + w) * cfg.width), round((y + h) * cfg.height))
            for index in range(first_index, last_index):
                budget()
                target = shot['source_start_ms'] / 1000 + (index / cfg.fps - shot['output_start_ms'] / 1000) * shot['speed']
                pos = bisect_right(ticks, min(target + 1e-9, shot['source_end_ms'] / 1000 - 1e-9)) - 1
                duration=getattr(frames[pos],'duration_s',1/cfg.fps) if pos>=0 else 0
                if pos < 0 or target - ticks[pos] >= duration+1e-6:
                    raise ValueError('Source timestamp coverage is missing; cannot fabricate a frame')
                native_frames.append({'output_frame':index,'native':frames[pos].native_provenance,
                    'source_sequence':frames[pos].sequence})
                with Image.open(io.BytesIO(frames[pos].data)) as image:
                    image = image.convert('RGB').crop(crop).resize((cfg.width, cfg.height), Image.Resampling.LANCZOS)
                draw = ImageDraw.Draw(image)
                label = f"REPLAY  {shot['speed']:g}x" + ('  ALTERNATE ANGLE' if shot['edit'] == 'repeat' else '')
                box = draw.textbbox((0, 0), label, font=font)
                draw.rectangle((8, 8, min(cfg.width, box[2] + 28), box[3] + 20), fill='#253028')
                draw.text((18, 12), label, font=font, fill='#EFA845')
                if index in (first_index, last_index - 1):
                    boundary_images[index] = image.copy()
                yield jpeg(image)
            output_index = last_index
            source_map.append({**shot, 'output_frame_start': first_index, 'output_frame_end': last_index,
                               'native_frames':native_frames,
                               'actual_output_start_ms': first_index * 1000 / cfg.fps, 'actual_output_end_ms': last_index * 1000 / cfg.fps,
                               'crop_transform': {'input_space': 'oriented normalized program frame', 'input_dimensions': [cfg.width, cfg.height],
                                                  'crop_pixels_ltrb': list(crop), 'output_dimensions': [cfg.width, cfg.height]}})
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'image2pipe', '-framerate', str(cfg.fps), '-i', 'pipe:0',
               '-an', '-filter_threads', '1', '-vf', 'scale=in_range=pc:out_range=tv,format=yuv420p',
               '-color_range', 'tv', '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-bf', '0',
               '-movflags', '+faststart', str(path)]
    # Temporary input bounds memory and makes subprocess timeout/cancellation cleanup reliable.
    import tempfile
    try:
        with tempfile.TemporaryFile() as stream:
            written=0
            for data in images():
                written+=len(data)
                if written>resolved.scratch_bytes:raise ValueError('capacity_reached: replay scratch')
                stream.write(data)
            stream.seek(0)
            run(command,stdin=stream)
        if cancelled and cancelled.is_set():
            raise ValueError('Replay rendering canceled')
        probe = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-show_frames', '-of', 'json', str(path)],maximum=15))
        stream = probe['streams'][0]
        pts = [float(f['best_effort_timestamp_time']) for f in probe['frames']]
        measured = float(probe['format']['duration'])
        expected_count = math.ceil(resolved.expected_ms * cfg.fps / 1000 - 1e-9)
        if len(probe['streams']) != 1 or stream['codec_name'] != 'h264' or stream['pix_fmt'] != 'yuv420p' or (stream['width'], stream['height']) != (cfg.width, cfg.height) or Fraction(stream['avg_frame_rate']) != cfg.fps:
            raise ValueError('Rendered format or audio policy is invalid')
        if len(pts) != expected_count or abs(measured * 1000 - resolved.expected_ms) > 1000 / cfg.fps + 2 or not pts or abs(pts[0]) > .001 or any(abs(b - a - 1 / cfg.fps) > .00001 for a, b in zip(pts, pts[1:])):
            raise ValueError('Rendered duration or continuous presentation timestamps failed')
        import av
        decoded = []
        blank_frames = []
        boundary_errors = {}
        with av.open(str(path)) as media:
            for i,frame in enumerate(media.decode(video=0)):
                budget()
                image=frame.to_image().convert('RGB')
                if i in boundary_images:
                    error = max(ImageStat.Stat(ImageChops.difference(image, boundary_images[i])).mean)
                    boundary_errors[i] = error
                    if error > 15:
                        raise ValueError('Encoded camera order or cut-boundary image does not match its pinned source')
                if max(ImageStat.Stat(image.crop((0, cfg.height // 3, cfg.width, cfg.height))).mean) < 2:
                    blank_frames.append(i)
                decoded.append(jpeg(image))
        if len(decoded)!=expected_count:raise ValueError('Replay did not fully decode')
        if blank_frames:
            raise ValueError(f'Unexpected black image content at output frames {blank_frames[:8]}')
        report = {'schema_version': resolved.plan['schema_version'], 'plan': resolved.plan, 'plan_hash': resolved.plan_hash,
                  'render_configuration': {'width': cfg.width, 'height': cfg.height, 'fps': cfg.fps, 'codec': 'h264', 'pixel_format': 'yuv420p', 'transition': 'cut'},
                  'source_map': source_map, 'planned_duration_s': resolved.expected_ms / 1000, 'measured_duration_s': measured,
                  'output_frames': expected_count, 'continuous_pts': True, 'decode_passed': True, 'first_last_frames_valid': True,
                  'black_frames': blank_frames, 'camera_order': [s['source_id'] for s in source_map],
                  'camera_order_check': 'decoded shot first/last images compared to timestamp-selected pinned source images',
                  'boundary_image_mean_errors': boundary_errors,
                  'audio_policy': 'mute_source_and_live_audio', 'render_s': time.monotonic() - started,
                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if resolved.plan.get('schema_version')=='1.2':
            report['evidence_origins']=sorted({origin for shot in source_map for origin in shot['evidence_origins']})
        if resolved.plan.get('timing_mode') == 'source_only':
            shot, frames = source_map[0], resolved.pins[0]
            left, top, right, bottom = shot['crop_transform']['crop_pixels_ltrb']
            report.update(experiment_schema=1, id=replay_id, source_path=shot['retained_media']['source_path'],
                          source_epoch=shot['source_epoch'], source_frame_range=shot['retained_media']['sequence'],
                          clock='normalized proxy PTS; capture mapping unknown',
                          normalized_media_range_s=[shot['source_start_ms'] / 1000, shot['source_end_ms'] / 1000],
                          normalized_pts_start=frames[0].pts, normalized_time_base=frames[0].time_base,
                          receipt_start_s=frames[0].received, receipt_end_s=frames[-1].received + 1 / cfg.fps,
                          speed=shot['speed'], crop_pixels=[left, top, right-left, bottom-top])
        path.with_suffix('.json').write_text(json.dumps(report, indent=2) + '\n')
        first = source_map[0]
        slot=first['source_slot'] if 'source_slot' in first else int(first['source_id'][7:])
        return Replay(replay_id, slot, first['source_epoch'], decoded, measured,
                      first['speed'], path, report)
    except BaseException:
        path.unlink(missing_ok=True)
        path.with_suffix('.json').unlink(missing_ok=True)
        raise
    finally:
        resolved.release()
