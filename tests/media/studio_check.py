"""Five-minute real media validation of local crew authority. Isolated, no provider."""
import argparse
import array
import math
import copy
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
import av
from PIL import ImageStat
from studio import App, Config, request_json
from media import stop_process
from check import wait_for


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('.runtime/autonomous-studio'))
    parser.add_argument('--seconds',type=int,default=300)
    parser.add_argument('--browser',action='store_true')
    parser.add_argument('--port',type=int,default=22080)
    parser.add_argument('--port-offset',type=int,default=2000)
    args=parser.parse_args(); folder=args.output.resolve();folder.mkdir(parents=True,exist_ok=True)
    runtime=folder/'runtime';runtime.mkdir(exist_ok=True)
    cfg=Config(runtime,f'http://localhost:{args.port}',bind='0.0.0.0',port=args.port,offset=args.port_offset,print_access=False)
    app=App(cfg);report={'passed':False,'mode':'Labeled real RTSP sample media; no provider or physical phone','checks':[], 'failures':[]}
    publishers=[];recorder=None
    def call(path,data=None):return request_json(cfg.public_url+path,data)
    def state():return call('/api/status')
    def action(op,**values):
        request={'id':uuid.uuid4().hex,'op':op,'args':values}
        request['expected']=app.control.expected(values)
        record=call('/api/actions',request)
        assert record['state']!='Rejected',record
        return record
    def record(name,**data):report['checks'].append({'name':name,**data});print(name,flush=True)
    def publish(count):
        log=open(folder/f'publisher-{len(publishers)}.log','wb')
        p=subprocess.Popen([sys.executable,str(Path('app/studio.py').resolve()),'sample','--runtime',str(runtime),'--count',str(count),'--seconds','600'],stdout=log,stderr=log)
        log.close();publishers.append(p)
    def audio_samples():
        raw=subprocess.run(['ffmpeg','-nostdin','-v','error','-rtsp_transport','tcp','-i',cfg.rtsp_url('program'),'-t','1','-vn','-ac','1','-ar','48000','-f','s16le','pipe:1'],capture_output=True,check=True,timeout=12).stdout
        return array.array('h',raw)
    def rms(samples):return math.sqrt(sum(v*v for v in samples)/len(samples))/32768
    def audio_rms():return rms(audio_samples())
    def ready(op):return wait_for(lambda:app.control.actions[op['id']]['state']=='Ready','render ready',timeout=40)
    def rehearsal():
        action('resume');action('rehearsal',slot=1)
        deadline=time.monotonic()+100
        while time.monotonic()<deadline:
            c=state()['control']
            if c['rehearsal']['state']=='Complete':
                assert state()['program']['actual']=='LIVE'
                record('R1',crew_paused=c['crew_paused'],rehearsal=c['rehearsal']);return
            assert c['rehearsal']['state'] not in ('Failed','Canceled'),c['rehearsal']
            time.sleep(.1)
        raise AssertionError('Rehearsal did not complete')
    try:
        app.start();wait_for(lambda:state()['program']['frames_written']>10,'encoder')
        assert not state()['control']['crew_paused'] and state()['program']['actual']=='HOLDING'
        publish(1);wait_for(lambda:state()['cameras'] and state()['cameras'][0].get('buffer_seconds',0)>10,'one source')
        action('live',slot=1);wait_for(lambda:state()['program']['actual']=='LIVE','one source on air')
        record('one_camera_before_five',source=state()['cameras'][0]['source_path'])
        pid=state()['program']['encoder_pid']
        recorder=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-y','-rtsp_transport','tcp','-i',cfg.rtsp_url('program'),'-c','copy',str(folder/'program.mkv')],stderr=open(folder/'recorder.log','wb'))
        recording_started=time.monotonic()
        publish(4);wait_for(lambda:state()['occupied']==5 and all(c.get('buffer_ready') for c in state()['cameras']),'five sources')
        record('UX4',occupied=5)
        # One designated microphone; explicit mute stays silent across encoder frames.
        muted=action('audio',slot=1,muted=True)
        wait_for(lambda:app.control.snapshot() and app.control.actions[muted['id']]['state']=='Finished','mute frame applied')
        mute_samples=audio_samples();muted_full_rms=rms(mute_samples);muted_rms=rms(mute_samples[len(mute_samples)//2:]);assert muted_rms<.005,muted_rms
        action('audio',slot=2,muted=False)
        selected=audio_samples();assert rms(selected)>.01
        selected=selected[len(selected)//2:len(selected)//2+4800]
        weights=[.5-.5*math.cos(2*math.pi*i/(len(selected)-1)) for i in range(len(selected))]
        amplitudes={}
        for slot in range(1,6):
            frequency=220+slot*110
            real=sum(v*w*math.cos(2*math.pi*frequency*i/48000) for i,(v,w) in enumerate(zip(selected,weights)))
            imaginary=sum(v*w*math.sin(2*math.pi*frequency*i/48000) for i,(v,w) in enumerate(zip(selected,weights)))
            amplitudes[slot]=2*math.hypot(real,imaginary)/sum(weights)/32768
        assert amplitudes[2]>.05 and max(v for k,v in amplitudes.items() if k!=2)<.005,amplitudes
        muted=action('audio',slot=2,muted=True)
        wait_for(lambda:app.control.snapshot() and app.control.actions[muted['id']]['state']=='Finished','second mute frame applied')
        mute_samples=audio_samples();second_full_rms=rms(mute_samples);second_muted_rms=rms(mute_samples[len(mute_samples)//2:]);assert second_muted_rms<.005,second_muted_rms
        action('audio',slot=1,muted=False)
        restored_rms=audio_rms();assert restored_rms>.01
        record('CAMERA_AUDIO',muted_full_capture_rms=muted_full_rms,muted_steady_rms=muted_rms,selected_camera=2,fixture_tone_amplitudes=amplitudes,second_full_capture_rms=second_full_rms,second_muted_steady_rms=second_muted_rms,restored_rms=restored_rms,steady_window='Latter half of one-second decoded audio capture; initial encoded audio may precede mute')

        rehearsal()
        # Retry returns the original command result and never repeats takeover.
        request={'id':'lost-response','op':'live','args':{'slot':2,'independent':True},'expected':app.control.expected({'slot':2,'independent':True})}
        first=call('/api/actions',request);revision=app.program.revision
        assert call('/api/actions',request)['id']==first['id'] and app.program.revision==revision
        altered=copy.deepcopy(request);altered['args']['slot']=3
        try:call('/api/actions',altered)
        except __import__('urllib.error',fromlist=['HTTPError']).HTTPError:pass
        else:raise AssertionError('Changed retry accepted')
        record('C4',command_revision=revision)
        action('resume');old={'id':'late-crew','op':'live','args':{'slot':3,'independent':True},'expected':app.control.expected({'slot':3,'independent':True}),'expires_at':time.time()+60}
        action('takeover');assert app.control.propose(old)['state']=='Rejected'
        record('C1_C2_C5',crew_paused=app.control.crew_paused,reason=app.control.actions['late-crew']['reason'])
        # Delayed worker completion cannot grant airtime, and urgent actions do not wait for it.
        import studio
        original=studio.render_plan;gate=threading.Event()
        def delayed(*values):gate.wait(10);return original(*values)
        studio.render_plan=delayed
        prepare=action('prepare',slot=1,seconds=2,speed=.5,zoom=1)
        t=time.monotonic();action('takeover');action('live',slot=1,independent=True);urgent=time.monotonic()-t
        assert urgent<1
        gate.set();ready(prepare);studio.render_plan=original
        assert state()['program']['replay_id'] is None
        record('R2_render',urgent_elapsed_s=urgent,render_did_not_air=True)
        # Explicit failed cancel checked without helper assertion.
        stale=call('/api/actions',{'id':'old-cancel','op':'cancel','args':{'job_id':prepare['job_id']},'expected':app.control.expected({})})
        assert stale['state']=='Rejected';record('C7',reason=stale['reason'])
        replay=action('replay',replay_id=prepare['job_id']);wait_for(lambda:state()['program']['actual']=='REPLAY','manual replay')
        action('takeover');wait_for(lambda:state()['program']['actual']=='LIVE','manual automatic replay completion',timeout=12)
        record('R2_replay',automatic_return_in_manual=True)
        # A selected source loss uses the existing controller holding fallback.
        action('live',slot=5,independent=True);wait_for(lambda:state()['program']['actual_target'].get('slot')==5,'camera five')
        action('audio',slot=5)
        old_asset=action('prepare',slot=5,seconds=2,speed=1,zoom=1);ready(old_asset)
        lease=next(c for c in state()['cameras'] if c['slot']==5)
        call('/api/cameras/'+lease['lease_id']+'/remove',{})
        wait_for(lambda:state()['program']['actual']=='HOLDING','source loss')
        publish(1)
        replacement=wait_for(lambda:next((c for c in state()['cameras'] if c['slot']==5 and c.get('buffer_ready') and c['source_path']!=lease['source_path']),None),'reused camera slot')
        assert state()['program']['actual']=='HOLDING'
        assert state()['program']['primary_source_path']==lease['source_path']
        assert state()['program']['audio_source_path']==lease['source_path']
        rejected=call('/api/actions',{'id':'old-source','op':'replay','args':{'replay_id':old_asset['job_id']},'expected':app.control.expected({'replay_id':old_asset['job_id']})})
        assert rejected['state']=='Rejected';record('R3_M1',reason=rejected['reason'],old_source_path=lease['source_path'],new_source_path=replacement['source_path'],same_epoch=lease['epoch']==replacement['epoch'],replacement_stays_off_air=True)
        action('live',slot=1,independent=True)
        wait_for(lambda:state()['program']['actual']=='LIVE','live after replacement')
        replacement_rms=audio_rms();assert replacement_rms<.005,replacement_rms
        action('audio',slot=1)
        restored_rms=audio_rms();assert restored_rms>.01,restored_rms
        record('R3_audio_owner',replacement_audio_rms=replacement_rms,explicit_restored_audio_rms=restored_rms)
        if args.browser:
            (folder/'browser-done').unlink(missing_ok=True); (folder/'browser-ready').write_text('ready\n'); print('BROWSER_READY',flush=True)
            wait_for(lambda:(folder/'browser-done').exists(),'browser checks',timeout=180)
            browser=json.loads((folder/'browser-report.json').read_text());assert browser['passed'];report['browser']=browser
        # Continue actual media with transitions until the recording spans at least five minutes.
        next_change=time.monotonic()+10;camera=1
        while time.monotonic()-recording_started<args.seconds+3:
            current=state()['program'];assert current['encoder_pid']==pid and not current['error']
            if time.monotonic()>=next_change:
                camera=2 if camera==1 else 1;action('live',slot=camera,independent=True)
                action('graphics',graphics={'op':'cue','preset':'corner-label','title':'LOCAL REHEARSAL','subtitle':'','duration_s':3})
                next_change=time.monotonic()+15
            time.sleep(.5)
        recorder.send_signal(signal.SIGINT);recorder.wait(10);recorder=None
        timestamps=[];black=0
        with av.open(str(folder/'program.mkv')) as container:
            for frame in container.decode(video=0):
                timestamps.append(float(frame.pts*frame.time_base))
                if max(ImageStat.Stat(frame.to_image()).mean)<4:black+=1
        gaps=[b-a for a,b in zip(timestamps,timestamps[1:])]
        assert timestamps[-1]-timestamps[0]>=args.seconds,(timestamps[0],timestamps[-1])
        assert black==0 and min(gaps)>0 and max(gaps)<.14
        record('M2',encoded_duration_s=timestamps[-1]-timestamps[0],frames=len(timestamps),black_frames=black,max_frame_gap_s=max(gaps),encoder_pid=pid,artifact=str(folder/'program.mkv'))
        report['passed']=True
    except Exception as error:
        report['failures'].append(str(error));raise
    finally:
        if recorder:stop_process(recorder)
        for p in publishers:stop_process(p)
        app.close()
        report['versions']=json.loads((runtime/'versions.json').read_text()) if (runtime/'versions.json').exists() else {}
        report['actions_path']=str(runtime/'program-history.jsonl')
        (folder/'validation.json').write_text(json.dumps(report,indent=2)+'\n')
        print(folder/'validation.json',flush=True)

if __name__=='__main__':main()
