"""Isolated PRD 1 foundation checks. Real media surrounds labeled provider results."""
from __future__ import annotations
import argparse
from collections import deque
from fractions import Fraction
import importlib.metadata
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

import av
from PIL import Image, ImageDraw
from foundation import Foundation
from foundation_records import FoundationSettings, SourceEpoch, Geometry, Interval
from foundation_storage import inspect_video
from media import Frame, jpeg, stop_process
from media_provenance import DecoderTrace
from studio import App, Config
from check import wait_for
from contract_check import CoverageResult,run_units

ROOT=Path(__file__).resolve().parents[2]


def marker_id(image):
    image=image.convert('RGB')
    value=0
    for bit in range(8):
        x=120+bit*50+20
        if sum(image.getpixel((x,210)))>400:value|=1<<bit
    return value


def marker_proof(folder):
    """Tolerance is set before evaluation: one output frame and one native frame.

    Decode pixel markers, calibrate the reader origin once, then compare every
    output marker against encoder submissions. Reader delivery is not airtime.
    """
    native=folder/'native-markers.mp4';proxy=folder/'proxy-markers.nut';output=folder/'program-markers.mkv'
    with av.open(str(native),'w') as container:
        stream=container.add_stream('libx264',rate=15);stream.width=640;stream.height=360;stream.pix_fmt='yuv420p'
        stream.options={'preset':'ultrafast','crf':'16'}
        for index in range(180):
            image=Image.new('RGB',(640,360),(50,120,70));draw=ImageDraw.Draw(image)
            for bit in range(8):
                draw.rectangle((120+bit*50,180,160+bit*50,240),fill='white' if index&(1<<bit) else 'black')
            frame=av.VideoFrame.from_image(image);frame.pts=index;frame.time_base=Fraction(1,15)
            for packet in stream.encode(frame):container.mux(packet)
        for packet in stream.encode():container.mux(packet)
    cmd=['ffmpeg','-nostdin','-y','-v','info','-copyts','-i',str(native),'-an','-filter_threads','1','-vf',
         'showinfo@native,fps=15,scale=640:360:force_original_aspect_ratio=decrease,showinfo@scaled,pad=640:360:(ow-iw)/2:(oh-ih)/2,showinfo@proxy',
         '-c:v','mjpeg','-threads','1','-q:v','5','-f','nut',str(proxy)]
    result=subprocess.run(cmd,capture_output=True,check=True,timeout=30)
    trace=DecoderTrace(15,640,360)
    for line in result.stderr.decode().splitlines():trace.accept(line)
    frames=[];errors=[]
    with av.open(str(proxy)) as container:
        for index,frame in enumerate(container.decode(video=0)):
            provenance=trace.take(index+1)
            if provenance is None:raise AssertionError('Proxy has no native mapping')
            observed=marker_id(frame.to_image())
            expected=round(provenance['native_pts']*float(Fraction(provenance['native_time_base']))*15)
            errors.append(abs(expected-observed))
            frames.append(Frame(index+1,0,jpeg(frame.to_image()),float(frame.pts*frame.time_base),frame.pts,str(frame.time_base),provenance))
    assert max(errors)==0,errors
    native_proxy_error=max(errors)
    # Real encoder, gateway, and program frame-selection code; immutable fixture source.
    runtime=folder/'marker-runtime';cfg=Config(runtime,'http://localhost:24080',port=24080,offset=20000,delay=0,print_access=False)
    runtime.mkdir(exist_ok=True);app=App(cfg);reader=None
    class MarkerSource:
        slot=1;path='camera/marker-fixture';epoch=1;has_audio=False
        def __init__(self):self.origin=time.monotonic();self.lock=threading.RLock();self.frames=deque(frames)
        def at(self,target):
            index=max(0,min(len(frames)-1,int((target-self.origin)*15)))
            frame=frames[index]
            return Frame(frame.sequence,self.origin+frame.media_s,frame.data,frame.media_s,frame.pts,frame.time_base,frame.native_provenance)
        def audio_at(self,target):return bytes(cfg.audio_size)
        def close(self):pass
    try:
        app.start();wait_for(lambda:app.program.frames_written>5,'marker encoder')
        source=MarkerSource();app.sources[1]=source
        action=app.control.submit({'id':'marker-live','op':'live','args':{'slot':1}})
        assert action['state']=='Applying',action
        reader=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-y','-rtsp_transport','tcp','-i',cfg.rtsp_url('program'),
            '-t','7','-c','copy',str(output)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        reader.communicate(timeout=20)
        assert reader.returncode==0
    finally:
        stop_process(reader);app.close()
    submitted=[frame for line in (runtime/'program-history.jsonl').read_text().splitlines()
        for record in [json.loads(line)] if record['kind']=='program_source_map'
        for frame in record['frames'] if frame['target']['kind']=='camera']
    predicted=[round(f['target']['native']['native_pts']*float(Fraction(f['target']['native']['native_time_base']))*15) for f in submitted]
    decoded=[]
    with av.open(str(output)) as container:
        for frame in container.decode(video=0):decoded.append((float(frame.pts*frame.time_base),marker_id(frame.to_image())))
    # Determine the reader's origin from its first decoded marker, then test later pixels.
    first=decoded[0][1]
    anchor=next(i for i,code in enumerate(predicted) if code==first)
    checked=min(len(decoded),len(predicted)-anchor)
    errors=[abs(decoded[i][1]-predicted[anchor+i]) for i in range(checked)]
    assert checked>=60 and max(errors)<=1,(checked,max(errors))
    # Native/proxy time precision and explicit calibration expiry are separate gates.
    from foundation_records import TimeMapping
    source_record=SourceEpoch(event_id='marker',run_id='marker-run',source_id='camera/marker',epoch=1,slot=1,time_base='1/15360')
    mapping=TimeMapping(source=source_record,revision=1,valid=Interval(start=0,end=184320),origin_pts=0,offset_event_ms=0,
                        uncertainty_ms=1000/15,calibration_evidence=['decoded-pixel-markers'])
    try:mapping.event_ms(184320)
    except ValueError:pass
    else:raise AssertionError('Expired mapping accepted')
    # Real orientation, scale, and padding. The colored center has a known native location.
    geometry_tolerance_pixels=1
    image=Image.new('RGB',(320,180),'black');ImageDraw.Draw(image).rectangle((65,30,95,60),fill='white')
    oriented=folder/'geometry.png';image.save(oriented)
    transformed=folder/'geometry-proxy.png'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-i',str(oriented),'-vf',
        'transpose=clock,scale=202:360,pad=640:360:219:0','-frames:v','1',str(transformed)],check=True,capture_output=True,timeout=10)
    geometry=Geometry(native_width=320,native_height=180,rotation=90,output_width=640,output_height=360,
        scaled_width=202,scaled_height=360,pad_x=219,pad_y=0)
    with Image.open(transformed) as result:
        white=[(index%640+.5,index//640+.5) for index,color in enumerate(result.convert('RGB').getdata()) if sum(color)>600]
    center=tuple(sum(point[axis] for point in white)/len(white) for axis in (0,1))
    point=geometry.native_point(center[0]/640,center[1]/360)
    geometry_error=max(abs(point[0]-80.5),abs(point[1]-45.5))
    assert geometry_error<=geometry_tolerance_pixels
    try:geometry.native_point(0.,.5)
    except ValueError:pass
    else:raise AssertionError('Padding interpreted as source geometry')
    return {'passed':True,'input':str(native),'proxy':str(proxy),'program':str(output),
        'native_proxy_frames':len(frames),'native_proxy_max_error_ticks':native_proxy_error*1024,
        'program_frames_checked':checked,'program_max_error_frames':max(errors),'tolerance_frames':1,
        'uncertainty_ms':1000/15,'native_time_base':'1/15360','geometry':geometry.model_dump(),
        'geometry_max_error_native_pixels':geometry_error,'geometry_tolerance_native_pixels':geometry_tolerance_pixels,
        'reader_origin':'one decoded marker; later frames evaluated independently'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,default=Path('/evidence'))
    parser.add_argument('--sustained-seconds',type=int,default=300)
    args=parser.parse_args()
    if args.sustained_seconds<300:parser.error('E15 requires at least 300 seconds')
    run_started=time.monotonic()
    folder=args.evidence/uuid.uuid4().hex;folder.mkdir(parents=True)
    report={'prd':'17','local_ready':False,'live_verified':False,'mode':'fixture providers and real encoded media',
            'coverage':{},'failures':[],
            'provider_gaps':['VAST tenant storage/trigger, YOLO, Cosmos, semantic search, W&B, and speech access are unverified'],
            'phone_gaps':['Physical phones and venue playback remain separate gates'],
            'versions':{n:importlib.metadata.version(n) for n in ('av','pydantic','pydantic-settings','pydantic-ai-slim','fastapi','uvicorn','httpx','boto3')}}
    os.environ['PYDANTIC_AI_NO_BANNER']='1'
    import pydantic_ai.models
    pydantic_ai.models.ALLOW_MODEL_REQUESTS=False
    # Block external connections in this process. The launcher also isolates the container from external networks.
    connect=socket.socket.connect
    def local_only(sock,address):
        if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1','localhost'):
            raise AssertionError('Fixture execution attempted an external request')
        return connect(sock,address)
    socket.socket.connect=local_only
    try:
        result=run_units(folder,'unit-tests.log');report['unit_tests']=result
        if not result['passed']:raise AssertionError('Unit contract checks failed')
        for number in (1,2,4,5,6,7,8,9,10,11,12,13,14,17):
            matching=[r for r in result['results'] if f'E{number:02}' in r['test']]
            report['coverage'][f'E{number:02}']={'passed':bool(matching) and all(r['passed'] for r in matching),'tests':[r['test'] for r in matching]}
        print('Contract checks passed',flush=True)
        report['coverage']['E03']=marker_proof(folder)
        print('Decoded marker and geometry checks passed',flush=True)
        command=[sys.executable,str(ROOT/'tests/media/check.py'),'--evidence',str(folder/'media'),
            '--file',str(ROOT/'demo.mp4'),'--sustained-seconds',str(args.sustained_seconds),
            '--foundation-config',str(ROOT/'config/foundation.fixture.json')]
        with (folder/'media-check.log').open('w') as log:subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
        media_report=next((folder/'media').glob('*/report.json'))
        report['coverage']['E15']={'passed':True,'report':str(media_report)}
        # Media regressions affected by timing and lifecycle changes.
        regressions=[]
        for name, browser in (('multi_camera_check.py','replay-check.cjs'),('studio_check.py','studio-check.cjs')):
            path=ROOT/'tests/media'/name; destination=folder/name.removesuffix('.py')
            command=[sys.executable,str(path),'--output',str(destination),'--browser']
            if name=='studio_check.py':command += ['--seconds','10']
            log_path=folder/(name+'.log')
            with log_path.open('w') as log:
                child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
                try:
                    deadline=time.monotonic()+150
                    while 'BROWSER_READY' not in log_path.read_text():
                        if child.poll() is not None or time.monotonic()>deadline:raise AssertionError(name+' did not reach browser review')
                        time.sleep(.2)
                    browser_command=['node',str(ROOT/'tests/browser'/browser),str(destination)]
                    with (folder/(browser+'.log')).open('w') as browser_log:
                        subprocess.run(browser_command,stdout=browser_log,stderr=subprocess.STDOUT,check=True,timeout=150)
                    if child.wait(timeout=60)!=0:raise AssertionError(name+' media regression failed')
                finally:stop_process(child)
            regressions.append({'command':command,'browser_command':browser_command,'passed':True})
        # Graphics and general browser behavior use a fresh manual Studio with five sample inputs.
        graphics_folder=folder/'graphics';graphics_folder.mkdir()
        (graphics_folder/'runtime').mkdir()
        app=App(Config(graphics_folder/'runtime','http://localhost:25080',port=25080,offset=25000,print_access=False))
        publisher=None
        try:
            app.start()
            publisher=subprocess.Popen([sys.executable,str(ROOT/'app/studio.py'),'sample','--runtime',str(app.cfg.runtime),'--count','5'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            wait_for(lambda:len(app.status()['cameras'])==5 and all(c.get('buffer_seconds',0)>8 for c in app.status()['cameras']),'graphics five sources',timeout=60)
            environment={**os.environ,'BREADCAST_RUNTIME':str(app.cfg.runtime)}
            commands=[[sys.executable,str(ROOT/'tests/media/graphics_check.py')]]+[
                ['node',str(ROOT/'tests/browser'/name),str(app.cfg.runtime)] for name in ('graphics-check.cjs','modal-check.cjs')]
            for command in commands:
                with (graphics_folder/(Path(command[1]).name+'.log')).open('w') as log:
                    subprocess.run(command,env=environment,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=240)
                regressions.append({'command':command,'passed':True})
        finally:stop_process(publisher);app.close()
        ui_folder=folder/'ui';ui_folder.mkdir()
        app=App(Config(ui_folder,'http://localhost:26080',port=26080,offset=30000,print_access=False))
        try:
            app.start()
            command=['node',str(ROOT/'tests/browser/ui-check.cjs'),str(ui_folder)]
            with (folder/'ui-check.cjs.log').open('w') as log:
                subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=240)
            regressions.append({'command':command,'passed':True})
        finally:app.close()
        report['regressions']=regressions
        report['regressions']=regressions
        report['configuration']=FoundationSettings.load(ROOT/'config/foundation.fixture.json').model_dump()
        report['coverage']['E16']={'passed':True,'command':'./scripts/studio foundation-check','report':str(folder/'report.json')}
        report['local_ready']=all(report['coverage'].get(f'E{i:02}',{}).get('passed') for i in range(1,18))
        if not report['local_ready']:raise AssertionError('Acceptance coverage is incomplete')
        return 0
    except BaseException as error:
        report['failures'].append({'type':type(error).__name__,'reason':str(error)});return 1
    finally:
        socket.socket.connect=connect
        report['elapsed_s']=time.monotonic()-run_started
        (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print(f'Foundation report: {folder / "report.json"}',flush=True)


if __name__=='__main__':raise SystemExit(main())
