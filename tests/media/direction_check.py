"""PRD 2 acceptance: isolated providers, real encoded media, explicit open gates."""
from __future__ import annotations
from array import array
import argparse
from collections import Counter
from fractions import Fraction
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback
import unittest
import uuid
import asyncio
from unittest.mock import patch
import av
from PIL import Image, ImageChops, ImageStat
from check import wait_for
from foundation_check import CoverageResult, marker_proof
from contract_check import run_units
from foundation_records import LLMResult, Interval
from media import stop_process
from studio import App, Config
from direction_media import PreparedCue


def queued_speech(app, folder):
    """Two prepared signals use the real controller queue and encoder."""
    folder.mkdir(parents=True,exist_ok=True)
    snapshot=app.foundation.reviewed_snapshot()
    deps={'snapshot':snapshot,'evidence_ids':[],'deadline':time.time()+8,'sources':{}}
    with app.program.lock:
        start=app.program.frames_written+2
        target={k:v for k,v in app.program.actual_target.items() if k in ('kind','source_path','epoch','id','command_revision')}
    cues=[]
    for number,frequencies in enumerate(((440,880),(660,1100))):
        pcm=array('h',[round(8000*math.sin(2*math.pi*frequencies[i//48000]*i/48000)) for i in range(96000)]).tobytes()
        cue_id=uuid.uuid4().hex
        cue=PreparedCue(cue_id,f'{snapshot.run_id}:{snapshot.program_revision}',f'Generated queue test signal {number}',pcm,None,
            start,start+(32 if number==0 else 100),deps['deadline'],snapshot.program_revision,target,
            lambda:app.direction._guard(deps))
        app.direction.prepared[cue.id]=cue
        app.direction._history(cue,'prepared');app.direction._history(cue,'pending')
        record=app.control.propose({'id':cue.id,'op':'commentary','args':{'cue_id':cue.id},
            'expected':app.direction.expected(snapshot,{}),'expires_at':cue.expires_at},actor='Provider crew',dependencies=deps)
        assert record['state']=='Scheduled',record
        (folder/f'{number}.pcm').write_bytes(pcm)
        cues.append(cue)
    wait_for(lambda:all(c.delivered.get('speech',{}).get('terminal') for c in cues),'queued signals completed',timeout=9)
    records=[]
    for number,cue in enumerate(cues):
        receipt=cue.delivered['speech']
        assert receipt['last']-receipt['first']==len(cue.pcm)//2,receipt
        wait_for(lambda:any(r['cue_id']==cue.id+'-speech' and r['state']=='completed' for r in rows(app)),'complete signal receipt')
        records.append({'id':cue.id,'first_sample':receipt['first'],'last_sample':receipt['last'],
            'input_samples':len(cue.pcm)//2,'reference':str(folder/f'{number}.pcm'),'original_start_frame':start,
            'admitted_start_frame':cue.start_frame,'original_end_frame':cue.end_frame,'original_expiry':cue.expires_at})
    assert records[1]['first_sample']>=records[0]['last_sample']
    assert cues[1].start_frame>start
    wait_for(lambda:app.program.cue is None,'queue signal window closed',timeout=5)
    return records


def repeat_plan(app):
    """Explicit synthetic receipt-clock calibration; no phone-sync claim."""
    from replay_fixture import evidence,plan
    base=app.get_source(1).origin
    intervals=[]
    for camera in (1,2):
        source=app.get_source(camera)
        with source.lock:frames=tuple(source.frames)
        def event_ms(frame):return (source.origin+frame.media_s-base)*1000
        markers=[{'pts':frames[i].pts,'event_ms':event_ms(frames[i]),'label':'Synthetic sample receipt-clock fixture'}
            for i in (0,len(frames)//2,len(frames)-1)]
        mapping=app.replay_context.calibrate({'source_id':f'camera-{camera}','source_epoch':source.epoch,'uncertainty_ms':3,'markers':markers})
        lo,hi=event_ms(frames[0]),event_ms(frames[-1])
        intervals.append((lo,hi))
        ev=evidence(camera);ev.update(source_epoch=source.epoch,mapping_revision=mapping['revision'],event_start_ms=lo,event_end_ms=hi,
            action='Labeled sample fixture; application replay timing only')
        app.replay_context.register_evidence(ev)
    end=math.floor(min(hi for _,hi in intervals)-100)
    start=end-4000
    assert start>=max(lo for lo,_ in intervals)
    result=plan(app.cfg,True)
    result['plan_id']='direction-mixed-repeat'
    first={**result['shots'][0],'event_start_ms':start,'event_end_ms':start+2000}
    second={**first,'event_start_ms':start+2000,'event_end_ms':end,'speed':.5}
    third={**result['shots'][1],'event_start_ms':start,'event_end_ms':start+2000,'speed':.5}
    result['shots']=[first,second,third]
    result['source_mapping_revisions']={}
    for shot in result['shots']:
        source=app.get_source(int(shot['source_id'].removeprefix('camera-')))
        shot['source_epoch']=source.epoch
        revision=app.replay_context.evidence[shot['evidence_ids'][0]]['mapping_revision']
        result['source_mapping_revisions'][f"{shot['source_id']}:{source.epoch}"]=revision
    return result


def stress_media(app):
    """Saturate real shared workers, preparation limits, and action history."""
    from direction_media import caption_layer
    control=app.control;direction=app.direction;foundation=app.foundation
    initial_frame=app.program.frames_written;encoder_pid=app.program.encoder.pid
    old_settings=direction.settings;old_foundation_settings=foundation.settings.direction
    counts=Counter();failures=[];maximum={'active':0,'pending':0};pool=None
    def terminal_jobs():
        with foundation.lock:return foundation.db.execute("SELECT count(*) FROM jobs WHERE state IN ('completed','failed','expired')").fetchone()[0]
    before_jobs=terminal_jobs()
    try:
        small=old_settings.model_copy(update={'speech_asset_bytes':2097152,'speech_total_bytes':2097152})
        direction.settings=small;foundation.settings.direction=small
        snapshot=foundation.reviewed_snapshot()
        start=app.program.frames_written+2
        pool=PreparedCue(uuid.uuid4().hex,f'{snapshot.run_id}:{snapshot.program_revision}','Retained synthetic storage test signal',bytes(672000),
            caption_layer(app.program.graphics,'Retained synthetic storage test signal'),start,start+120,time.time()+9,snapshot.program_revision,{},lambda:True)
        assert pool.memory_bytes<small.speech_asset_bytes
        direction.prepared[pool.id]=pool;direction._history(pool,'prepared');direction._history(pool,'pending')
        app.program.schedule_commentary(pool)
        source=next(s for s in snapshot.sources if s.source_id==app.program.actual_target['source_path'])
        context=direction._context('commentator',source,snapshot)
        phrase=foundation.registry.labels['speech']['variants'][0]['text']
        direction.dispatch(LLMResult(text=json.dumps({'op':'commentary','text':phrase,'reason':'Storage saturation fixture'}),
            snapshot=snapshot,origin='fixture',model_id='fault-fixture',model_version='fixture-1'),'commentator',context,time.time()+8)
        wait_for(lambda:direction.state['speech']=='Unavailable' and not any(r['channel']=='intent' and r['state']=='pending' for r in rows(app)),
            'storage rejection and reservation cleanup')
        assert len(direction.prepared)==1 and direction.status()['prepared_bytes']<=small.speech_total_bytes
        storage_reason=direction.reason
        assert 'bounded buffer' in storage_reason
        direction.discard(pool.id,'Storage test complete');direction.drain_receipts();pool=None
        direction.settings=old_settings;foundation.settings.direction=old_foundation_settings
        async def delayed_failure(role):
            counts[role]+=1
            await asyncio.sleep(.5)
            raise ConnectionError('Labeled provider failure')
        def role_work(role):
            async def call():
                try:
                    async with asyncio.timeout(.2):
                        if role=='speech':await foundation.registry.speech(phrase,foundation.storage,time.time()+.2,event_context=foundation.event_context())
                        else:await foundation.registry.llm(role,context,snapshot,time.time()+.2)
                except (TimeoutError,ConnectionError) as error:failures.append({'role':role,'reason':type(error).__name__})
            asyncio.run(call())
        async def analysis_failure(*args):
            counts['analysis']+=1
            await asyncio.sleep(.2)
            raise TimeoutError('Labeled analysis timeout')
        async def llm_failure(role,*args):await delayed_failure(role)
        async def speech_failure(*args,**kwargs):await delayed_failure('speech')
        with patch.object(foundation.registry,'llm',side_effect=llm_failure), \
             patch.object(foundation.registry,'speech',side_effect=speech_failure), \
             patch.object(foundation.registry,'analyze',side_effect=analysis_failure):
            for _ in range(100):
                for role in ('director','commentator','speech'):foundation.submit_role(role,lambda role=role:role_work(role))
            began=time.monotonic()
            while time.monotonic()-began<4:
                with foundation.lock:
                    maximum['active']=max(maximum['active'],len(foundation.role_active))
                    maximum['pending']=max(maximum['pending'],len(foundation.role_pending))
                    assert len(foundation.role_active)<=foundation.settings.limits.concurrency
                    assert len(foundation.role_pending)<=3
                assert app.program.encoder.pid==encoder_pid and app.program.encoder.poll() is None
                time.sleep(.02)
        assert all(counts[role] and any(f['role']==role for f in failures) for role in ('director','commentator','speech')),counts
        assert terminal_jobs()>before_jobs,'Analysis did not retain progress under crew load'
        expired=control.propose({'id':uuid.uuid4().hex,'op':'holding','args':{},'expected':control.expected({}),
            'expires_at':time.time()-1},actor='Provider crew')
        assert expired['state']=='Rejected' and 'expired' in expired['reason'].lower(),expired
        capacity=max(32,len(control.actions)+1)
        direction.settings=old_settings.model_copy(update={'action_records':capacity})
        foundation.settings.direction=direction.settings
        while len(control.actions)<capacity:
            control.propose({'id':uuid.uuid4().hex,'op':'policy','args':{},'expected':control.expected({}),'expires_at':time.time()+8},actor='Provider crew')
        try:control.propose({'id':uuid.uuid4().hex,'op':'holding','args':{}},actor='Provider crew')
        except ValueError as error:
            history_reason=str(error);assert 'capacity' in history_reason
        else:raise AssertionError('Full history accepted new crew work')
        began=time.monotonic()
        urgent=control.submit({'id':uuid.uuid4().hex,'op':'live','args':{'slot':app.program.slot}})
        assert urgent['state']!='Rejected',urgent
        wait_for(lambda:control.actions[urgent['id']]['state'] in ('On air','Finished'),'urgent human return under saturation')
        latency=time.monotonic()-began;assert latency<=1,latency
        assert app.program.frames_written>initial_frame+app.cfg.fps*3 and not app.program.error
        return {'passed':True,'conditions':{'provider_timeouts':True,'role_queue_bounds':True,'speech_storage_saturation':True,
            'history_saturation':True,'analysis_progress':True,'media_progress':True,'urgent_human_return':True,'expired_work_dropped':True},
            'provider_calls':dict(counts),'failures':failures,'maximum_role_work':maximum,'storage_rejection':storage_reason,
            'history_rejection':history_reason,'expired_action':expired,'urgent_return_s':latency,'frames_advanced':app.program.frames_written-initial_frame}
    finally:
        if pool:direction.discard(pool.id,'Storage test cleanup');direction.drain_receipts()
        direction.settings=old_settings;foundation.settings.direction=old_foundation_settings

ROOT=Path(__file__).resolve().parents[2]

# Unit test names establish contract checks, not complete PRD acceptance.
REQUIRED_CONDITIONS={
    'D01':('contract_checks','setup_media','asset_font_and_text_failures'),
    'D02':('contract_checks','concurrent_edits','official_effective_time','failed_replacement'),
    'D03':('contract_checks','live_decoded','audio_decoded','graphics_decoded','holding_decoded','scheduler_return','no_ready_replay'),
    'D04':('contract_checks','crop_and_reset_decoded','all_source_reset_cases'),
    'D05':('contract_checks','slow_calls_all_dependencies','missing_and_stale_health'),
    'D06':('contract_checks','lost_response_retry','all_authority_and_evidence_cases'),
    'D07':('contract_checks','receipt_mapping_decoded','clock_discontinuity_decoded','unknown_mapping'),
    'D08':('contract_checks','development_and_callback','future_fact_exclusion','preparation_duplicates'),
    'D09':('contract_checks','partial_delivery','all_invalidation_cases'),
    'D10':('contract_checks','speech_decoded','queued_complete_audio','all_invalid_asset_cases'),
    'D11':('contract_checks','audio_levels_decoded','microphone_loss_and_reuse'),
    'D12':('contract_checks','mixed_speed_repeat_map','matching_speech_decoded','source_silence','session_cancellation','screen_entry_display_exit','live_sound_restored'),
    'D13':('contract_checks','caption_and_expiry_decoded','all_layer_and_footage_cases'),
    'D14':('browser_regressions','monitor_mute_is_local'),
    'D15':('contract_checks','provider_timeouts','all_provider_fault_paths','role_queue_bounds','speech_storage_saturation','history_saturation','analysis_progress','media_progress','urgent_human_return','expired_work_dropped'),
    'D16':('five_source_continuity_decoded','command_return','viewer_return'),
    'D17':('contract_checks','restart_and_shutdown','affected_regressions'),
    'D18':('contract_checks','same_validators','all_capability_and_retry_failures','no_provider_traffic'),
    'D19':('acceptance_command','complete_evidence_and_handoff'),
}


def acceptance_coverage(contracts,media=None,regression=None):
    coverage={}
    for criterion,required in REQUIRED_CONDITIONS.items():
        base=dict((contracts or {}).get(criterion,{}))
        result=dict((media or {}).get(criterion,{}))
        conditions={'contract_checks':base.get('contract_checks_passed',False),**result.get('conditions',{})}
        if regression:
            if criterion=='D14':conditions['browser_regressions']=regression['local_ready']
            if criterion=='D17':conditions['affected_regressions']=regression['local_ready']
        missing=[name for name in required if conditions.get(name) is not True]
        coverage[criterion]={**base,**result,'conditions':conditions,'missing_conditions':missing,'passed':not missing}
    return coverage


def rows(app):
    with app.foundation.lock:
        return [json.loads(r['body']) for r in app.foundation._records('program_text')]


def correlate(output, reference, expected, rate=48000):
    """Find recorded speech offset within a declared +/-500 ms search window."""
    out=array('h',output);ref=array('h',reference)
    stride=96;length=min(len(ref),rate*3)
    centered=list(ref[:length:stride]);mean=sum(centered)/len(centered)
    centered=[x-mean for x in centered];energy=sum(x*x for x in centered)
    best=(-1,None)
    for shift in range(-rate//2,rate//2+1,240):
        start=expected+shift
        if start<0 or start+length>=len(out):continue
        values=list(out[start:start+length:stride]);avg=sum(values)/len(values)
        denominator=math.sqrt(energy*sum((x-avg)**2 for x in values))
        score=sum(x*(y-avg) for x,y in zip(centered,values))/denominator if denominator else 0
        if score>best[0]:best=(score,shift)
    if best[1] is None or best[0]<.55:raise AssertionError('Recorded speech does not correlate with the declared fixture')
    return {'correlation':best[0],'alignment_error_ms':abs(best[1])*1000/rate,'tolerance_ms':500,'search_step_ms':5}


def crew_media(folder, seconds=300):
    """One camera first, then five. No model or fixture acknowledgement proves pixels."""
    folder.mkdir(parents=True,exist_ok=True);runtime=folder/'runtime';runtime.mkdir()
    cfg=Config(runtime,'http://localhost:27080',port=27080,offset=32000,
        foundation_config=ROOT/'config/direction.fixture.json',program_proof=folder/'program.mkv',print_access=False)
    app=App(cfg);publishers=[];checks={};pixel_checks=[];states=[]
    # Scripted fixture deadlines prove prepared-media behavior independently of
    # cold model initialization. Automatic scheduling is exercised in D03 below.
    app.direction.settings=app.direction.settings.model_copy(update={'enabled':False})
    def human(op,**args):
        result=app.control.submit({'id':uuid.uuid4().hex,'op':op,'args':args})
        if result['state']=='Rejected':raise AssertionError(result['reason'])
        return result
    def resume():human('resume')
    def publish():
        log=(folder/f'publisher-{len(publishers)}.log').open('w')
        process=subprocess.Popen([sys.executable,str(ROOT/'app/studio.py'),'sample','--runtime',str(runtime)],stdout=log,stderr=log)
        log.close();publishers.append(process)
    def wait_applied(action):
        return wait_for(lambda:app.control.actions[action['id']]['state'] in ('On air','Finished'),'actual action media submission',timeout=10)
    def take_pixel(kind):
        with app.program.lock:
            target=dict(app.program.actual_target);index=app.program.frames_written-1
            source=app.get_source(app.program.slot)
            if kind=='holding':image=app.program.holding.copy();sequence=None
            else:
                with source.lock:frame=next(f for f in source.frames if f.sequence==target['sequence'])
                image=Image.open(io.BytesIO(frame.data)).convert('RGB');sequence=frame.sequence
        if kind=='crop':image=image.crop((160,90,480,270)).resize((640,360),Image.Resampling.LANCZOS)
        if kind=='director-graphic':
            image=Image.alpha_composite(image.convert('RGBA'),app.program.graphics.event_package['prepared']['corner-label'].layer).convert('RGB')
        path=folder/f'{kind}-expected.png';image.save(path)
        pixel_checks.append({'kind':kind,'program_frame':index,'expected':str(path),'sequence':sequence})
    def dispatch(role,intent,deadline_s=8):
        snapshot=app.foundation.reviewed_snapshot()
        source=next(s for s in snapshot.sources if s.source_id==app.program.actual_target.get('source_path',snapshot.runtime.program.primary_source_path))
        context=app.direction._context(role,source,snapshot)
        result=LLMResult(text=json.dumps(intent),snapshot=snapshot,origin='fixture',model_id='scripted-role-fixture',model_version='fixture-1')
        before=set(app.direction.prepared)
        record=app.direction.dispatch(result,role,context,time.time()+deadline_s)
        if role=='commentator':
            wait_for(lambda:set(app.direction.prepared)-before,'prepared scripted speech')
            return next(app.direction.prepared[key] for key in set(app.direction.prepared)-before)
        return record
    def snapshot_audio(label,duration=.4):
        start=app.program.frames_written*3200
        time.sleep(duration)
        states.append({'name':label,'start_sample':start+4800,'end_sample':app.program.frames_written*3200-1600})
    encoder_pid=None;continuous_start=None
    try:
        app.start();publish()
        wait_for(lambda:app.get_source(1) and app.get_source(1).status()['buffer_seconds']>5 and app.get_source(1).status()['buffer_ready'],'one camera',timeout=45)
        event=app.foundation.event_context().model_dump();event.update(title='LOCAL DIRECTION CHECK',revision=event['revision']+1,broadcast_delay_s=cfg.delay)
        original=app.program.revision
        setup=app.setup_event({'context':event,'expected_revision':event['revision']-1,'operation_key':'direction-setup'})
        assert app.program.revision==original
        (folder/'setup.json').write_text(json.dumps(setup,indent=2)+'\n')
        for key,prepared in app.program.graphics.event_package['prepared'].items():(folder/f'preview-{key}.png').write_bytes(app.program.graphics.preview(prepared))
        checks['D01']={'passed':True,'package':str(folder/'setup.json'),'preview_airtime_effect':False}
        human('audio',slot=1,muted=True)
        began=time.monotonic();wait_applied(human('live',slot=1));application_s=time.monotonic()-began
        encoder_pid=app.program.encoder.pid;resume()
        phrase=app.foundation.registry.labels['speech']['text']
        dispatch('commentator',{'op':'commentary','text':phrase,'reason':'Scripted one-camera speech fixture'})
        spoken=wait_for(lambda:next((r for r in rows(app) if r['channel']=='speech' and r['state']=='completed'),None),'scripted fixture speech delivered',timeout=12)
        wait_for(lambda:app.program.cue is None,'caption completed')
        caption=wait_for(lambda:next((r for r in rows(app) if r['channel']=='caption' and r['state'] in ('completed','interrupted') and r['first_program_ms'] is not None),None),'caption terminal receipt')
        assert caption['last_program_ms']-caption['first_program_ms']<=8000
        wanted_frame=spoken['first_sample']//3200+5
        submitted=[frame for line in (runtime/'program-history.jsonl').read_text().splitlines()
            for record in [json.loads(line)] if record['kind']=='program_source_map' for frame in record['frames']]
        mapping=next(record for record in submitted if record['program_frame']==wanted_frame)
        source=app.get_source(1)
        with source.lock:raw=next(f for f in source.frames if f.sequence==mapping['target']['sequence'])
        from direction_media import caption_layer
        layer=caption_layer(app.program.graphics,spoken['text'])
        expected=Image.alpha_composite(Image.open(io.BytesIO(raw.data)).convert('RGBA'),layer).convert('RGB')
        expected.save(folder/'caption-expected.png')
        pixel_checks.append({'kind':'caption','program_frame':wanted_frame,'expected':str(folder/'caption-expected.png')})
        take_pixel('caption-cleared')
        # Explicit scripts own subsequent fixture triggers, without a competing loop.
        app.direction.settings=app.direction.settings.model_copy(update={'enabled':False})
        queue_signals=queued_speech(app,folder/'queue-signals')
        checks['one_camera_first']={'passed':True,'command_application_s':application_s,'speech_receipt':spoken}
        assert application_s<=1
        mappings=[json.loads(line) for line in (runtime/'program-history.jsonl').read_text().splitlines() if json.loads(line)['kind']=='live_recording_mapping']
        assert mappings,'No measured recording-to-decoder mapping'
        checks['D07']={'passed':True,'measured_recording_mappings':len(mappings),'matches':mappings[:3],
            'unknown_capture_clock':True,'unknown_mapping_rejection':'test_D07_unknown_ambiguous_and_discontinuous_mapping'}
        # One camera is proved before other slots are admitted.
        for count in range(2,6):
            publish();wait_for(lambda:len(app.leases.rows())==count,f'admit camera {count}')
        wait_for(lambda:len(app.status()['cameras'])==5 and all(c['buffer_ready'] for c in app.status()['cameras']),'five buffered sources',timeout=45)
        continuous_start=time.monotonic();continuous_frame=app.program.frames_written
        # Freeze automatic text during deterministic editorial checks; this is a human policy/control boundary.
        human('takeover');resume()
        audio=dispatch('director',{'op':'audio','slot':1,'muted':False,'reason':'Configured fixture microphone'})
        wait_applied(audio);snapshot_audio('ambient-before')
        muted=dispatch('director',{'op':'audio','slot':1,'muted':True,'reason':'Scripted microphone mute'})
        wait_applied(muted);snapshot_audio('director-muted')
        audio=dispatch('director',{'op':'audio','slot':1,'muted':False,'reason':'Scripted microphone restore'})
        wait_applied(audio);snapshot_audio('director-unmuted')
        # Framing uses the same human validation and media path; director subject evidence is separately checked.
        crop=human('crop',rect={'x':.25,'y':.25,'width':.5,'height':.5},geometry_revision=app.program.actual_target['native']['timeline_revision']);wait_applied(crop);time.sleep(.2);take_pixel('crop')
        reset=human('reset_crop');wait_applied(reset);time.sleep(.2);take_pixel('reset')
        checks['D04']={'passed':False,'pixel_checks':pixel_checks,'aspect':16/9,'maximum_magnification':2}
        resume()
        assert not app.replays,'D03 must run without a ready replay'
        selected=dispatch('director',{'op':'live','slot':2,'independent':True,'reason':'Independent fixture angle'})
        wait_applied(selected);time.sleep(.2);take_pixel('director-camera')
        assert app.program.slot==2 and app.program.audio_slot==1
        human('live',slot=1,independent=True);resume()
        wait_applied(dispatch('director',{'op':'holding','reason':'Scripted holding cue'}))
        time.sleep(.2);take_pixel('holding')
        recovered=[]
        def scripted_recovery(role):
            assert role=='director'
            recovered.append(dispatch('director',{'op':'return_live','reason':'Scripted recovery through crew scheduler'}))
        with patch.object(app.direction,'_decide',side_effect=scripted_recovery):
            app.direction.settings=app.direction.settings.model_copy(update={'enabled':True})
            wait_for(lambda:recovered,'director scheduled during holding')
            wait_applied(recovered[0])
            app.direction.settings=app.direction.settings.model_copy(update={'enabled':False})
        checks['D03']={'passed':False,'scripted_controller_actions':['audio','graphics','live','holding','return_live'],
            'no_replay_required':True,'scheduler_recovery_action':recovered[0]['id']}
        checks['D15']=stress_media(app)
        resume()
        phrase=app.foundation.registry.labels['speech']['variants'][1]['text']
        cue=dispatch('commentator',{'op':'commentary','text':phrase,'reason':'Explicit interruption fixture'})
        wait_for(lambda:'first' in cue.delivered.get('speech',{}),'interruption fixture start')
        snapshot_audio('ambient-ducked')
        human('takeover');wait_for(lambda:app.program.cue is None,'takeover clears narration')
        wait_for(lambda:any(r['cue_id']==cue.id+'-speech' and r['state']=='interrupted' for r in rows(app)),'actual partial history')
        snapshot_audio('ambient-restored')
        checks['D09']={'passed':True,'partial_cue_id':cue.id,'history':rows(app)}
        resume()
        action=dispatch('director',{'op':'graphics','preset':'corner-label','reason':'Prepared fixture package','duration_s':2.0})
        wait_applied(action);time.sleep(.9);take_pixel('director-graphic');time.sleep(1.4)
        # Three shots change speed and repeat an earlier interval from another camera.
        prepared=human('prepare',plan=repeat_plan(app))
        wait_for(lambda:app.jobs.get(prepared.get('job_id'),{}).get('state')=='ready','ready replay',timeout=30)
        replay=human('replay',replay_id=prepared['job_id']);wait_applied(replay);resume()
        snapshot_audio('replay-silence')
        phrase=app.foundation.registry.labels['speech']['variants'][0]['text']
        replay_cue=dispatch('commentator',{'op':'commentary','text':phrase,'reason':'Explicit replay fixture'})
        wait_for(lambda:'first' in replay_cue.delivered.get('speech',{}),'replay voice')
        source_map=app.replays[prepared['job_id']].report['source_map']
        wait_for(lambda:app.program.actual_target.get('output_frame',0)>=source_map[-1]['output_frame_start']+3,'alternate angle actually submitted',timeout=10)
        replay_speech=wait_for(lambda:next((r for r in rows(app) if r['cue_id']==replay_cue.id+'-speech' and r['state']=='completed'),None),'replay speech completed')
        began=time.monotonic();wait_applied(human('live',slot=1));return_s=time.monotonic()-began
        assert return_s<=1
        wait_for(lambda:app.program.cue is None,'return cancels replay narration')
        checks['D12']={'passed':False,'replay_session':replay_cue.session_id,'replay_cue_id':replay_cue.id,'local_return_s':return_s,
            'source_map':source_map,'speech_receipt':replay_speech,'calibration_scope':'Synthetic sample receipt-clock fixture; physical capture synchronization is unknown'}
        snapshot_audio('ambient-after-replay')
        # Full-screen entry, display, and exit remain silent.
        screen=human('graphics',graphics={'op':'cue','preset':'opening','title':'LOCAL CHECK','subtitle':'Prepared fixture','duration_s':2.0})
        wait_applied(screen);snapshot_audio('full-screen-entry',.3);snapshot_audio('full-screen',.5)
        clear=human('graphics',graphics={'op':'clear','slot':'screen'});wait_applied(clear)
        snapshot_audio('full-screen-exit',.25);time.sleep(.4);snapshot_audio('after-screen')
        # Keep five-source media active for at least five minutes, while all provider calls remain labeled fixtures.
        human('takeover')
        while time.monotonic()-continuous_start<seconds:
            assert app.program.encoder.pid==encoder_pid and app.program.encoder.poll() is None
            assert app.program.actual=='LIVE'
            assert all(p.poll() is None for p in publishers)
            time.sleep(.25)
        checks['D16']={'passed':seconds>=300,'duration_s':time.monotonic()-continuous_start,'program_frames':app.program.frames_written-continuous_frame,
            'sources':len(app.leases.rows()),'encoder_pid':encoder_pid,'command_application_s':application_s}
    finally:
        for publisher in publishers:stop_process(publisher)
        app.close()
    proof=folder/'program.mkv'
    # Decode the actual encoder output. Controller acknowledgements are not acceptance.
    decoded=0;black=0;errors={};pts=[];wanted={p['program_frame']:p for p in pixel_checks}
    with av.open(str(proof)) as media:
        for frame in media.decode(video=0):
            image=frame.to_image().convert('RGB');pts.append(float(frame.pts*frame.time_base))
            if sum(ImageStat.Stat(image.resize((16,9))).mean)<5:black+=1
            if decoded in wanted:
                item=wanted[decoded];expected=Image.open(item['expected']).convert('RGB')
                # Compare an interior region with no brand, captions, or camera labels.
                region=(32,270,609,342) if item['kind'].startswith('caption') else (485,20,610,48) if item['kind']=='director-graphic' else (120,150,520,235)
                error=sum(ImageStat.Stat(ImageChops.difference(image.crop(region),expected.crop(region))).mean)/3
                errors[item['kind']]=error;image.save(folder/f"{item['kind']}-decoded.png")
            decoded+=1
    assert decoded>seconds*cfg.fps and black==0,(decoded,black)
    assert len(errors)==7 and max(errors.values())<=12,errors
    assert all(b>a for a,b in zip(pts,pts[1:])), 'Encoded timestamps are not continuous'
    checks['D04'].update(passed=True,pixel_mae=errors,tolerance_mae=12)
    checks['D03'].update(passed=True,decoded_camera_and_holding={k:errors[k] for k in ('director-camera','holding')},
        conditions={'live_decoded':True,'holding_decoded':True,'graphics_decoded':True,'scheduler_return':True,'no_ready_replay':True})
    checks['D16'].update(decoded_frames=decoded,unexpected_black_frames=black,proof=str(proof))
    pcm_path=folder/'program.pcm'
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(proof),'-vn','-ac','1','-ar','48000','-f','s16le',str(pcm_path)],check=True,timeout=60)
    output=pcm_path.read_bytes()
    from direction_media import decode_speech
    from foundation_storage import FileStorage
    from foundation_providers import Registry
    from foundation_records import FoundationSettings
    settings=FoundationSettings.load(ROOT/'config/direction.fixture.json');registry=Registry(settings);store=FileStorage(folder/'voice-reference')
    import asyncio
    speech=asyncio.run(registry.speech(registry.labels['speech']['text'],store,time.time()+10))
    reference=decode_speech(speech,store,registry.labels['speech']['text'],settings.event,settings)
    alignment=correlate(output,reference,spoken['first_sample'])
    for signal in queue_signals:
        signal['decoded_alignment']=correlate(output,Path(signal['reference']).read_bytes(),signal['first_sample'])
    checks['D10']={'passed':True,'speech':spoken,'decoded_alignment':alignment,'queued_speech':queue_signals,'proof':str(proof)}
    checks['D10']['conditions']={'speech_decoded':True,'queued_complete_audio':True}
    values=array('h',output);levels={}
    # Controller polling can return near the end of a short graphic exit.
    # Measure the actual submitted exit frames, not a wall-clock sleep after polling.
    frame_states=[json.loads(line) for line in (runtime/'program-history.jsonl').read_text().splitlines()
        if json.loads(line)['kind']=='encoder_frame_state']
    exit_start=next(i for i,r in enumerate(frame_states)
        if any(c['preset']=='opening' and c.get('exiting') for c in r['graphics']['visible']))
    exit_end=next(r for r in frame_states[exit_start+1:]
        if not any(c['preset']=='opening' for c in r['graphics']['visible']))
    exit_interval=next(state for state in states if state['name']=='full-screen-exit')
    exit_interval.update(start_sample=frame_states[exit_start]['program_frame']*3200+1600,
        end_sample=exit_end['program_frame']*3200-1600,origin='submitted graphic exit frames')
    for state in states:
        window=values[state['start_sample']:state['end_sample']]
        assert window,state
        levels[state['name']]=math.sqrt(sum(x*x for x in window)/len(window))
    (folder/'audio-report.json').write_text(json.dumps({'rms':levels,'intervals':states},indent=2)+'\n')
    assert levels['ambient-before']>500
    assert levels['director-muted']<levels['ambient-before']*.05
    assert levels['director-unmuted']>levels['ambient-before']*.65
    assert levels['ambient-restored']>levels['ambient-before']*.65
    assert levels['full-screen']<levels['ambient-before']*.05
    assert levels['full-screen-entry']<levels['ambient-before']*.05
    assert levels['full-screen-exit']<levels['ambient-before']*.05
    assert levels['replay-silence']<levels['ambient-before']*.05
    assert levels['ambient-after-replay']>levels['ambient-before']*.65
    assert max(abs(x) for x in values)<32767
    checks['D11']={'passed':True,'rms':levels,'peak':max(abs(x) for x in values),'separate_camera_sync':'unknown; no lip-sync claim',
        'mixer_gain_proof':'test_D11_mixer_duck_headroom_ramps_restore_and_bounds'}
    checks['D03']['conditions']['audio_decoded']=True
    checks['D11']['conditions']={'audio_levels_decoded':True}
    checks['D13']={'passed':True,'pixel_mae':{key:value for key,value in errors.items() if key.startswith('caption')},
        'tolerance_mae':12,'caption_receipt':caption,'safe_margins':'test_D13_caption_two_lines_fit_safe_margin_and_contrast',
        'proof':str(proof)}
    submitted=[f for line in (runtime/'program-history.jsonl').read_text().splitlines() for r in [json.loads(line)]
        if r['kind']=='program_source_map' for f in r['frames'] if f['target'].get('id')==prepared['job_id']]
    by_frame={f['target']['output_frame']:f['target'] for f in submitted}
    for shot in source_map:
        for native in shot['native_frames']:
            frame=native['output_frame']
            if frame in by_frame:
                assert by_frame[frame]['native']==native['native']
                assert by_frame[frame]['source_path']==shot['retained_media']['source_path']
    assert all(any(i in by_frame for i in range(s['output_frame_start'],s['output_frame_end'])) for s in source_map)
    checks['D12'].update(passed=True,actual_mapped_frames=len(by_frame),speeds=[s['speed'] for s in source_map],
        repeated_angles=[s['source_id'] for s in source_map if s['edit']=='repeat'],decoded_audio_levels=levels)
    replay_audio=asyncio.run(registry.speech(replay_cue.text,store,time.time()+10))
    replay_reference=decode_speech(replay_audio,store,replay_cue.text,settings.event,settings)
    checks['D12']['decoded_speech_alignment']=correlate(output,replay_reference,replay_speech['first_sample'])
    checks['D12']['conditions']={name:True for name in REQUIRED_CONDITIONS['D12'] if name!='contract_checks'}
    (folder/'media-report.json').write_text(json.dumps({'checks':checks,'pixel_checks':pixel_checks,'audio_intervals':states},indent=2)+'\n')
    return checks


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,default=Path('/evidence'))
    parser.add_argument('--offline',action='store_true',help='Partial file-only checks; cannot establish local-ready and returns nonzero')
    parser.add_argument('--diagnostic-seconds',type=int,help='Short isolated media diagnostics; skip regression acceptance and never claim local-ready')
    args=parser.parse_args();folder=args.evidence/(time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6]);folder.mkdir(parents=True)
    if args.diagnostic_seconds is not None and not 0<args.diagnostic_seconds<300:parser.error('Diagnostic seconds must be 1–299; normal acceptance uses at least 300')
    report={'prd':'19','local_ready':False,'live_verified':False,'mode':'labeled fixtures with real media',
        'started_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'coverage':{},'failures':[],
        'versions':{name:importlib.metadata.version(name) for name in ('av','pydantic','pydantic-ai-slim','fastapi','uvicorn')},
        'open_live_gates':['Verified VAST, YOLO, Cosmos, search, W&B and speech adapters','L01-L03 and listening review','Physical phones and venue/audience validation in Task 3']}
    os.environ['PYDANTIC_AI_NO_BANNER']='1'
    import pydantic_ai.models
    pydantic_ai.models.ALLOW_MODEL_REQUESTS=False
    original=socket.socket.connect
    def isolated(sock,address):
        if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):
            raise AssertionError('Fixture execution attempted an external provider connection')
        return original(sock,address)
    socket.socket.connect=isolated
    try:
        result=run_units(folder);report['unit_tests']=result
        if not result['passed']:raise AssertionError('Unit contracts failed')
        for number in (1,2,3,4,5,6,7,8,9,10,11,12,13,15,17,18):
            matches=[r for r in result['results'] if f'D{number:02}' in r['test']]
            report['coverage'][f'D{number:02}']={'contract_checks_passed':bool(matches) and all(r['passed'] for r in matches),'contract_tests':[r['test'] for r in matches]}
        contracts=dict(report['coverage'])
        report['coverage']=acceptance_coverage(contracts)
        print('Direction contracts passed',flush=True)
        if args.offline:
            report['offline_media']=offline_media(folder/'offline-media')
            report['failures'].append({'type':'IncompleteAcceptance',
                'reason':'File-only output cannot prove RTSP/WebRTC, camera admission, five-minute continuity, or browser regressions'})
            return 1
        report['marker_mapping']=marker_proof(folder)
        media=crew_media(folder/'crew-media',seconds=args.diagnostic_seconds or 300)
        report['coverage']=acceptance_coverage(contracts,media)
        print('Crew media checks finished',flush=True)
        if args.diagnostic_seconds:
            report['failures'].append({'type':'IncompleteAcceptance','reason':'Diagnostic media duration; full continuity and browser acceptance were not run'})
            return 1
        # Reuse all affected foundation, five-camera, graphics, replay, browser, and lifecycle checks.
        with (folder/'foundation-regressions.log').open('w') as log:
            subprocess.run([sys.executable,str(ROOT/'tests/media/foundation_check.py'),'--evidence',str(folder/'regressions')],stdout=log,stderr=subprocess.STDOUT,check=True)
        foundation_report=next((folder/'regressions').glob('*/report.json'))
        regression=json.loads(foundation_report.read_text())
        report['regressions']={'passed':regression['local_ready'],'report':str(foundation_report)}
        report['coverage']=acceptance_coverage(contracts,media,regression)
        report['coverage']['D14']['studio_browser_and_control_regressions']=str(foundation_report)
        report['coverage']['D19']['command']='./scripts/studio direction-check'
        report['coverage']['D19']['provider_traffic']='blocked by internal Docker network and socket guard'
        report['local_ready']=all(report['coverage'].get(f'D{i:02}',{}).get('passed') for i in range(1,20))
        if not report['local_ready']:
            report['failures'].append({'type':'IncompleteAcceptance','reason':'Required local acceptance remains incomplete; inspect missing_conditions'})
            return 1
        return 0
    except BaseException as error:
        report['failures'].append({'type':type(error).__name__,'reason':str(error),'traceback':traceback.format_exc()});return 1
    finally:
        socket.socket.connect=original
        (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n');print('Direction report: '+str(folder/'report.json'),flush=True)


def offline_media(folder):
    """Real file encoding through Program; RTSP sink and source admission are excluded.

    Use only as a partial check when Docker is inaccessible. The encoder receives
    the actual Program frames/audio. A local file replaces its network sink.
    """
    from collections import deque
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import patch
    from PIL import ImageDraw
    from media import Frame,jpeg
    from foundation_records import FoundationSettings,Geometry,SourceEpoch
    from direction_media import decode_speech
    folder.mkdir(parents=True,exist_ok=True);runtime=folder/'runtime';runtime.mkdir(exist_ok=True)
    config=json.loads((ROOT/'config/direction.fixture.json').read_text())
    config['fixture_file']=str(ROOT/'tests/fixtures/foundation/labels.json')
    config_path=folder/'fixture-config.json';config_path.write_text(json.dumps(config))
    cfg=Config(runtime,'http://localhost',delay=0,foundation_config=config_path,print_access=False)
    app=App(cfg);app.foundation.registry.labels['speech']['file']=str(ROOT/'tests/fixtures/foundation/speech.wav')
    for variant in app.foundation.registry.labels['speech']['variants']:variant['file']=str(ROOT/'tests/fixtures/direction'/Path(variant['file']).name)
    output=folder/'program.mkv';original_popen=subprocess.Popen
    def file_encoder(command,**kwargs):
        if command[0]=='ffmpeg' and command[-1]==cfg.rtsp_url('program',cfg.program_token):
            command=command[:-5]+['-f','matroska','-y',str(output)]
        return original_popen(command,**kwargs)
    native=Geometry(native_width=640,native_height=360,output_width=640,output_height=360,scaled_width=640,scaled_height=360)
    image=Image.new('RGB',(640,360),'#284534');draw=ImageDraw.Draw(image)
    colors=['#CC6666','#66CC66','#6666CC','#CCCC66']
    for n,color in enumerate(colors):draw.rectangle((n*160,120,(n+1)*160,240),fill=color)
    payload=jpeg(image);origin=time.monotonic();frames=deque()
    def frame_at(target):
        index=max(0,int((target-origin)*15))
        frame=Frame(index+1,origin+index/15,payload,index/15,index,'1/15',
            {'native_pts':index,'native_time_base':'1/15','timeline_revision':1,'geometry':native.model_dump(),'uncertainty_ms':1000/15},1,time.time())
        if not frames or frames[-1].sequence!=frame.sequence:
            frames.append(frame)
            while len(frames)>450:frames.popleft()
        return frame
    tone=array('h',[round(4000*math.sin(2*math.pi*330*i/48000)) for i in range(3200)]).tobytes()
    source=SimpleNamespace(path='camera/offline-fixture',slot=1,epoch=1,timeline_revision=1,has_audio=True,origin=origin,
        lock=__import__('threading').RLock(),frames=frames,at=frame_at,audio_at=lambda _:tone,close=lambda:None)
    source.status=lambda:{'last_frame_age_s':0.,'buffer_seconds':20.,'buffer_ready':True,'has_audio':True}
    app.sources[1]=source
    record=SourceEpoch(event_id=app.foundation.settings.event.event_id,run_id=app.control.run_id,source_id=source.path,epoch=1,slot=1,time_base='1/15')
    app.foundation.register_source(record)
    checks={};wanted={};intervals=[];encoder_pid=None
    def human(op,**args):
        value=app.control.submit({'id':uuid.uuid4().hex,'op':op,'args':args})
        if value['state']=='Rejected':raise AssertionError(value['reason'])
        wait_for(lambda:app.control.actions[value['id']]['state'] in ('Finished','On air'),'offline controller receipt',timeout=5)
        return value
    def picture(kind,crop=False,caption=None):
        with app.program.lock:index=app.program.frames_written-1
        expected=Image.open(io.BytesIO(payload)).convert('RGB')
        if crop:expected=expected.crop((160,90,480,270)).resize((640,360),Image.Resampling.LANCZOS)
        if caption:expected=Image.alpha_composite(expected.convert('RGBA'),caption).convert('RGB')
        path=folder/f'{kind}-expected.png';expected.save(path)
        wanted[index]={'kind':kind,'expected':path}
    def audio_interval(name,duration=.5):
        first=app.program.frames_written*3200;time.sleep(duration)
        intervals.append({'name':name,'first':first+3200,'last':app.program.frames_written*3200-800})
    try:
        with patch('media.subprocess.Popen',side_effect=file_encoder):
            app.program.start();app.control.start();app.foundation.start();app.direction.start()
            wait_for(lambda:app.program.frames_written>5,'offline encoder')
            encoder_pid=app.program.encoder.pid
            human('audio',slot=1,muted=True);human('live',slot=1);human('resume')
            snapshot=app.foundation.reviewed_snapshot();context=app.foundation.context('commentator',record,Interval(start=0,end=1),snapshot)
            phrase=app.foundation.registry.labels['speech']['text']
            result=LLMResult(text=json.dumps({'op':'commentary','text':phrase,'reason':'Explicit prerecorded disclosure'}),snapshot=snapshot,
                origin='fixture',model_id='offline-fixture',model_version='fixture-1')
            app.direction.dispatch(result,'commentator',context,time.time()+8)
            cue=wait_for(lambda:app.program.cue,'offline prepared voice')
            wait_for(lambda:'first' in cue.delivered.get('caption',{}),'offline caption submission')
            time.sleep(.4);picture('caption',caption=cue.caption)
            voice=wait_for(lambda:next((r for r in rows(app) if r['channel']=='speech' and r['state']=='completed'),None),'offline speech complete',timeout=9)
            wait_for(lambda:app.program.cue is None,'offline caption expiry',timeout=5)
            time.sleep(.2);picture('caption-cleared')
            queue_signals=queued_speech(app,folder/'queue-signals')
            human('crop',rect={'x':.25,'y':.25,'width':.5,'height':.5},geometry_revision=1);time.sleep(.2);picture('crop',crop=True)
            human('reset_crop');time.sleep(.2);picture('reset')
            human('audio',slot=1,muted=False);audio_interval('ambient')
            human('graphics',graphics={'op':'cue','preset':'opening','title':'LOCAL CHECK','subtitle':'File-only output','duration_s':1.5})
            audio_interval('screen-silent');time.sleep(1.6);audio_interval('restored')
            assert app.program.encoder.pid==encoder_pid and not app.program.error
    finally:app.close()
    decoded=0;errors={};black=0
    with av.open(str(output)) as media:
        for frame in media.decode(video=0):
            rendered=frame.to_image().convert('RGB')
            if sum(ImageStat.Stat(rendered.resize((16,9))).mean)<5:black+=1
            if decoded in wanted:
                item=wanted[decoded];expected=Image.open(item['expected']).convert('RGB')
                area=(32,270,609,342) if item['kind'].startswith('caption') else (120,150,520,235)
                errors[item['kind']]=sum(ImageStat.Stat(ImageChops.difference(rendered.crop(area),expected.crop(area))).mean)/3
                rendered.save(folder/f"{item['kind']}-decoded.png")
            decoded+=1
    assert len(errors)==4 and max(errors.values())<12 and black==0,(errors,black)
    pcm=folder/'program.pcm'
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(output),'-vn','-ac','1','-ar','48000','-f','s16le',str(pcm)],check=True,timeout=30)
    from foundation_providers import Registry
    from foundation_storage import FileStorage
    import asyncio
    settings=FoundationSettings.model_validate(config);registry=Registry(settings);registry.labels['speech']['file']=str(ROOT/'tests/fixtures/foundation/speech.wav')
    storage=FileStorage(folder/'reference')
    speech=asyncio.run(registry.speech(phrase,storage,time.time()+5))
    reference=decode_speech(speech,storage,phrase,settings.event,settings)
    alignment=correlate(pcm.read_bytes(),reference,voice['first_sample'])
    for signal in queue_signals:
        signal['decoded_alignment']=correlate(pcm.read_bytes(),Path(signal['reference']).read_bytes(),signal['first_sample'])
    values=array('h',pcm.read_bytes());levels={}
    for interval in intervals:
        window=values[interval['first']:interval['last']]
        levels[interval['name']]=math.sqrt(sum(x*x for x in window)/len(window))
    assert levels['ambient']>1000 and levels['screen-silent']<50 and levels['restored']>1000,levels
    report={'passed':True,'scope':'Real Program encoder to a local file; generated source fixture; no RTSP/WebRTC/admission proof',
        'decoded_frames':decoded,'unexpected_black_frames':black,'encoder_pid':encoder_pid,'pixel_mae':errors,
        'speech_alignment':alignment,'queued_speech':queue_signals,'rms':levels,'speech_receipt':voice,'program':str(output),'complete_local_acceptance':False}
    (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':raise SystemExit(main())
