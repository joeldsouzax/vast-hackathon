"""Required S02 smoke: five synthetic cameras and three independent RTSP readers."""
from __future__ import annotations

import argparse
import array
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import traceback
import urllib.error
import uuid

import av

from check import wait_for
from media import stop_process
from studio import App, Config, request_json

COLORS=((40,120,78),(150,50,70),(45,75,160),(160,125,35),(125,55,150))


def publisher_command(cfg, path, token, slot, font):
    red,green,blue=COLORS[slot-1]
    return ['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-re','-f','lavfi','-i',
        f'color=c=0x{red:02x}{green:02x}{blue:02x}:size={cfg.width}x{cfg.height}:rate={cfg.fps}',
        '-f','lavfi','-i',f'sine=frequency={330+slot*110}:sample_rate=48000',
        '-filter_threads','1','-vf',f"drawtext=fontfile={font}:text='S02 SYNTHETIC CAMERA {slot} - TEST TONE':x=20:y=80:fontsize=20:fontcolor=white",
        '-c:v','libx264','-threads','1','-preset','ultrafast','-tune','zerolatency',
        '-profile:v','baseline','-pix_fmt','yuv420p','-bf','0','-g',str(cfg.fps),
        '-c:a','libopus','-ac','1','-f','rtsp','-rtsp_transport','tcp',cfg.rtsp_url(path,token)]


def reader_command(cfg, output, seconds):
    # Program has a one-second GOP. Budget one bounded startup GOP before scoring.
    return ['ffmpeg','-nostdin','-v','error','-y','-rtsp_transport','tcp',
        '-i',cfg.rtsp_url('program'),'-t',str(seconds+1),'-c','copy',str(output)]


