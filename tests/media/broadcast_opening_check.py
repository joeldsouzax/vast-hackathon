"""Private program encode: full opening delivery before the normal crew gate opens."""
from array import array
from collections import deque
import argparse
import json
import math
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from studio import App, Config
from media import Frame, jpeg
from foundation_records import Geometry

ROOT=Path(__file__).resolve().parents[2]


def check(event_path=None):
    with tempfile.TemporaryDirectory(prefix='breadcast-opening-') as folder:
        root=Path(folder)
        if event_path:
            from workshop_config import settings
            settings=settings().model_dump(mode='json')
            settings['event']=json.loads(Path(event_path).read_text())
        else:
            settings=json.loads((ROOT/'config/direction.fixture.json').read_text())
            settings['fixture_file']=str(ROOT/'tests/fixtures/foundation/labels.json')
            settings['event']['opening_script']=['Welcome to our event.','Let us see the demos.']
        config=root/'config.json';config.write_text(json.dumps(settings))
        cfg=Config(root/'runtime','http://localhost',delay=0,foundation_config=config,print_access=False)
        cfg.runtime.mkdir();app=App(cfg);program=app.program
        geometry=Geometry(native_width=640,native_height=360,output_width=640,output_height=360,scaled_width=640,scaled_height=360)
        payload=jpeg(Image.new('RGB',(640,360),'green'));origin=time.monotonic()
        def frame_at(target):
            index=max(0,int((target-origin)*15))
            return Frame(index+1,origin+index/15,payload,index/15,index,'1/15',
                {'native_pts':index,'native_time_base':'1/15','timeline_revision':1,
                 'geometry':geometry.model_dump(),'uncertainty_ms':1000/15},1,time.time())
        source=SimpleNamespace(path='camera/opening-check',slot=1,epoch=1,has_audio=False,
            origin=origin,lock=threading.RLock(),frames=deque(),at=frame_at,audio_at=lambda _:bytes(cfg.audio_size),close=lambda:None,
            status=lambda:{'last_frame_age_s':0.,'buffer_seconds':20.,'buffer_ready':True,'has_audio':False})
        app.sources[1]=source
        output=root/'program.mkv';original=subprocess.Popen
        def file_encoder(command,**kwargs):
            if command[0]=='ffmpeg' and command[-1]==cfg.rtsp_url('program',cfg.program_token):
                command=command[:-5]+['-f','matroska','-y',str(output)]
            return original(command,**kwargs)
        def wait_for(predicate):
            deadline=time.monotonic()+65
            while not predicate():
                app.direction.drain_receipts()
                if time.monotonic()>deadline:raise AssertionError('Opening timed out: '+app.direction.reason)
                time.sleep(.025)
        speech=app.direction._speech;parts=[]
        tone=array('h',[round(7000*math.sin(2*math.pi*660*i/48000)) for i in range(5*48000)]).tobytes()
        def measured_speech(intent,deps,event):
            started=time.monotonic()
            result=speech(intent,deps,event) if event_path else (tone,None,None)
            parts.append({'text':intent.text,'duration_s':len(result[0])/96000,'preparation_s':round(time.monotonic()-started,3)})
            return result
        try:
            with patch('media.subprocess.Popen',side_effect=file_encoder), \
                    patch.object(app.foundation,'submit_role',side_effect=lambda role,job:job()), \
                    patch.object(app.direction,'_speech',side_effect=measured_speech):
                program.start();app.control.start()
                wait_for(lambda:program.frames_written>3)
                program.command('live',program.revision,1)
                app.control.program_started=True
                wait_for(lambda:program.actual=='LIVE')
                assert app.direction._opening()
                assert app.direction.prepared,app.direction.reason
                cue=app.direction.prepared[app.direction.opening['cue_id']]
                assert len(cue.pcm)>8*96000
                assert app.direction._opening()
                wait_for(lambda:app.direction.opening['state']=='Completed')
                assert not app.direction._opening()
                receipt=cue.delivered['speech']
                assert receipt['last']-receipt['first']==len(cue.pcm)//2,receipt
                assert program.actual=='LIVE' and not program.error
                wait_for(lambda:program.cue is None)
        finally:app.close()
        raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-vn','-ac','1','-ar','48000','-f','s16le','-'])
        samples=array('h',raw)[receipt['first']:receipt['last']]
        rms=round(math.sqrt(sum(x*x for x in samples)/len(samples)),2)
        assert rms>100,rms
        return {'scope':('Real Gemini speech' if event_path else 'Synthetic speech')+'; synthetic camera; private real program encode and decode',
            'parts':parts,'duration_s':len(cue.pcm)/96000,'delivered_samples':receipt['last']-receipt['first'],
            'decoded_rms':rms,'state':app.direction.opening['state'],'public_phone_playback':'pending'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--event-context');parser.add_argument('--output')
    args=parser.parse_args();result=check(args.event_context);text=json.dumps(result,indent=2)+'\n'
    if args.output:Path(args.output).write_text(text)
    print(text)
