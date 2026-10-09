"""Synthetic staged action from five views. Labels and clocks are test instrumentation."""
from collections import deque
from fractions import Fraction
import io
from pathlib import Path
import subprocess
import threading
import time

import av
from PIL import Image, ImageDraw, ImageFont
from media import Frame, Source, jpeg

COLORS = ['#704436', '#285e76', '#426d38', '#70476d', '#75712d']


def staged_image(cfg, camera, index):
    image = Image.new('RGB', (cfg.width, cfg.height), COLORS[camera - 1])
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(__import__('os').environ['BREADCAST_FONT'], 18)
    t = index / cfg.fps
    draw.text((16, 70), f'SYNTHETIC VIEW {camera}  EVENT {t:06.3f}s', font=font, fill='white')
    # The same ball action has a wide view and a closer side view. No real identity/result.
    x = int(cfg.width * (.2 + .6 * min(1, max(0, (t - 1) / 5))))
    y = int(cfg.height * (.55 + .14 * __import__('math').sin(t * 2)))
    radius = 12 if camera == 1 else 22
    if camera <= 2:
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill='#f2ce45')
        draw.line((cfg.width*.15, cfg.height*.8, cfg.width*.85, cfg.height*.8), fill='white', width=2)
    else:
        draw.rectangle((cfg.width*.15, cfg.height*.3, cfg.width*.9, cfg.height*.8), fill='#464646')
        draw.text((cfg.width*.2, cfg.height*.5), 'ACTION OBSCURED', font=font, fill='white')
    # Readable frame-index marker allows encoded-output alignment measurement without OCR.
    for bit in range(8):
        left = 10 + bit * 18
        draw.rectangle((left, cfg.height-25, left+13, cfg.height-8), fill='white' if index & (1 << bit) else 'black')
    return image


def memory_source(cfg, camera, count=120):
    source = Source.__new__(Source)
    source.cfg, source.slot, source.epoch, source.path = cfg, camera, 1, f'fixture/camera-{camera}'
    source.lock = threading.RLock()
    source.stop = threading.Event(); source.threads = []; source.processes = []
    source.audio = deque(); source.has_audio = False; source.error = None
    source.origin = time.monotonic() - 8; source.last_frame = time.monotonic()
    source.sequence = count; source.bytes = 0
    source.frames = deque(Frame(i+1, 100+i/cfg.fps, jpeg(staged_image(cfg, camera, i)), i/cfg.fps, i, f'1/{cfg.fps}') for i in range(count))
    return source


def encode_source(cfg, camera, directory):
    path = Path(directory) / f'camera-{camera}.mp4'
    path.parent.mkdir(parents=True, exist_ok=True)
    count = cfg.fps * 8
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'image2pipe', '-framerate', str(cfg.fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', str(path)], input=b''.join(jpeg(staged_image(cfg, camera, i)) for i in range(count)), check=True, timeout=30)
    source = memory_source(cfg, camera, 0)
    with av.open(str(path)) as container:
        for i, frame in enumerate(container.decode(video=0)):
            source.frames.append(Frame(i+1, 100+float(frame.pts*frame.time_base), jpeg(frame.to_image()), float(frame.pts*frame.time_base), frame.pts, str(frame.time_base)))
    return source


def calibration(source, uncertainty=3):
    frames = tuple(source.frames)
    return {'source_id': f'camera-{source.slot}', 'source_epoch': source.epoch, 'uncertainty_ms': uncertainty,
            'markers': [{'pts': frames[i].pts, 'event_ms': frames[i].media_s * 1000, 'label': f'Shared visible clock {frames[i].media_s:.3f}s'} for i in (0, len(frames)//2, len(frames)-1)]}


def evidence(camera, quality='usable'):
    return {'evidence_id': f'view-{camera}', 'revision': 1, 'event_id': 'local-studio', 'scene_id': 'staged-ball', 'scene_revision': 1,
            'source_id': f'camera-{camera}', 'source_epoch': 1, 'mapping_revision': 1, 'event_start_ms': 0, 'event_end_ms': 7800,
            'action': 'A yellow ball moves across the frame' if quality == 'usable' else 'The action is hidden by a panel',
            'claim_kind': 'observation', 'subject_visible': quality == 'usable', 'quality': quality,
            'adds': 'Wide view establishes the path' if camera == 1 else 'Closer side view shows the ball movement',
            'origin': 'fixture', 'status': 'active', 'expires_at': time.time()+1800}


def plan(cfg, repeat=False):
    shots = [dict(source_id='camera-1', source_epoch=1, event_start_ms=1000, event_end_ms=3000 if repeat else 3500,
                  speed=1, crop_normalized=[0,0,1,1], evidence_ids=['view-1'], reason='The wide view establishes the visible ball path and lead-in.', edit='continuous'),
             dict(source_id='camera-2', source_epoch=1, event_start_ms=1000 if repeat else 3500, event_end_ms=3000 if repeat else 6000,
                  speed=1, crop_normalized=[0,0,1,1], evidence_ids=['view-2'], reason='The closer view shows the same ball movement and aftermath.', edit='repeat' if repeat else 'continuous')]
    return {'schema_version': '1.1', 'plan_id': 'staged-repeat' if repeat else 'staged-continuous', 'event_id': 'local-studio', 'context_revision': 1,
            'scene_id': 'staged-ball', 'scene_revision': 1, 'fixture': True, 'expires_at': time.time()+900,
            'source_mapping_revisions': {'camera-1:1':1, 'camera-2:1':1}, 'evidence_ids':['view-1','view-2'],
            'evidence_revisions': {'view-1':1,'view-2':1}, 'shots':shots, 'transition':'cut',
            'audio_policy':'mute_source_and_live_audio', 'replay_marker':True, 'score_overlay':'hidden',
            'output': {'width':cfg.width,'height':cfg.height,'fps_num':cfg.fps,'fps_den':1}}
