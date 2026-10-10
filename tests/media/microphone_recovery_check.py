"""Isolated reconnect check with a synthetic microphone and real encoded audio."""
from array import array
from collections import deque
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


def check():
    with tempfile.TemporaryDirectory(prefix='breadcast-microphone-') as folder:
        root=Path(folder)
        settings=json.loads((ROOT/'config/direction.fixture.json').read_text())
        settings['fixture_file']=str(ROOT/'tests/fixtures/foundation/labels.json')
        config=root/'config.json';config.write_text(json.dumps(settings))
        cfg=Config(root/'runtime','http://localhost',delay=0,foundation_config=config,print_access=False)
        cfg.runtime.mkdir()
        app=App(cfg);program=app.program
        geometry=Geometry(native_width=640,native_height=360,output_width=640,output_height=360,scaled_width=640,scaled_height=360)
        payload=jpeg(Image.new('RGB',(640,360),'green'));origin=time.monotonic()
        tone=array('h',[round(7000*math.sin(2*math.pi*660*i/48000)) for i in range(3200)]).tobytes()
        def frame_at(target):
            index=max(0,int((target-origin)*15))
            return Frame(index+1,origin+index/15,payload,index/15,index,'1/15',
                {'native_pts':index,'native_time_base':'1/15','timeline_revision':1,
                 'geometry':geometry.model_dump(),'uncertainty_ms':1000/15},source.epoch,time.time())
        source=SimpleNamespace(path='camera/reconnect-check',slot=1,epoch=1,has_audio=True,
            origin=origin,lock=threading.RLock(),frames=deque(),at=frame_at,audio_at=lambda _:tone,close=lambda:None,
            status=lambda:{'last_frame_age_s':0.,'buffer_seconds':20.,'buffer_ready':True,'has_audio':True})
        app.sources[1]=source
        output=root/'program.mkv';original=subprocess.Popen
        def file_encoder(command,**kwargs):
            if command[0]=='ffmpeg' and command[-1]==cfg.rtsp_url('program',cfg.program_token):
                command=command[:-5]+['-f','matroska','-y',str(output)]
            return original(command,**kwargs)
        def wait_for(predicate):
            deadline=time.monotonic()+10
            while not predicate():
                if time.monotonic()>deadline:raise AssertionError('Encoder check timed out: '+str(program.error))
                time.sleep(.025)
        def interval():
            first=program.frames_written
            wait_for(lambda:program.frames_written>=first+15)
            return first+3,first+12
        try:
            with patch('media.subprocess.Popen',side_effect=file_encoder),patch.object(app.leases,'rows',
                    side_effect=lambda:[{'path':source.path,'slot':1,'epoch':source.epoch,'state':'ACTIVE'}]):
                program.start();wait_for(lambda:program.frames_written>3)
                program.command('audio',program.revision,1,muted=False)
                program.command('live',program.revision,1)
                wait_for(lambda:program.actual=='LIVE')
                baseline=interval()
                with program.lock,source.lock:source.epoch=2
                before=interval()
                assert program.audio_epoch==1 and not program.audio_muted
                assert app.control.recover_microphone()
                after=interval()
                assert program.audio_epoch==2 and not program.audio_muted
                assert program.actual=='LIVE' and program.slot==1 and not program.error
        finally:app.close()
        raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-vn','-ac','1','-ar','48000','-f','s16le','-'])
        samples=array('h',raw)
        def rms(bounds):
            window=samples[bounds[0]*3200:bounds[1]*3200]
            assert window
            return round(math.sqrt(sum(x*x for x in window)/len(window)),2)
        levels={'before_reconnect':rms(baseline),'before_recovery':rms(before),'after_recovery':rms(after)}
        assert levels['before_reconnect']>1000,levels
        assert levels['before_recovery']<10,levels
        assert levels['after_recovery']>1000,levels
        return {'scope':'Synthetic source epoch change; real encoder and decoded PCM; no public broadcast',
            'rms':levels,'camera_unchanged':True,'microphone_unmuted':True,'selected_epoch':2}


if __name__=='__main__':print(json.dumps(check()))