def inspect_output(path, cfg, slot, seconds, microphone_slot=1):
    timestamps=[];matching=[];expected=COLORS[slot-1]
    with av.open(str(path)) as container:
        for frame in container.decode(video=0):
            timestamps.append(float(frame.pts*frame.time_base))
            actual=frame.to_image().convert('RGB').getpixel((cfg.width//2,cfg.height//2))
            matching.append(max(abs(a-b) for a,b in zip(actual,expected))<=25)
    assert timestamps,'Program output has no decoded video'
    first_video_pts=timestamps[0]
    assert 0<=first_video_pts<=1.2,(path,first_video_pts)
    gaps=[b-a for a,b in zip(timestamps,timestamps[1:])]
    assert gaps and min(gaps)>0 and max(gaps)<=.2,(path,gaps)
    scored=[stamp for stamp in timestamps if stamp>=timestamps[-1]-seconds]
    assert len(scored)>=seconds*cfg.fps*.8,(path,len(scored))
    assert scored[-1]-scored[0]>=seconds-.3,(path,scored)
    # A reader may join before the next IDR. The final second must show the cut.
    tail=matching[-cfg.fps:]
    assert sum(tail)>=len(tail)*.95,(path,matching)
    pcm=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(path),'-vn','-ac','1',
        '-ar','48000','-f','s16le','pipe:1'],capture_output=True,check=True,timeout=15).stdout
    samples=array.array('h',pcm)[-48000:]
    assert samples,'Decoded program audio is missing'
    rms=math.sqrt(sum(value*value for value in samples)/len(samples))/32768
    assert rms>.01,(path,rms)
    rising=sum(a<=0<b for a,b in zip(samples,samples[1:]))
    frequency=rising*48000/len(samples)
    assert abs(frequency-(330+microphone_slot*110))<=10,(path,frequency)
    return {'frames':len(scored),'duration_s':scored[-1]-scored[0],
        'first_decoded_video_pts_s':first_video_pts,'startup_budget_s':1.2,
        'capture_budget_s':seconds+1,'scored_interval_s':seconds,
        'max_frame_gap_s':max(gaps),'selected_source_last_second_frames':sum(tail),
        'normalized_audio_rms':rms,'test_tone_hz':frequency,'output':str(path)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence',type=Path,default=Path('/evidence'))
    parser.add_argument('--seconds',type=int,default=5)
    parser.add_argument('--port',type=int,default=22080)
    parser.add_argument('--port-offset',type=int,default=17000)
    args=parser.parse_args()
    if not 5<=args.seconds<=30:parser.error('Seconds must be 5–30 for this bounded smoke')
    run_started=time.monotonic();folder=(args.evidence/uuid.uuid4().hex).resolve()
    folder.mkdir(parents=True,mode=0o700);runtime=folder/'runtime';runtime.mkdir(mode=0o700)
    report={'sprint':'S02','passed':False,'physical_five_phone_verified':False,
        'browser_whep_verified':False,'physical_15_minute_verified':False,
        'scope':'Five labeled synthetic RTSP cameras and three separate software RTSP readers; no browser, phone, or provider proof',
        'checks':[],'failure':None}
    app=None;publishers=[];readers=[];source_processes=[]
    def record(name,**values):
        report['checks'].append({'name':name,**values});print(name,flush=True)
    try:
        for binary in ('ffmpeg','mediamtx'):
            if not shutil.which(binary):raise RuntimeError(f'Required binary is missing from PATH: {binary}')
        font=Path(os.environ['BREADCAST_FONT']).resolve()
        if not font.is_file():raise RuntimeError('BREADCAST_FONT must name a readable font file')
        cfg=Config(runtime,f'http://127.0.0.1:{args.port}',port=args.port,
            offset=args.port_offset,print_access=False,delay=3)
        app=App(cfg);app.start();wait_for(lambda:app.program.frames_written>5,'holding encoder')
        assert app.program.status()['actual']=='HOLDING' and not app.control.program_started
        encoder_pid=app.program.status()['encoder_pid']
        def reserve():
            try:return {'accepted':True,'lease':request_json(cfg.public_url+'/api/leases',{'code':app.join_code,'client':uuid.uuid4().hex})}
            except urllib.error.HTTPError as error:
                return {'accepted':False,'status':error.code}
        leases=[reserve()['lease'] for _ in range(4)]
        with ThreadPoolExecutor(max_workers=2) as workers:race=list(workers.map(lambda _:reserve(),range(2)))
        assert sum(row['accepted'] for row in race)==1,race
        assert next(row for row in race if not row['accepted'])['status']==409,race
        leases.extend(row['lease'] for row in race if row['accepted']);leases.sort(key=lambda lease:lease['slot'])
        assert len(app.leases.rows())==5
        record('http_last_slot_race_admits_only_one',accepted=1,rejected=1,occupied=5)
        def publish(lease,label):
            with (folder/f'{label}.log').open('wb') as log:
                process=subprocess.Popen(publisher_command(cfg,lease['source_path'],lease['token'],lease['slot'],font),
                    stdout=subprocess.DEVNULL,stderr=log)
            publishers.append(process);return process
        for lease in leases:publish(lease,f'publisher-{lease["slot"]}')
        def five_ready():
            return len(app.leases.rows())==5 and all(app.get_source(lease['slot']) and
                app.get_source(lease['slot']).status()['buffer_ready'] and app.get_source(lease['slot']).has_audio for lease in leases)
        wait_for(five_ready,'five camera media buffers',timeout=40)
        assert all(process.poll() is None for process in publishers)
        assert app.program.status()['actual']=='HOLDING' and not app.control.program_started
        record('five_sources_join_off_air',sources=5)
        def denied_publish(path,token,label):
            with (folder/f'{label}.log').open('wb') as log:
                process=subprocess.Popen(publisher_command(cfg,path,token,1,font),stdout=subprocess.DEVNULL,stderr=log)
            publishers.append(process)
            try:code=process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                stop_process(process);raise AssertionError('Unauthorized publisher stayed running') from None
            assert code!=0,'Unauthorized publisher was accepted'
            return code
        denied_publish('camera/'+uuid.uuid4().hex,leases[0]['token'],'sixth-direct-publisher')
        assert len(app.leases.rows())==5
        record('sixth_direct_publisher_rejected',occupied=5)
        def action(op,**values):
            request={'id':uuid.uuid4().hex,'op':op,'args':values,'expected':app.control.expected(values)}
            response=request_json(cfg.public_url+'/api/actions',request)
            assert response['state'] in ('Applying','Finished'),response
            wait_for(lambda:app.program.status()['applied_revision']==response['program_revision'],'command encoded')
        action('audio',slot=1)
        microphone=leases[0]['source_path']
        for lease in leases:
            action('live',slot=lease['slot'],independent=True)
            assert app.program.status()['audio_source_path']==microphone
            output=folder/f'cut-{lease["slot"]}.mkv'
            subprocess.run(reader_command(cfg,output,3),check=True,capture_output=True,timeout=23)
            record('manual_cut_decodes_selected_source',slot=lease['slot'],**inspect_output(output,cfg,lease['slot'],3))
        assert app.program.status()['encoder_pid']==encoder_pid
        outputs=[folder/f'rtsp-viewer-{index}.mkv' for index in range(1,4)]
        for index,output in enumerate(outputs,1):
            with (folder/f'rtsp-viewer-{index}.log').open('wb') as log:
                readers.append(subprocess.Popen(reader_command(cfg,output,args.seconds),stdout=subprocess.DEVNULL,stderr=log))
        for reader in readers:assert reader.wait(timeout=args.seconds+20)==0
        for index,output in enumerate(outputs,1):
            record('independent_rtsp_viewer_advances',viewer=index,transport='RTSP software reader',
                **inspect_output(output,cfg,5,args.seconds))
        assert five_ready()
        assert app.program.status()['encoder_pid']==encoder_pid
        # Remove an off-air camera, reuse its slot, and prove old publish authority is gone.
        removed=leases[1]
        request_json(cfg.public_url+f'/api/cameras/{removed["lease_id"]}/remove',{})
        stop_process(publishers[1]);replacement=reserve()['lease']
        assert replacement['slot']==removed['slot'] and replacement['source_path']!=removed['source_path']
        denied_publish(removed['source_path'],removed['token'],'old-token-publisher')
        publish(replacement,'replacement-publisher')
        wait_for(lambda:app.get_source(replacement['slot']) and
            app.get_source(replacement['slot']).path==replacement['source_path'] and
            app.get_source(replacement['slot']).status()['buffer_ready'],'replacement camera buffer',timeout=30)
        assert len(app.leases.rows())==5
        record('operator_remove_and_slot_reuse_fence_old_token',slot=replacement['slot'],old_token_rejected=True)
        report['versions']=json.loads((runtime/'versions.json').read_text())
        old_run=app.control.run_id;old_join=app.join_code
        for process in publishers:stop_process(process)
        with app.sources_lock:sources=list(app.sources.values())
        source_processes.extend(process for source in sources for process in source.processes)
        app.close()
        def web_closed():
            try:
                with socket.create_connection(('127.0.0.1',args.port),timeout=.2):return False
            except OSError:return True
        wait_for(web_closed,'old web listener closes',timeout=10)
        assert app.gateway_process.poll() is not None and app.program.encoder.poll() is not None
        app=App(cfg);app.start();wait_for(lambda:app.program.frames_written>5,'restarted holding encoder')
        assert app.program.status()['actual']=='HOLDING' and not app.control.program_started
        assert app.control.run_id!=old_run and app.join_code!=old_join and app.leases.rows()==[]
        record('restart_holds_and_clears_camera_authority',occupied=0,new_run=True,holding=True)
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'reason':str(error),'traceback':traceback.format_exc()}
    finally:
        for process in readers+publishers:stop_process(process)
        if app is not None:
            with app.sources_lock:sources=list(app.sources.values())
            source_processes.extend(process for source in sources for process in source.processes)
            try:app.close()
            except BaseException as error:
                report['passed']=False;report['cleanup_failure']={'type':type(error).__name__,'reason':str(error)}
        cleanup=all(process.poll() is not None for process in readers+publishers+source_processes)
        if app is not None:
            cleanup=cleanup and (app.gateway_process is None or app.gateway_process.poll() is not None)
            cleanup=cleanup and (app.program.encoder is None or app.program.encoder.poll() is not None)
        report['cleanup_passed']=cleanup
        if not cleanup:report['passed']=False
        report['elapsed_s']=time.monotonic()-run_started
        (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print('Five-camera report: '+str(folder/'report.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
