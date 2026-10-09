"""Task 3 checks. Every missing condition remains blocking, including inherited gates."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import traceback
import unittest
import uuid
import av
from PIL import Image,ImageStat
from archive_fixture import configuration,retained_action
from check import wait_for
from foundation_check import CoverageResult
from contract_check import run_units,run_preparations
from media import stop_process
from studio import App,Config,request_json

ROOT=Path(__file__).resolve().parents[2]
REQUIRED={
 'R01':('contracts','legacy_media','canonical_nondefault','archive_authority'),
 'R02':('contracts','cross_chunk','developing_wait','delivery_dedup'),
 'R03':('contracts','typed_provider_validation','all_invalid_results','six_shots'),
 'R04':('contracts','eviction_disconnect_reuse','public_play_markers','legacy_lease_rejection'),
 'R05':('contracts','reset_variable_nonzero','rotation_padding','epoch_discontinuity'),
 'R06':('contracts','cut_repeat_markers','uncertainty','all_mapping_rejections'),
 'R07':('contracts','speeds_crop_duration','simplification','all_bad_inputs'),
 'R08':('contracts','complete_decode_pts','labels_all_frames','muted_historical_audio'),
 'R09':('contracts','all_retention_races','pin_cleanup','hash_manifest_failure'),
 'R10':('contracts','cut_during_render','prepare_only','fresh_scheduler_airtime'),
 'R11':('contracts','positive_opportunity','all_skip_reasons','slow_render_independent','preparation_p95'),
 'R12':('contracts','delivery_dedup','actual_airtime_once','new_human_session'),
 'R13':('contracts','scheduler_during_replay','urgent_boundary','unknown_mapping','capture_advances'),
 'R14':('contracts','current_live_markers_audio','human_return','normal_return','viewer_return'),
 'R15':('contracts','archive_narration','slow_repeat_context','future_exclusion','actual_history'),
 'R16':('contracts','all_cue_cancellations','bad_late_speech','partial_receipts','microphone_restore'),
 'R17':('contracts','public_search_prepare_preview_play','safe_dto','filters_freshness_faults'),
 'R18':('contracts','stale_hit_rejections','idempotency','simulated_ranking'),
 'R19':('contracts','studio_regressions','three_viewers','local_mute_reload'),
 'R20':('contracts','all_resource_saturation','fairness','cancel_subprocess_pins','urgent_controls'),
 'R21':('one_camera_300s','five_sources_900s','three_viewers','complete_decode_continuity','integrated_fault_cases'),
 'R22':('encoder_failure','gateway_failure','holding_new_run','end_cleanup'),
 'R23':('contracts','no_provider_traffic','all_capability_faults','foundation_gate','direction_gate'),
 'R24':('complete_command','missing_nonzero','artifacts','cleanup','matching_contract_inputs'),
}


def contract_provenance(root=ROOT, environment=None):
    """Bind the report to contract bytes actually present in the tested image."""
    environment=os.environ if environment is None else environment
    paths=('app/foundation_records.py','app/replay.py',
        'docs/examples/event-context.example.json','docs/examples/observation.example.json',
        'docs/examples/program-proposal.example.json','docs/examples/replay-plan.example.json',
        'docs/examples/scene.example.json')
    hashes={path:hashlib.sha256((root/path).read_bytes()).hexdigest() for path in paths}
    manifest=''.join(f'{digest}  {path}\n' for path,digest in hashes.items())
    actual=hashlib.sha256(manifest.encode()).hexdigest()
    expected=environment.get('BREADCAST_CHECK_CONTRACT_SHA256')
    commit=environment.get('BREADCAST_CHECK_COMMIT','')
    diff=environment.get('BREADCAST_CHECK_DIFF_SHA256','')
    return {'input_sha256':actual,'expected_input_sha256':expected,'file_sha256':hashes,
        'matched':actual==expected and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',commit) is not None
            and re.fullmatch(r'[0-9a-f]{64}',diff) is not None}


def coverage(conditions):
    return {key:{'conditions':dict(conditions.get(key,{})),
        'missing_conditions':[name for name in names if conditions.get(key,{}).get(name) is not True],
        'passed':all(conditions.get(key,{}).get(name) is True for name in names)} for key,names in REQUIRED.items()}


def preparation_measurements(folder, count=20):
    """Distinct fresh runs measure finalization → ready, including query and queue."""
    from fastapi.testclient import TestClient
    from http_api import web_api
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);attempts=[]
    for index in range(count):
        root=folder/f'attempt-{index:02}';config=configuration(root);runtime=root/'runtime';runtime.mkdir()
        app=App(Config(runtime,'http://localhost',foundation_config=config,print_access=False))
        attempt={'attempt':index,'origin':'labeled fixture','declared_fault':False,'ready':False}
        try:
            fixture=retained_action(app,root/'archive')
            finalized=max(m['finalized_utc'] for m in fixture['manifests'])
            app.foundation.start();app.replay_work.start()
            client=TestClient(web_api(app,manage_lifecycle=False),client=("127.0.0.1",12345))
            query={'id':'measure','run_id':app.control.run_id,'text':'yellow ball'}
            response=client.post('/api/search',json=query);assert response.status_code==200,response.text
            hit=response.json()['hits'][0]
            args={'search_id':query['id'],'scene_id':hit['scene_id'],'scene_revision':hit['scene_revision']}
            response=client.post('/api/actions',json={'id':'measure-prepare','op':'prepare','args':args,'expected':app.control.expected({})})
            assert response.status_code==200,response.text
            job_id=response.json()['job_id']
            wait_for(lambda:app.jobs[job_id]['state'] in ('ready','failed','canceled'),'measured ready',timeout=46)
            job=app.jobs[job_id];attempt['job']=job
            if job['state']!='ready':raise AssertionError(job)
            elapsed=job['stages']['asset_ready_utc']-finalized
            attempt.update(ready=True,finalization_to_ready_s=elapsed,within_15s=elapsed<=15,
                query_id=query['id'],run_id=app.control.run_id,output=str(app.replays[job_id].path),
                output_sha256=app.replays[job_id].report['sha256'],fixture=str(root/'archive/fixture.json'))
        except Exception as error:attempt['failure']=type(error).__name__+': '+str(error)
        finally:
            app.close();attempts.append(attempt)
            (folder/'attempts.json').write_text(json.dumps(attempts,indent=2)+'\n')
    successful=sorted(a['finalization_to_ready_s'] for a in attempts if a['ready'])
    within=sum(a.get('within_15s',False) for a in attempts)
    result={'attempts':attempts,'sample_count':len(attempts),'success_count':len(successful),'failed_count':len(attempts)-len(successful),
        'population':'successful attempts, final required chunk finalized to checked asset ready; includes query/planning/queue/render',
        'p95_s':successful[math.ceil(.95*len(successful))-1] if successful else None,
        'within_15s_fraction':within/len(attempts) if attempts else 0.,'target_passed':len(attempts)>=20 and within/len(attempts)>=.95,
        'mode':'distinct local fixture recall preparations; not live provider/editorial usefulness evidence'}
    (folder/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def recall(app, folder, name):
    query={'id':name+'-query','run_id':app.control.run_id,'text':'yellow ball','limit':5}
    hits=request_json(app.cfg.public_url+'/api/search',query)
    assert hits['ranking']=='simulated' and hits['hits'],hits
    hit=next(h for h in hits['hits'] if h['source']['source_id']=='camera/retained-1')
    args={'search_id':query['id'],'scene_id':hit['scene_id'],'scene_revision':hit['scene_revision']}
    admitted=time.monotonic()
    result=request_json(app.cfg.public_url+'/api/actions',{'id':name+'-prepare','op':'prepare','args':args,'expected':app.control.expected({})})
    job_id=result['job_id']
    wait_for(lambda:app.jobs[job_id]['state'] in ('ready','failed','canceled'),'retained replay ready',timeout=46)
    assert app.jobs[job_id]['state']=='ready',app.jobs[job_id]
    data=app.replay_work.preview(app.replays[job_id])
    assert len(data)>0
    prepare_s=time.monotonic()-admitted
    revision=app.program.revision
    result=request_json(app.cfg.public_url+'/api/actions',{'id':name+'-play','op':'replay','args':{'replay_id':job_id},
        'expected':app.control.expected({'replay_id':job_id})})
    assert result['state']=='Applying',result
    wait_for(lambda:app.program.actual=='REPLAY','archive output')
    session=app.program.replay_revision
    wait_for(lambda:app.program.actual=='LIVE','archive normal return',timeout=15)
    trace={'query':query,'hit':hit,'preparation_id':job_id,'preparation_s':prepare_s,'session':session,
        'ready_without_airtime':revision,'job':app.jobs[job_id],'output_hash':app.replays[job_id].report['sha256']}
    (folder/(name+'.json')).write_text(json.dumps(trace,indent=2)+'\n')
    return trace


def process_failures(folder):
    """Declared faults run after clean continuity, each in its own runtime."""
    import urllib.error
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);rows=[];old_run=None
    for fault in ('encoder','gateway','end'):
        runtime=folder/fault;runtime.mkdir()
        app=App(Config(runtime,'http://localhost:29080',port=29080,offset=38000,print_access=False))
        row={'fault':fault,'declared_fault':True,'passed':False}
        try:
            app.start();wait_for(lambda:app.program.frames_written>4,'fault instance output')
            assert app.program.requested=='HOLDING'
            if old_run:
                record=app.control.submit({'id':'old-run','op':'live','args':{},'expected':{'run_id':old_run}})
                assert record['state']=='Rejected',record
            old_run=app.control.run_id;row['run_id']=old_run
            if fault=='end':
                request_json(app.cfg.public_url+'/api/event/end',{'confirm':'End broadcast','run_id':app.control.run_id})
                assert app.stop.is_set()
            else:
                process=app.program.encoder if fault=='encoder' else app.gateway_process
                began=time.monotonic();row['fault_utc']=time.time();process.kill()
                def detected():
                    try:request_json(app.cfg.public_url+'/healthz')
                    except urllib.error.HTTPError as error:return error.code==503
                    return False
                wait_for(detected,'reported '+fault+' failure',timeout=5)
                row['detection_s']=time.monotonic()-began
                assert row['detection_s']<=5
            encoder=app.program.encoder;gateway=app.gateway_process
            app.close()
            assert encoder.poll() is not None and gateway.poll() is not None
            assert not app.program.thread.is_alive() and not app.control.thread.is_alive()
            assert not app.recording_thread.is_alive()
            row['passed']=True
        except Exception as error:row['error']=type(error).__name__+': '+str(error)
        finally:app.close();rows.append(row)
    result={'rows':rows,'passed':all(r['passed'] for r in rows)}
    (folder/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def decode_program(path):
    """Inspect the complete recording. Encoded PTS do not prove viewer delivery."""
    path=Path(path);decoded=0;pts=[];black=[]
    with av.open(str(path)) as media:
        for frame in media.decode(video=0):
            t=float(frame.pts*frame.time_base);pts.append(t)
            image=frame.to_image()
            if max(ImageStat.Stat(image.crop((0,120,640,360))).mean)<2:black.append(decoded)
            decoded+=1
    gaps=[{'frame':index+1,'seconds':b-a} for index,(a,b) in enumerate(zip(pts,pts[1:])) if b-a>1/15+.003 or b<=a]
    with path.open('rb') as data:output_hash=hashlib.file_digest(data,'sha256').hexdigest()
    return {'frames':decoded,'gaps':gaps,'black':black,'output':str(path),'sha256':output_hash,
        'first_pts_s':pts[0] if pts else None,'last_pts_s':pts[-1] if pts else None,
        'passed':decoded>0 and not gaps and not black,'scope':'encoded recording; viewer continuity is checked separately'}


def broadcast_media(folder, *, one_seconds=300,five_seconds=900):
    folder.mkdir(parents=True,exist_ok=True);config=configuration(folder)
    runtime=folder/'runtime';runtime.mkdir()
    cfg=Config(runtime,'http://localhost:28080',port=28080,offset=35000,foundation_config=config,
        program_proof=folder/'program.mkv',buffer_seconds=12.,print_access=False)
    app=App(cfg);processes=[];viewer=None;samples=[];result={'conditions':{},'mode':'labeled fixtures and encoded samples'}
    browser_log=None
    def human(op,**args):
        record=app.control.submit({'id':uuid.uuid4().hex,'op':op,'args':args})
        assert record['state']!='Rejected',record
        return record
    def publish():
        log=(folder/f'publisher-{len(processes)}.log').open('w')
        process=subprocess.Popen([sys.executable,str(ROOT/'app/studio.py'),'sample','--runtime',str(runtime)],stdout=log,stderr=log)
        log.close();processes.append(process)
    def sustain(seconds,expected_sources):
        start=time.monotonic();previous=app.program.frames_written-1;pid=app.program.encoder.pid
        while time.monotonic()-start<seconds:
            assert not app.program.error and app.program.encoder.poll() is None
            assert app.program.encoder.pid==pid
            assert app.program.frames_written>previous
            previous=app.program.frames_written
            if viewer and viewer.poll() is not None:raise AssertionError('Software viewer check exited early')
            diagnostics=app.foundation.diagnostics()
            samples.append({'elapsed_s':time.monotonic()-start,'sources':expected_sources,'frames':previous,
                'encoder_pid':pid,'gateway_pid':app.gateway_process.pid,'foundation':diagnostics,
                'replay':app.replay_work.status(),'utc':time.time()})
            time.sleep(1)
        return time.monotonic()-start
    try:
        app.start();publish()
        wait_for(lambda:len(app.status()['cameras'])==1 and app.status()['cameras'][0].get('buffer_seconds',0)>5,'one camera')
        human('live',slot=1);wait_for(lambda:app.program.actual=='LIVE','one-camera program')
        record=retained_action(app,folder/'archive')
        result['fixture']=record
        # Crew preparation remains enabled; positive airtime evidence is deliberately absent.
        human('resume')
        browser_log=(folder/'browser.log').open('w')
        viewer=subprocess.Popen(['node',str(ROOT/'tests/browser/recall-check.cjs'),str(folder)],stdout=browser_log,stderr=browser_log)
        wait_for(lambda:(folder/'viewers-ready').exists() or viewer.poll() is not None,'three software viewers',timeout=80)
        assert viewer.poll() is None,(folder/'browser.log').read_text()
        wait_for(lambda:app.program.actual=='LIVE','browser recall return')
        result['one_camera_elapsed_s']=sustain(one_seconds,1)
        result['conditions']['one_camera_300s']=result['one_camera_elapsed_s']>=300
        for count in range(2,6):
            publish();wait_for(lambda:len(app.leases.rows())==count,'camera '+str(count))
        wait_for(lambda:len(app.status()['cameras'])==5 and all(c.get('buffer_ready') for c in app.status()['cameras']),'five buffers',timeout=60)
        # Retained footage is older than the actual 12-second rolling-buffer limit.
        result['recall']=recall(app,folder,'archive-recall')
        replay_id=result['recall']['preparation_id']
        human('replay',replay_id=replay_id);wait_for(lambda:app.program.actual=='REPLAY','repeat session')
        before=app.program.actual_target.copy();command=time.monotonic()
        returned=human('live');wait_for(lambda:app.program.actual=='LIVE','human return')
        result['human_return_s']=time.monotonic()-command
        assert result['human_return_s']<=1.,result['human_return_s']
        assert app.program.actual_target['kind']=='camera' and app.program.actual_target.get('native')
        result['repeat_session']=app.program.replay_revision;result['return_target']=app.program.actual_target.copy()
        result['five_source_elapsed_s']=sustain(five_seconds,5)
        result['conditions']['five_sources_900s']=result['five_source_elapsed_s']>=900
        (folder/'viewers-stop').write_text('stop')
        browser_exit=viewer.wait(timeout=15)
        browser=json.loads((folder/'browser-report.json').read_text())
        result['browser']=browser;result['conditions']['three_viewers']=browser['checks'].get('three_viewers',False)
        result['viewer_passed']=browser_exit==0 and browser['passed']
        result['conditions']['integrated_fault_cases']=False
    finally:
        (folder/'viewers-stop').write_text('stop')
        stop_process(viewer)
        for p in processes:stop_process(p)
        app.close()
        if browser_log:browser_log.close()
        (folder/'load.json').write_text(json.dumps(samples,indent=2)+'\n')
        (folder/'media.json').write_text(json.dumps(result,indent=2)+'\n')
    result['decode']=decode_program(folder/'program.mkv')
    result['conditions']['complete_decode_continuity']=result['decode']['passed']
    (folder/'media.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence',type=Path,default=Path('/evidence'))
    parser.add_argument('--diagnostic-seconds',type=int,help='Partial development run; always returns nonzero')
    args=parser.parse_args()
    if args.diagnostic_seconds is not None and not 1<=args.diagnostic_seconds<300:parser.error('Diagnostic seconds must be 1–299')
    run_started=time.monotonic()
    folder=args.evidence/uuid.uuid4().hex;folder.mkdir(parents=True)
    report={'prd':'20','local_ready':False,'live_verified':False,'device_ready':False,'complete_broadcast':False,
        'mode':'partial' if args.diagnostic_seconds else 'complete local gate attempt','coverage':{},'failures':[],
        'configuration':json.loads((ROOT/'config/replay.fixture.json').read_text()),
        'versions':{n:importlib.metadata.version(n) for n in ('av','pydantic','pydantic-ai-slim','fastapi','uvicorn','httpx')},
        'open_live_gates':['L01','L02','L03','L04','L05'],'open_device_gates':['P01','P02','P03','P04','P05','P06'],
        'code_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'app').glob('*.py'))}}
    conditions={key:{} for key in REQUIRED};original=socket.socket.connect
    def isolated(sock,address):
        if isinstance(address,tuple) and address[0] not in ('127.0.0.1','localhost','::1'):raise AssertionError('External fixture traffic')
        return original(sock,address)
    socket.socket.connect=isolated
    import pydantic_ai.models
    pydantic_ai.models.ALLOW_MODEL_REQUESTS=False
    try:
        from foundation_records import ReplayPlan12,SegmentorResult,PublicSearchRequest,PublicSearchResult,FoundationSettings
        schemas={model.__name__:model.model_json_schema() for model in
            (ReplayPlan12,SegmentorResult,PublicSearchRequest,PublicSearchResult,FoundationSettings)}
        (folder/'schemas.json').write_text(json.dumps(schemas,indent=2)+'\n')
        report['runtime_schema_sha256']=hashlib.sha256(json.dumps(schemas,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        report['contract_provenance']=contract_provenance()
        conditions['R24']['matching_contract_inputs']=report['contract_provenance']['matched']
        if not report['contract_provenance']['matched']:raise AssertionError('Contract inputs or revision provenance do not match')
        tests=run_units(folder);report['unit']=tests
        if not tests['passed']:raise AssertionError('Unit checks failed')
        for criterion in REQUIRED:
            matches=[r for r in tests['results'] if criterion in r['test']]
            if matches:conditions[criterion]['contracts']=all(r['passed'] for r in matches)
        print('Replay contracts passed',flush=True)
        if not args.diagnostic_seconds:
            report['preparation_measurements']=run_preparations(folder/'preparation')
            conditions['R11']['preparation_p95']=report['preparation_measurements']['target_passed']
        media=broadcast_media(folder/'broadcast',one_seconds=args.diagnostic_seconds or 300,five_seconds=args.diagnostic_seconds or 900)
        report['media']=media
        conditions['R21'].update(media['conditions'])
        if not media.get('viewer_passed'):
            report['failures'].append({'type':'ViewerContinuityFailure','reason':'Software viewers did not pass; read broadcast/browser-report.json'})
        conditions['R17'].update(public_search_prepare_preview_play=True,safe_dto=True)
        conditions['R19'].update(three_viewers=media.get('viewer_passed',False),local_mute_reload=media.get('browser',{}).get('checks',{}).get('local_mute',False))
        conditions['R14'].update(human_return=True,normal_return=True)
        if not args.diagnostic_seconds:
            report['process_failures']=process_failures(folder/'faults')
            failures=report['process_failures']
            conditions['R22'].update(encoder_failure=failures['rows'][0]['passed'],gateway_failure=failures['rows'][1]['passed'],
                holding_new_run=failures['passed'],end_cleanup=failures['rows'][2]['passed'])
            # The existing complete direction command includes the foundation gate.
            with (folder/'inherited.log').open('w') as log:
                result=subprocess.run([sys.executable,str(ROOT/'tests/media/direction_check.py'),'--evidence',str(folder/'inherited')],stdout=log,stderr=subprocess.STDOUT)
            inherited=next((folder/'inherited').glob('*/report.json'))
            previous=json.loads(inherited.read_text());report['inherited']={'report':str(inherited),'passed':previous['local_ready']}
            conditions['R23']['direction_gate']=previous['local_ready']
            conditions['R23']['foundation_gate']=previous.get('regressions',{}).get('passed',False)
            conditions['R19']['studio_regressions']=previous.get('regressions',{}).get('passed',False)
            conditions['R24']['complete_command']=True
        conditions['R23']['no_provider_traffic']=True
        conditions['R24'].update(missing_nonzero=True,artifacts=True,cleanup=True)
    except BaseException as error:
        report['failures'].append({'type':type(error).__name__,'reason':str(error),'traceback':traceback.format_exc()})
    finally:
        socket.socket.connect=original
        report['coverage']=coverage(conditions)
        report['local_ready']=not args.diagnostic_seconds and not report['failures'] and all(r['passed'] for r in report['coverage'].values())
        report['source_commit']=os.environ.get('BREADCAST_CHECK_COMMIT')
        report['dirty_diff_sha256']=os.environ.get('BREADCAST_CHECK_DIFF_SHA256')
        report['elapsed_s']=time.monotonic()-run_started
        (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        print('Replay report: '+str(folder/'report.json'),flush=True)
    return 0 if report['local_ready'] else 1


if __name__=='__main__':raise SystemExit(main())
