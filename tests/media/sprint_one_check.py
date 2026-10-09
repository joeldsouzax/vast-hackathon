"""Required S01 smoke: one labeled synthetic camera through real local media."""
from __future__ import annotations

import argparse
import array
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
import traceback
import uuid

import av

from check import wait_for
from media import stop_process
from studio import App, Config, request_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence',type=Path,default=Path('/evidence'))
    parser.add_argument('--seconds',type=int,default=5)
    parser.add_argument('--port',type=int,default=20080)
    parser.add_argument('--port-offset',type=int,default=14000)
    args=parser.parse_args()
    if not 3<=args.seconds<=30:parser.error('Seconds must be 3–30 for this bounded smoke')
    run_started=time.monotonic()
    folder=(args.evidence/uuid.uuid4().hex).resolve()
    folder.mkdir(parents=True,mode=0o700)
    runtime=folder/'runtime';runtime.mkdir(mode=0o700)
    report={'sprint':'S01','passed':False,'physical_phone_verified':False,
        'scope':'One labeled synthetic RTSP camera through real local MediaMTX and FFmpeg; no browser, physical-phone, or provider proof',
        'checks':[],'failure':None}
    app=publisher=None
    def record(name,**values):
        report['checks'].append({'name':name,**values})
        print(name,flush=True)
    try:
        for binary in ('ffmpeg','mediamtx'):
            if not shutil.which(binary):raise RuntimeError(f'Required binary is missing from PATH: {binary}')
        font=Path(os.environ['BREADCAST_FONT']).resolve()
        if not font.is_file():raise RuntimeError('BREADCAST_FONT must name a readable font file')
        cfg=Config(runtime,f'http://127.0.0.1:{args.port}',port=args.port,
            offset=args.port_offset,print_access=False,delay=3)
        app=App(cfg);app.start()
        wait_for(lambda:app.program.frames_written>5,'holding encoder advancing')
        assert app.program.status()['actual']=='HOLDING',app.program.status()
        assert app.control.snapshot()['program_started'] is False,app.control.snapshot()
        encoder_pid=app.program.status()['encoder_pid']
        record('starts_in_holding_until_human_start',encoder_pid=encoder_pid)
        lease=request_json(cfg.public_url+'/api/leases',{'code':app.join_code,'client':uuid.uuid4().hex})
        command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-re','-f','lavfi','-i',
            f'color=c=0x28784e:size={cfg.width}x{cfg.height}:rate={cfg.fps}',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
            '-filter_threads','1','-vf',f"drawtext=fontfile={font}:text='S01 SYNTHETIC CAMERA - TEST TONE':x=20:y=80:fontsize=20:fontcolor=white",
            '-c:v','libx264','-threads','1','-preset','ultrafast','-tune','zerolatency',
            '-profile:v','baseline','-pix_fmt','yuv420p','-bf','0','-g',str(cfg.fps),
            '-c:a','libopus','-ac','1','-f','rtsp','-rtsp_transport','tcp',
            cfg.rtsp_url(lease['source_path'],lease['token'])]
        with (folder/'publisher.log').open('wb') as log:
            publisher=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=log)
        wait_for(lambda:app.get_source(lease['slot']) and
            app.get_source(lease['slot']).status()['buffer_ready'] and
            app.get_source(lease['slot']).has_audio,'one camera video and audio buffer',timeout=30)
        assert publisher.poll() is None,'Synthetic publisher exited'
        assert app.program.status()['actual']=='HOLDING'
        assert app.control.snapshot()['program_started'] is False
        record('camera_arrival_does_not_start_program',occupied=len(app.leases.rows()))
        action={'id':uuid.uuid4().hex,'op':'live','args':{'slot':lease['slot']},
            'expected':app.control.expected({'slot':lease['slot']})}
        command_started=time.monotonic()
        result=request_json(cfg.public_url+'/api/actions',action)
        assert result['state'] in ('Applying','Finished'),result
        wait_for(lambda:app.program.status()['actual']=='LIVE','human Start applied')
        assert app.control.snapshot()['program_started'] is True
        record('explicit_human_start_applies_live',application_s=time.monotonic()-command_started)
        output=folder/'program.mkv'
        before=app.program.frames_written
        subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-rtsp_transport','tcp',
            '-i',cfg.rtsp_url('program'),'-t',str(args.seconds),'-c','copy',str(output)],
            check=True,capture_output=True,timeout=args.seconds+20)
        assert app.program.frames_written>before
        assert app.program.status()['encoder_pid']==encoder_pid,'Encoder restarted'
        assert app.program.status()['actual']=='LIVE'
        timestamps=[];source_color_frames=0
        with av.open(str(output)) as container:
            for frame in container.decode(video=0):
                timestamps.append(float(frame.pts*frame.time_base))
                red,green,blue=frame.to_image().convert('RGB').getpixel((cfg.width//2,cfg.height//2))
                if max(abs(red-40),abs(green-120),abs(blue-78))<=25:source_color_frames+=1
        assert len(timestamps)>=args.seconds*cfg.fps*.8,(len(timestamps),args.seconds)
        gaps=[b-a for a,b in zip(timestamps,timestamps[1:])]
        assert gaps and min(gaps)>0 and max(gaps)<=.2,gaps
        assert timestamps[-1]-timestamps[0]>=args.seconds-.3,timestamps
        assert source_color_frames>=len(timestamps)*.95,source_color_frames
        record('decoded_live_video_advances',frames=len(timestamps),source_color_frames=source_color_frames,
            decoded_duration_s=timestamps[-1]-timestamps[0],max_frame_gap_s=max(gaps),encoder_pid=encoder_pid)
        pcm=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(output),'-vn',
            '-ac','1','-ar','48000','-f','s16le','pipe:1'],capture_output=True,check=True,timeout=15).stdout
        samples=array.array('h',pcm)
        assert samples,'Program output has no decoded audio'
        rms=math.sqrt(sum(sample*sample for sample in samples)/len(samples))/32768
        assert rms>.01,rms
        record('source_test_tone_reaches_program',decoded_samples=len(samples),normalized_rms=rms)
        report['versions']=json.loads((runtime/'versions.json').read_text())
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'reason':str(error),'traceback':traceback.format_exc()}
    finally:
        stop_process(publisher)
        if app is not None:
            try:app.close()
            except BaseException as error:
                report['passed']=False
                report['cleanup_failure']={'type':type(error).__name__,'reason':str(error)}
            cleanup=app.closed and (app.gateway_process is None or app.gateway_process.poll() is not None)
            cleanup=cleanup and (app.program.encoder is None or app.program.encoder.poll() is not None)
            with app.sources_lock:sources=list(app.sources.values())
            for source in sources:
                with source.lock:processes=list(source.processes)
                cleanup=cleanup and all(process.poll() is not None for process in processes)
            report['cleanup_passed']=cleanup and (publisher is None or publisher.poll() is not None)
            if not report['cleanup_passed']:report['passed']=False
        else:report['cleanup_passed']=publisher is None or publisher.poll() is not None
        report['elapsed_s']=time.monotonic()-run_started
        (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print('One-camera report: '+str(folder/'report.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
