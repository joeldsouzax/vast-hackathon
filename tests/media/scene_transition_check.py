"""Isolated encoder check: a short stinger must not interrupt narration.

Run inside the media container; output stays in /tmp/transition-media.
This generated camera never reaches the public program.
"""
import io,json,math,subprocess,time,threading
from array import array
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from studio import App,Config
from media import Frame,jpeg
from foundation_records import Geometry
from direction_media import PreparedCue

root=Path('/tmp/transition-media');root.mkdir(exist_ok=True);(root/'runtime').mkdir(exist_ok=True)
config=json.loads(Path('/opt/breadcast/config/direction.fixture.json').read_text())
config['fixture_file']='/opt/breadcast/tests/fixtures/foundation/labels.json'
path=root/'config.json';path.write_text(json.dumps(config))
cfg=Config(root/'runtime','http://localhost',delay=0,foundation_config=path,print_access=False)
app=App(cfg);p=app.program
geom=Geometry(native_width=640,native_height=360,output_width=640,output_height=360,scaled_width=640,scaled_height=360)
payload=jpeg(Image.new('RGB',(640,360),'green'));origin=time.monotonic();frames=deque()
def frame_at(target):
 i=max(0,int((target-origin)*15))
 f=Frame(i+1,origin+i/15,payload,i/15,i,'1/15',{'native_pts':i,'native_time_base':'1/15','timeline_revision':1,'geometry':geom.model_dump(),'uncertainty_ms':1000/15},1,time.time())
 frames.append(f)
 while len(frames)>450:frames.popleft()
 return f
source=SimpleNamespace(path='camera/transition-check',slot=1,epoch=1,timeline_revision=1,has_audio=True,origin=origin,
 lock=threading.RLock(),frames=frames,at=frame_at,audio_at=lambda _:bytes(6400),close=lambda:None)
source.status=lambda:{'last_frame_age_s':0.,'buffer_seconds':20.,'buffer_ready':True,'has_audio':True}
app.sources[1]=source
original=subprocess.Popen;output=root/'program.mkv'
def file_encoder(command,**kwargs):
 if command[0]=='ffmpeg' and command[-1]==cfg.rtsp_url('program',cfg.program_token):command=command[:-5]+['-f','matroska','-y',str(output)]
 return original(command,**kwargs)
def wait_for(fn):
 until=time.monotonic()+8
 while not fn():
  if time.monotonic()>until:raise RuntimeError('Local media check timed out: '+str(p.error))
  time.sleep(.03)
try:
 with patch('media.subprocess.Popen',side_effect=file_encoder):
  p.start();wait_for(lambda:p.frames_written>3)
  p.command('live',p.revision,1);wait_for(lambda:p.actual=='LIVE')
  pcm=array('h',[round(7000*math.sin(2*math.pi*660*i/48000)) for i in range(3*48000)]).tobytes()
  start=p.frames_written+2
  cue=PreparedCue('tone','local','Synthetic narration signal',pcm,None,start,start+100,time.time()+8,p.revision,
    {'kind':'camera','source_path':source.path,'epoch':1},lambda:True)
  p.schedule_commentary(cue);wait_for(lambda:cue.offset>9600)
  p.command('graphics',p.revision,graphics={'op':'cue','preset':'iris-reveal','duration_s':.8})
  first=p.frames_written
  wait_for(lambda:cue.delivered.get('speech',{}).get('state')=='completed' or cue.delivered.get('speech',{}).get('terminal'))
  assert not cue.canceled and cue.offset==len(pcm),(cue.delivered,cue.cancel_reason)
  wait_for(lambda:'stinger' not in p.graphics.active)
  assert p.actual=='LIVE' and not p.error
finally:app.close()
raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-vn','-ac','1','-ar','48000','-f','s16le','-'])
samples=array('h',raw);window=samples[(first+2)*3200:(first+9)*3200]
rms=math.sqrt(sum(x*x for x in window)/len(window));assert rms>1000,rms
report={'scope':'isolated synthetic source and narration tone; actual encoder to local file, no public broadcast',
 'transition':'iris-reveal','duration_s':.8,'speech_complete':True,'speech_canceled':cue.canceled,
 'decoded_rms_during_transition':round(rms,2),'camera_remained_live':True}
(root/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
