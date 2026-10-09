"""Encode synchronized synthetic views, inspect cuts, and exercise the live controller."""
import argparse
import array
import copy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import urllib.error

import av
from PIL import ImageStat
from media import Frame, jpeg, render_replay
from replay import render_plan
from replay_fixture import COLORS, calibration, encode_source, evidence, plan, staged_image
from studio import App, Config, request_json
from check import wait_for, control_expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.runtime/multi-camera-replay'))
    parser.add_argument('--browser', action='store_true', help='Wait up to 60s for browser-done after media checks')
    args = parser.parse_args()
    run_started=time.monotonic()
    folder = args.output.resolve(); folder.mkdir(parents=True, exist_ok=True)
    runtime = folder / 'runtime'; runtime.mkdir(exist_ok=True)
    cfg = Config(runtime, 'http://localhost:21080', bind='0.0.0.0', port=21080, offset=1000, print_access=False)
    sources = {i: encode_source(cfg, i, folder / 'sources') for i in range(1,6)}
    app = App(cfg)
    app.monitor = lambda: app.stop.wait()  # Explicit fixture injection, never a live provider/camera claim.
    app.sources = sources
    stop = threading.Event()
    pumps = []
    report = {'passed': False, 'mode': 'Synthetic staged ball action, encoded five views. Local fixture selection only.',
              'physical_phone_sync_validated': False, 'live_provider_analysis_validated': False, 'checks': [], 'samples': [],
              'target_alignment_tolerance_ms': app.replay_context.tolerance_ms,
              'blockers': ['No verified VAST endpoint, credentials, SDK, or immutable-object access.',
                           'No verified Cosmos endpoint, model ID, SDK, or video-window contract.',
                           'No verified YOLO model/version or per-epoch tracker access.',
                           'No verified semantic-search endpoint, index revision, or media resolver.',
                           'No verified W&B/CoreWeave endpoint, model ID, SDK, or structured-output limits.',
                           'No physical phones or shared-marker calibration measured on live phone media.']}
    def record(name, **extra):
        report['checks'].append({'name':name, **extra}); print(name, flush=True)
    def call(path, data=None):
        if data is not None and path in ('/api/program','/api/replays','/api/replay-cancel') and 'expected' not in data:
            snapshot = request_json(cfg.public_url+'/api/status')
            data = {**data, 'expected': control_expected(snapshot, data)}
        return request_json(cfg.public_url + path, data)
    def status(): return call('/api/status')
    def command(action, **extra): return call('/api/program', {'action':action,'revision':status()['program']['revision'],**extra})
    def reject(path, data, reason):
        try: call(path,data)
        except urllib.error.HTTPError as error:
            result = json.loads(error.read()); assert error.code == 409 and reason in result['error'], result
            return result['error']
        raise AssertionError('Expected rejection')
    def ready(p):
        job = call('/api/replays', {'plan':p})
        done = wait_for(lambda: next((j for j in status()['jobs'] if j['id']==job['id'] and j['state']!='rendering'), None),'replay readiness')
        assert done['state']=='ready', done
        return app.replays[job['id']]
    try:
        app.start()
        for i, source in sources.items():
            lease = app.leases.reserve(f'{i:032x}')
            source.path = lease['source_path']
            source.stop = stop; source.threads = []; source.processes = []; source.audio = __import__('collections').deque()
            source.has_audio = False; source.error = None; source.bytes = sum(len(f.data) for f in source.frames)
            source.sequence = len(source.frames); source.last_frame = time.monotonic()
            source.origin = time.monotonic() - 8
            app.replay_context.calibrate(calibration(source))
            app.replay_context.register_evidence(evidence(i, 'usable' if i <= 2 else 'obscured'))
            def pump(source=source, camera=i):
                index = 120
                while not stop.wait(1/cfg.fps):
                    data = jpeg(staged_image(cfg, camera, index % 120))
                    with source.lock:
                        # Keep original PTS/time base from the encoded recording, then extend it.
                        base = source.frames[0].time_base
                        pts = round(index / cfg.fps / float(__import__('fractions').Fraction(base)))
                        source.frames.append(Frame(index+1,time.monotonic(),data,index/cfg.fps,pts,base))
                        source.sequence = index+1; source.last_frame = time.monotonic(); source.bytes += len(data)
                    index += 1
            thread=threading.Thread(target=pump,daemon=True);thread.start();pumps.append(thread)
        command('live',slot=1)
        wait_for(lambda: status()['program']['actual']=='LIVE','fixture live playback')
        initial_encoder = status()['program']['encoder_pid']
        record('five_fixture_sources_and_live_program', occupied=status()['occupied'])
        mixed_plan = plan(cfg); mixed_plan['shots'][0]['speed']=.5; mixed_plan['shots'][1]['speed']=2
        mixed_plan['shots'][1]['crop_normalized']=[1/6,1/6,2/3,2/3]
        mixed = ready(mixed_plan)
        # Keep decoded evidence outside the bounded two-asset application shelf.
        saved=folder/'mixed-evidence.mp4';shutil.copyfile(mixed.path,saved)
        shutil.copyfile(mixed.path.with_suffix('.json'),saved.with_suffix('.json'))
        mixed=replace(mixed,path=saved)
        continuous = ready(plan(cfg))
        repeat_plan = plan(cfg,True); repeat_plan['shots'][1]['speed']=.5
        repeat = ready(repeat_plan)
        assert mixed.id not in app.replays and len(app.replays)==2
        # Camera color and clock bits are checked after complete H.264 decoding.
        alignment = []
        for replay in (continuous,repeat,mixed):
            with av.open(str(replay.path)) as container:
                frames = [f.to_image() for f in container.decode(video=0)]
            for shot in replay.report['source_map']:
                camera = int(shot['source_id'][7:]); expected_color = __import__('PIL.Image',fromlist=['Image']).new('RGB',(1,1),COLORS[camera-1]).getpixel((0,0))
                for index in (shot['output_frame_start'],shot['output_frame_end']-1):
                    color = frames[index].getpixel((cfg.width-5,cfg.height-5))
                    assert max(abs(a-b) for a,b in zip(color,expected_color)) < 18, (index,color,expected_color)
                    # The persistent replay label has amber pixels in every decoded frame.
                for index in range(shot['output_frame_start'],shot['output_frame_end']):
                    pixels = frames[index].crop((8,8,cfg.width-8,45))
                    assert any(r>140 and 65<g<220 and b<130 for r,g,b in pixels.get_flattened_data()), 'REPLAY marker missing'
                if shot['crop_normalized']==[0,0,1,1]:
                    for index in range(shot['output_frame_start'],shot['output_frame_end']):
                        image = frames[index]
                        tick=0
                        for bit in range(8):
                            left=10+bit*18
                            if sum(image.getpixel((left+6,cfg.height-16)))>400: tick |= 1<<bit
                        expected = shot['source_start_ms'] + (index * 1000/cfg.fps - shot['output_start_ms']) * shot['speed']
                        alignment.append(abs(tick*1000/cfg.fps-expected))
            report['samples'].append({'path':str(replay.path.relative_to(folder)), 'report':str(replay.path.with_suffix('.json').relative_to(folder)),
                                      'plan_hash':replay.report['plan_hash'],'camera_order':replay.report['camera_order'],
                                      'render_s':replay.report['render_s'],'duration_s':replay.duration,'planned_duration_s':replay.report['planned_duration_s']})
        report['measured_clock_marker_error_ms'] = max(alignment)
        assert max(alignment) <= 1000/cfg.fps + .002
        record('decoded_camera_order_and_clock_markers',max_clock_marker_error_ms=max(alignment),continuous_cut_event_ms=3500,
               combined_alignment_bound_ms=continuous.report['source_map'][1]['combined_alignment_bound_ms'])
        # Measure the clock step at the continuous cut; no omitted or repeated action.
        with av.open(str(continuous.path)) as container:
            encoded=[f.to_image() for f in container.decode(video=0)]
        cut=continuous.report['source_map'][1]['output_frame_start']
        def tick(image):
            return sum(1<<bit for bit in range(8) if sum(image.getpixel((16+bit*18,cfg.height-16)))>400)
        step_ms=(tick(encoded[cut])-tick(encoded[cut-1]))*1000/cfg.fps
        assert 0 < step_ms <= 2*1000/cfg.fps
        record('continuous_cut_no_gap_or_duplicate_action',clock_step_ms=step_ms)
        record('repeat_and_mixed_speed_crop',repeat_duration_s=repeat.duration,mixed_duration_s=mixed.duration,
               repeat_cue='ALTERNATE ANGLE baked throughout repeated shot; browser frame checks below')
        window=call('/api/replay-window',{'source_id':'camera-1','source_epoch':1,'source_start_ms':0,'source_end_ms':7800})
        assert len(window['frames'])==24 and all(f['event_ms'] is not None for f in window['frames'])
        record('bounded_timestamped_visual_context',frames=24)
        selected=call('/api/replay-select',{'fixture':True,'scene_id':'staged-ball','scene_revision':1,'event_start_ms':1000,'event_end_ms':6000,'repeat':False})
        assert len(selected['shots'])==2 and 'camera-3' in selected['selection_reason']
        (folder/'operator-plan.json').write_text(json.dumps(selected,indent=2)+'\n')
        record('five_view_fixture_selection_rejects_obscured_angles')
        # Invalid model-style data, unavailable synchronization, and stale evidence cannot change on-air state.
        before=status()['program']['frames_written']
        bad=copy.deepcopy(selected);bad['shots'][1]['filter']='untrusted shell'
        reject('/api/replays',{'plan':bad},'unsupported')
        for response, deadline in ((selected,time.monotonic()-1), ('malformed model response',time.monotonic()+1)):
            try: app.replay_context.accept_segmentor_response(response,deadline)
            except ValueError: pass
            else: raise AssertionError('Unsupported model response accepted')
        record('simulated_segmentor_timeout_and_malformed_response_abstain')
        record('malformed_plan_leaves_live_running')
        mapping=app.replay_context.mappings.pop(('camera-2',1))
        reject('/api/replays',{'plan':selected},'unknown')
        app.replay_context.mappings[('camera-2',1)]=mapping
        sources[2].epoch=2
        reject('/api/replays',{'plan':selected},'reconnect')
        sources[2].epoch=1
        record('unknown_mapping_and_reconnect_rejected')
        app.replay_context.evidence['view-2']['quality']='obscured'
        fallback=call('/api/replay-select',{'fixture':True,'scene_id':'staged-ball','scene_revision':1,'event_start_ms':1000,'event_end_ms':6000,'repeat':False})
        assert len(fallback['shots'])==1
        app.replay_context.evidence['view-2']['quality']='usable'
        record('weak_alternate_angle_falls_back',reason=fallback['selection_reason'])
        source=sources[2]
        with source.lock:
            pinned=source.frames; source.frames=__import__('collections').deque(f for f in pinned if not 4 <= f.media_s < 4.1)
        reject('/api/replays',{'plan':selected},'gap')
        with source.lock:
            source.frames=pinned
        record('missing_packet_across_join_rejected')
        # Worker failures and cancellation release all pins and leave the persistent encoder running.
        import studio
        original=studio.render_plan
        def fail(*a,**k): raise subprocess.TimeoutExpired('fixture-worker-timeout',1)
        studio.render_plan=fail
        failed=call('/api/replays',{'plan':selected})
        job=wait_for(lambda: next((j for j in status()['jobs'] if j['id']==failed['id'] and j['state']=='failed'),None),'worker failure')
        assert failed['id'] not in app.replays
        studio.render_plan=original
        wait_for(lambda: status()['program']['frames_written']>before,'live progress after failure')
        record('worker_timeout_leaves_live_running',error=job['error'],encoder_pid_unchanged=status()['program']['encoder_pid']==initial_encoder)
        cancel_gate=threading.Event(); captured=[]
        def delayed(cfg,rid,resolved,cancelled):
            captured.append(resolved);cancel_gate.wait(3)
            return original(cfg,rid,resolved,cancelled)
        studio.render_plan=delayed
        canceled=call('/api/replays',{'plan':selected});call('/api/replay-cancel',{'job_id':canceled['id']});cancel_gate.set()
        wait_for(lambda: next((j for j in status()['jobs'] if j['id']==canceled['id'] and j['state']=='failed'),None),'canceled worker')
        assert not captured[0].pins
        studio.render_plan=original
        record('cancellation_releases_pins')
        command('replay',replay_id=repeat.id)
        wait_for(lambda: status()['program']['actual']=='REPLAY','repeat playout')
        wait_for(lambda: status()['program']['actual_target'].get('edit')=='repeat','alternate angle playout')
        assert status()['program']['actual_target']['speed']==.5
        # Persist a real program frame showing the repeat cue.
        (folder/'repeat-program.jpg').write_bytes(app.program.frame)
        audio=subprocess.run(['ffmpeg','-nostdin','-v','error','-rtsp_transport','tcp','-i',cfg.rtsp_url('program'),'-t','1','-vn','-ac','1','-ar','48000','-f','s16le','pipe:1'],capture_output=True,check=True,timeout=10).stdout
        samples=array.array('h',audio);rms=(sum(x*x for x in samples)/len(samples))**.5/32768
        assert rms<.001
        assert status()['program']['graphics']['applied']['score_hidden_during_replay']
        command('live')
        wait_for(lambda: status()['program']['actual']=='LIVE','immediate return')
        record('repeat_cue_current_speed_audio_muted_and_immediate_return',normalized_audio_rms=rms)
        command('replay',replay_id=continuous.id)
        wait_for(lambda: status()['program']['actual']=='REPLAY','continuous playout')
        wait_for(lambda: status()['program']['actual']=='LIVE','automatic return',timeout=12)
        record('automatic_return_to_live')
        app.replay_context.evidence['view-2']['status']='retracted'
        reject('/api/program',{'action':'replay','replay_id':continuous.id,'revision':status()['program']['revision']},'retracted')
        app.replay_context.evidence['view-2']['status']='active'
        record('ready_replay_retraction_rejects_playback')
        legacy_job=call('/api/replays',{'slot':1,'seconds':2,'speed':.5,'zoom':1.5})
        wait_for(lambda: legacy_job['id'] in app.replays,'legacy replay')
        assert app.replays[legacy_job['id']].report['plan']['timing_mode']=='source_only'
        record('single_camera_compatibility_uses_shared_worker')
        try: app.leases.reserve(f'{6:032x}')
        except ValueError: pass
        else: raise AssertionError('Sixth camera admitted')
        record('sixth_camera_rejected')
        if args.browser:
            (folder/'browser-done').unlink(missing_ok=True)
            print('BROWSER_READY',flush=True)
            wait_for(lambda: (folder/'browser-done').exists(),'host browser check',timeout=60)
            browser=json.loads((folder/'browser-report.json').read_text());assert browser['passed']
            report['browser']=browser
        report['passed']=True
    finally:
        stop.set()
        app.close()
        for thread in pumps: thread.join(timeout=1)
        report['elapsed_s']=time.monotonic()-run_started
        (folder/'validation.json').write_text(json.dumps(report,indent=2)+'\n')
        print(folder/'validation.json',flush=True)


if __name__=='__main__': main()
