"""Read real encoded media from a running isolated studio; test graphics audio policy."""
import array
import json
import math
import os
from pathlib import Path
import subprocess
import time
import av
from studio import request_json
from check import control_expected

runtime = Path(os.environ['BREADCAST_RUNTIME'])
access = json.loads((runtime / 'access.json').read_text())
base = access['public_url']
output = runtime / 'graphics-evidence'; output.mkdir(exist_ok=True)

def state(): return request_json(base + '/api/status')
def wait(predicate, name, timeout=20):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        value = state()
        if predicate(value): return value
        time.sleep(.1)
    raise AssertionError(name)

def command(action, **extra):
    revision = state()['program']['revision']
    result = request_json(base + '/api/program', {'action': action, 'revision': revision, 'expected': control_expected(state(), extra), **extra})
    return wait(lambda s: s['program']['applied_revision'] == result['revision'], action)

def capture(name, seconds=3):
    path = output / (name + '.mkv')
    result = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                             '-rtsp_transport', 'tcp', '-i', f"rtsp://127.0.0.1:{access['rtsp_port']}/program",
                             '-t', str(seconds), '-c', 'copy', str(path)], capture_output=True, timeout=seconds+15)
    if result.returncode: raise AssertionError(result.stderr.decode())
    video_pts, samples = [], array.array('h')
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format='s16', layout='mono', rate=48000)
        for packet in container.demux():
            for frame in packet.decode():
                if isinstance(frame, av.VideoFrame):
                    assert (frame.width, frame.height) == (640, 360)
                    video_pts.append(float(frame.pts*frame.time_base))
                else:
                    for mono in resampler.resample(frame):
                        chunk = array.array('h'); chunk.frombytes(bytes(mono.planes[0])[:mono.samples*2]); samples.extend(chunk)
    assert len(video_pts) > 25, name
    gaps = [b-a for a, b in zip(video_pts, video_pts[1:])]
    # RTSP can adjust its clock mapping after the first keyframe. Preserve that
    # interval in the report; require the steady intervals to match 15 fps.
    assert gaps and min(gaps) > 0 and max(gaps) < .14, (name, max(gaps))
    assert max(gaps[1:]) <= 1/15+.002, (name, max(gaps[1:]))
    middle = samples[len(samples)//3:len(samples)*2//3]
    rms = math.sqrt(sum(v*v for v in middle)/len(middle))
    return {'file': str(path), 'decoded_video_frames': len(video_pts), 'max_video_gap_s': round(max(gaps), 6),
            'first_video_gap_s': round(gaps[0], 6), 'max_steady_video_gap_s': round(max(gaps[1:]), 6),
            'audio_samples': len(samples), 'middle_audio_rms': round(rms, 3)}

wait(lambda s: s['occupied'] == 5 and all(c.get('buffer_ready') for c in s['cameras']), 'Five source buffers')
pid = state()['program']['encoder_pid']
checks = []
try:
    command('live', slot=1, independent=True)
    live = capture('live'); assert live['middle_audio_rms'] > 100
    command('graphics', graphics={'op': 'cue', 'preset': 'opening'})
    wait(lambda s: s['program']['graphics']['applied']['covers_camera'], 'Full-screen frame')
    opening = capture('opening'); assert opening['middle_audio_rms'] < 3
    command('live')
    restored = capture('restored'); assert restored['middle_audio_rms'] > 100
    job = request_json(base + '/api/replays', {'slot': 1, 'seconds': 6, 'speed': 1, 'zoom': 1, 'expected': control_expected(state(), {'slot': 1})})
    ready = wait(lambda s: any(r['id'] == job['id'] for r in s['replays']), 'Rendered replay')
    command('replay', replay_id=job['id'])
    replay = capture('replay'); assert replay['middle_audio_rms'] < 3
    command('live')
    assert state()['program']['encoder_pid'] == pid
    assert state()['program']['graphics']['error'] is None
    checks = [live, opening, restored, replay]
    (output / 'graphics-media-report.json').write_text(json.dumps({'passed': True,
        'mode': 'Five generated camera fixtures and labeled test tones; real RTSP H264/Opus output',
        'checks': checks, 'encoder_pid_unchanged': True, 'physical_phone_tested': False}, indent=2)+'\n')
    print(json.dumps({'passed': True, 'checks': checks}), flush=True)
finally:
    command('live')
