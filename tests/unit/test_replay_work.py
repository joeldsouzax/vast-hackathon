"""Archive identity, canonical plans, search and scheduling through production boundaries."""
import asyncio
from collections import deque
from fractions import Fraction
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import av
from fastapi.testclient import TestClient
from pydantic import ValidationError
from PIL import Image
from archive_fixture import configuration, retained_action
from foundation_records import (Interval, PublicSearchRequest, ReplayPlan12, SegmentorResult, TimeMapping,
    ShotIntent, SegmentPlan, SourceEpoch, LLMResult, ReplaySettings, ReplayCandidate)
from http_api import web_api
from media import Frame,jpeg
from replay_inputs import resolve_plan
from replay import render_plan
from replay_work import ReplayError
from studio import App,Config


class ReplayContracts(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name)
        config=configuration(self.root)
        (self.root/'runtime').mkdir()
        self.app=App(Config(self.root/'runtime','http://localhost',foundation_config=config,print_access=False))
        self.f=self.app.foundation;self.work=self.app.replay_work
        self.record=retained_action(self.app,self.root/'input');self.plan=ReplayPlan12.model_validate(self.record['plan'])
        self.scene=self.work.scene(self.record['scene_id'],self.record['scene_revision'])
        self.client=TestClient(web_api(self.app,manage_lifecycle=False),client=("127.0.0.1",12345))

    def tearDown(self):self.app.close();self.folder.cleanup()

    def render(self, plan=None):
        resolved=resolve_plan(self.app,plan or self.plan,'render-test',time.time()+15)
        return render_plan(self.app.cfg,'encoded',resolved)

    def test_R01_R03_strict_canonical_identity_and_six_shots(self):
        for update in ({'schema_version':'9'},{'event_id':'other'},{'run_id':'old'},{'unknown':True},{'replay_marker':1},{'output':{'width':640.0}}):
            with self.assertRaises(ValidationError):ReplayPlan12.model_validate({**self.plan.model_dump(),**update})
        intent=SegmentPlan(op='plan',source=self.plan.source,action=self.plan.action,required=self.plan.required,
            shots=[self.plan.shots[0]]*6,reason='Six bounded shots fit typed payload')
        result=SegmentorResult(payload=intent,snapshot=self.plan.snapshot,origin='fixture',model_id='fixture',model_version='1')
        self.assertGreater(len(result.model_dump_json()),2048)
        self.assertLess(len(result.model_dump_json()),65536)
        for field in ('shell','actor','url','approval'):
            with self.assertRaises(ValidationError):SegmentorResult.model_validate({**result.model_dump(),field:'forbidden'})
        changed=self.plan.model_copy(update={'input_kind':'live_buffer'})
        with self.assertRaisesRegex(ValueError,'lease'):resolve_plan(self.app,changed,'forged',time.time()+15)
        self.assertEqual(self.f.diagnostics()['pins'],0)

    def test_R04_R05_R08_retained_two_reset_chunks_and_original_markers(self):
        # The old rolling buffer has actually been emptied; its slot has a new owner.
        old=SimpleNamespace(frames=deque([object()]),path=self.plan.source.source_id,epoch=1)
        old.frames.clear();self.assertFalse(old.frames)
        self.app.sources[1]=SimpleNamespace(path='camera/replacement',epoch=2,slot=1,close=lambda:None)
        replay=self.render()
        self.assertTrue(replay.report['continuous_pts']);self.assertTrue(replay.report['decode_passed'])
        self.assertEqual(replay.duration,4.)
        native=replay.report['source_map'][0]['native_frames']
        self.assertIsNone(native[0]['source_sequence'])
        self.assertEqual(native[30]['native']['native_pts'],30720)
        self.assertEqual(native[30]['native']['file_pts'],0)
        self.assertNotEqual(native[0]['native']['chunk_id'],native[30]['native']['chunk_id'])
        with av.open(str(replay.path)) as media:
            self.assertFalse(media.streams.audio)
            frames=list(media.decode(video=0))
        from foundation_check import marker_id
        # Replay fixtures encode bits along the bottom; decode every source index.
        markers=[]
        for frame in frames:
            image=frame.to_image();value=0
            for bit in range(8):
                if sum(image.getpixel((16+bit*18,343)))>400:value|=1<<bit
            markers.append(value)
        self.assertEqual(markers,list(range(60)))
        self.assertEqual(self.f.diagnostics()['pins'],0)

    def test_R05_variable_nonzero_file_pts_and_geometry(self):
        record=retained_action(self.app,self.root/'variable',camera=2,variable=True,start_pts=4096)
        replay=self.render(ReplayPlan12.model_validate(record['plan']))
        native=replay.report['source_map'][0]['native_frames']
        self.assertGreater(native[0]['native']['file_pts'],0)
        self.assertEqual(native[0]['native']['native_pts'],0)
        self.assertEqual(native[16]['native']['native_pts'],native[15]['native']['native_pts'])
        self.assertEqual(replay.report['output_frames'],60)

    def test_R06_archive_mapping_cuts_and_explicit_repeat(self):
        record=retained_action(self.app,self.root/'angle',camera=2)
        other=ReplayPlan12.model_validate(record['plan'])
        for source in (self.plan.source,other.source):
            self.f.register_mapping(TimeMapping(source=source,revision=1,valid=Interval(start=0,end=61440),
                origin_pts=0,offset_event_ms=0,uncertainty_ms=3.,calibration_evidence=['inspected-fixture-markers']))
        first=self.plan.shots[0].model_copy(update={'native':Interval(start=0,end=30720),'event':Interval(start=0,end=2000),'mapping_revision':1})
        second=other.shots[0].model_copy(update={'native':Interval(start=30720,end=61440),'event':Interval(start=2000,end=4000),'mapping_revision':1})
        repeat=other.shots[0].model_copy(update={'native':Interval(start=0,end=30720),'event':Interval(start=0,end=2000),'mapping_revision':1,'edit':'repeat','speed':.5})
        plan=self.plan.model_copy(update={'shots':[first,second,repeat],'snapshot':self.f.reviewed_snapshot()})
        same_camera_epoch=repeat.model_copy(update={'source':first.source.model_copy(update={'epoch':2})})
        with self.assertRaisesRegex(ValidationError,'another source'):
            ReplayPlan12.model_validate({**plan.model_dump(),'shots':[first.model_dump(),second.model_dump(),same_camera_epoch.model_dump()]})
        replay=self.render(plan)
        self.assertEqual(replay.report['camera_order'],[self.plan.source.source_id,other.source.source_id,other.source.source_id])
        self.assertEqual(replay.duration,8.)
        self.f.invalidate_mapping(other.source,1,'Visible marker correction')
        with self.assertRaisesRegex(ValueError,'mapping'):resolve_plan(self.app,plan,'invalid',time.time()+15)
        self.assertEqual(self.f.diagnostics()['pins'],0)

    def test_R07_speeds_crop_and_missing_coverage(self):
        for speed in (.5,1.,2.):
            shot=self.plan.shots[0].model_copy(update={'speed':speed})
            replay=self.render(self.plan.model_copy(update={'shots':[shot]}))
            self.assertAlmostEqual(replay.duration,4/speed)
        from foundation_records import CropRect
        crop=CropRect(x=.25,y=.25,width=.5,height=.5)
        replay=self.render(self.plan.model_copy(update={'shots':[self.plan.shots[0].model_copy(update={'crop':crop})]}))
        self.assertEqual(replay.report['source_map'][0]['crop_transform']['crop_pixels_ltrb'],[160,90,480,270])
        weak=self.plan.shots[0].model_copy(update={'evidence_ids':[self.plan.shots[0].evidence_ids[0]]})
        with self.assertRaisesRegex(ValueError,'coverage'):resolve_plan(self.app,self.plan.model_copy(update={'shots':[weak]}),'gap',time.time()+15)

    def test_R09_pin_ownership_cleanup_hash_and_retraction(self):
        replay=self.render();self.app.replays[replay.id]=replay
        ticket=self.work.preload(replay,'play-test')
        self.f.resolve(self.plan.source,self.plan.required,owner='other-consumer',deadline_utc=time.time()+30)
        self.f.settings.limits=self.f.settings.limits.model_copy(update={'archive_bytes':1})
        self.f.cleanup()
        self.assertTrue(self.work.ticket_valid(ticket))
        self.assertTrue(self.f.resolve(self.plan.source,self.plan.required)['available'])
        self.work.release_ticket('play-test')
        self.f.cleanup()
        self.assertTrue(self.f.resolve(self.plan.source,self.plan.required)['available'])
        self.f.release('other-consumer');self.f.cleanup()
        self.assertFalse(self.work.ticket_valid(ticket))
        self.assertEqual(self.f.diagnostics()['pins'],0)

    def test_R09_hash_changes_and_retraction_revoke_preloaded_ticket(self):
        replay=self.render();self.app.replays[replay.id]=replay
        ticket=self.work.preload(replay,'play-test')
        self.f.retract(self.scene.evidence_ids[0],'correction','Inspected source was withdrawn')
        self.assertFalse(self.work.ticket_valid(ticket))
        self.work.release_ticket('play-test')
        self.assertEqual(self.f.diagnostics()['pins'],0)
        replay.path.write_bytes(b'changed')
        with self.assertRaises(ValueError):self.work.preload(replay,'play-corrupt')
        self.assertEqual(self.f.diagnostics()['pins'],0)

    def test_R17_R18_search_safe_idempotent_and_retraction(self):
        self.f.start()
        request={'id':'find','run_id':self.app.control.run_id,'text':'yellow ball','limit':5}
        result=self.client.post('/api/search',json=request)
        self.assertEqual(result.status_code,200,result.text)
        public=result.json();self.assertEqual(public['ranking'],'simulated');self.assertTrue(public['hits'])
        raw=json.dumps(public)
        for forbidden in ('/tmp/','path','secret','file_pts','manifest'):
            self.assertNotIn(forbidden,raw)
        self.assertEqual(self.client.post('/api/search',json=request).json(),public)
        self.assertEqual(self.client.post('/api/search',json={**request,'text':'changed'}).status_code,409)
        self.assertEqual(self.client.post('/api/search',json={**request,'limit':11}).status_code,422)
        self.assertEqual(self.app.program.revision,0);self.assertFalse(self.app.control.crew_paused)
        hit=public['hits'][0]
        self.f.retract(self.scene.evidence_ids[0],'retract','Labeled correction')
        action={'id':'prepare','op':'prepare','args':{'search_id':'find','scene_id':hit['scene_id'],'scene_revision':hit['scene_revision']},'expected':self.app.control.expected({})}
        response=self.client.post('/api/actions',json=action)
        self.assertEqual(response.status_code,410,response.text)
        self.assertEqual(response.json()['reason'],'evidence_retracted')

    def test_R02_R03_R10_typed_segmentor_and_prepare_only(self):
        snapshot=self.f.reviewed_snapshot()
        context=self.f.context('segmentor',self.plan.source,self.plan.required,snapshot,archive=True)
        context['target']['scene_id']=self.scene.scene_id
        result=asyncio.run(self.f.registry.llm('segmentor',context,snapshot,time.time()+10))
        self.assertEqual(result.payload.op,'plan')
        partial=context.copy();partial['scenes']=[{**context['scenes'][0],'native':{'start':0,'end':30720}}]
        wait=asyncio.run(self.f.registry.llm('segmentor',partial,snapshot,time.time()+10))
        self.assertEqual(wait.payload.op,'wait')
        # Canonical preparation ignores a normal camera cut during rendering.
        self.app.program.revision+=1
        replay=self.render()
        self.assertEqual(self.app.program.requested,'HOLDING')
        self.assertTrue(self.work.eligible(replay))

    def test_R04_R17_public_query_prepare_preview_play_after_slot_reuse(self):
        self.f.start();self.work.start()
        request={'id':'recall','run_id':self.app.control.run_id,'text':'yellow ball'}
        result=self.client.post('/api/search',json=request)
        self.assertEqual(result.status_code,200,result.text)
        hit=result.json()['hits'][0]
        newer=SourceEpoch(event_id=self.plan.event_id,run_id=self.plan.run_id,source_id='camera/reused',epoch=2,slot=1,time_base='1/15360')
        self.f.register_source(newer)
        self.app.sources[1]=SimpleNamespace(slot=1,path=newer.source_id,epoch=2,close=lambda:None)
        args={'search_id':'recall','scene_id':hit['scene_id'],'scene_revision':hit['scene_revision']}
        action={'id':'recall-prepare','op':'prepare','args':args,'expected':self.app.control.expected({})}
        response=self.client.post('/api/actions',json=action)
        self.assertEqual(response.status_code,200,response.text)
        job_id=response.json()['job_id'];end=time.monotonic()+15
        while self.app.jobs[job_id]['state'] not in ('ready','failed','canceled') and time.monotonic()<end:time.sleep(.05)
        self.assertEqual(self.app.jobs[job_id]['state'],'ready',self.app.jobs[job_id])
        self.assertFalse(self.app.control.crew_paused)
        self.assertEqual(self.app.program.revision,0)
        preview=self.client.get('/api/replay-media/'+job_id)
        self.assertEqual(preview.status_code,200,preview.text[:100] if preview.status_code!=200 else '')
        self.assertEqual(self.app.program.revision,0)
        played=self.app.control.submit({'id':'recall-play','op':'replay','args':{'replay_id':job_id}})
        self.assertEqual(played['state'],'Applying',played)
        self.assertTrue(self.app.control.crew_paused)
        self.assertEqual(self.app.program.replay.report['source_map'][0]['retained_media']['source_path'],self.plan.source.source_id)
        self.assertTrue(self.work.ticket_valid(self.app.program.replay_ticket))

    def test_R20_R23_settings_capabilities_and_limits(self):
        for changes in ({'jobs':21},{'ready_assets':3},{'query_s':6.},{'memory_bytes':268435457},{'recall_s':45.,'recall_expiry_s':30.}):
            with self.assertRaises(ValidationError):ReplaySettings.model_validate(changes)
        self.f.settings.replay=self.f.settings.replay.model_copy(update={'input_bytes':1})
        with self.assertRaisesRegex(ValueError,'capacity'):resolve_plan(self.app,self.plan,'overbudget',time.time()+15)
        self.assertEqual(self.f.diagnostics()['pins'],0)
        self.work.settings=self.work.settings.model_copy(update={'query_records':1})
        self.f.start();self.work.search(PublicSearchRequest(id='only',run_id=self.app.control.run_id,text='ball'))
        with self.assertRaises(ReplayError):self.work.search(PublicSearchRequest(id='full',run_id=self.app.control.run_id,text='ball'))

    def live_source(self, pts=61439):
        from foundation_records import Geometry
        geometry=Geometry(native_width=640,native_height=360,output_width=640,output_height=360,scaled_width=640,scaled_height=360)
        native={'native_pts':pts,'native_time_base':'1/15360','timeline_revision':1,'geometry':geometry.model_dump(),'uncertainty_ms':0.0}
        frame=Frame(61,time.monotonic(),jpeg(Image.new('RGB',(640,360),'green')),pts/15360,pts,'1/15360',native,1,time.time())
        source=SimpleNamespace(path=self.plan.source.source_id,slot=1,epoch=1,timeline_revision=1,lock=threading.RLock(),
            frames=deque([frame]),at=lambda _:frame,has_audio=False,close=lambda:None)
        self.app.sources[1]=source
        original=self.app.foundation_snapshot
        def snapshot():
            value=original();value['runtime']['source_health']=[{'source_id':source.path,'slot':1,'epoch':1,'state':'ACTIVE',
                'last_frame_age_s':0.,'buffer_seconds':6.,'buffer_ready':True,'has_audio':False}]
            return value
        self.f.snapshot_reader=snapshot
        self.app.program.command('live',self.app.program.revision,1)
        self.app.program.actual='LIVE';self.app.program.actual_target={'kind':'camera','source_path':source.path,'epoch':1,'native':native}
        self.app.control.program_started=True
        return source

    def test_R11_R12_finite_opportunity_expiry_and_no_automatic_repeat(self):
        self.live_source();replay=self.render();self.app.replays[replay.id]=replay
        from foundation_records import ReplayCandidate
        candidate=ReplayCandidate(candidate_id='logical-action',scene_id=self.scene.scene_id,scene_revision=self.scene.revision,
            source=self.plan.source,purpose='automatic',admitted_utc=time.time(),deadline_utc=time.time()+30,
            preparation_id=replay.id,state='ready')
        self.work._save(candidate)
        self.assertEqual(len(self.work.ready()),1)
        snapshot=self.f.reviewed_snapshot()
        context=self.app.direction._context('director',self.plan.source,snapshot)
        intent={'op':'replay','replay_id':replay.id,'evidence_ids':[self.scene.evidence_ids[-1]],'reason':'Ready alone is insufficient'}
        result=LLMResult(text=json.dumps(intent),snapshot=snapshot,origin='fixture',model_id='fixture',model_version='1')
        with self.assertRaisesRegex(ValueError,'opportunity'):self.app.direction.validate(result,'director',context,time.time()+8)
        # Cooldown cannot produce an already expired Scheduled card.
        self.app.control.last_replay=time.monotonic()
        request={'id':'late-opportunity','op':'replay','args':{'replay_id':replay.id},
            'expected':self.app.control.expected({'replay_id':replay.id}),'expires_at':time.time()+2}
        admitted=self.app.control.propose(request)
        self.assertEqual(admitted['state'],'Expired',admitted)
        self.work.mark_aired(replay.id);self.assertFalse(self.work.ready())
        self.assertEqual(self.app.control.propose(request)['id'],request['id'])
        with self.assertRaises(ValueError):self.app.control.propose({**request,'expires_at':time.time()+3})

    def test_R04_R13_late_archive_registration_does_not_replace_reviewed_live_source(self):
        self.live_source()
        historical=self.plan.source.model_copy(update={'source_id':'camera/late-history','epoch':2})
        self.f.register_source(historical)
        self.assertEqual(self.f.reviewed_snapshot().sources,[self.plan.source])
        progress=self.f.diagnostics()['sources']
        self.assertEqual(len(progress),1);self.assertEqual(progress[0]['source'],self.plan.source.model_dump())

    def test_R11_partial_chunk_receipt_blocks_automatic_but_allows_explicit_recall(self):
        self.f.registry.labels['segmentor']['retained-action']['required_s']['end']=3.5
        self.app.control.program_started=True;self.work._reconcile()
        candidate=next(iter(self.work.candidates.values()));self.work.pending['automatic']=None
        with patch.object(self.app,'render') as render:self.work._prepare(candidate.candidate_id);render.assert_not_called()
        job=self.app.jobs[candidate.preparation_id]
        self.assertEqual(job['state'],'failed');self.assertIn('mapping_unknown',job['error'])
        self.assertIsNone(job['stages']['last_included_frame_receipt_utc'])
        self.assertEqual(self.f.diagnostics()['pins'],0)
        self.f.start();self.work.start()
        request={'id':'partial-query','run_id':self.app.control.run_id,'text':'yellow ball'}
        hit=self.work.search(request)['hits'][0]
        self.work.prepare_hit({'search_id':request['id'],'scene_id':hit['scene_id'],'scene_revision':hit['scene_revision']},'partial-recall')
        end=time.monotonic()+5
        while self.app.jobs['partial-recall']['state'] not in ('ready','failed') and time.monotonic()<end:time.sleep(.02)
        self.assertEqual(self.app.jobs['partial-recall']['state'],'ready',self.app.jobs['partial-recall'])
        self.assertIsNone(self.app.jobs['partial-recall']['stages']['last_included_frame_receipt_utc'])
        self.assertEqual(self.app.program.requested,'HOLDING')

    def test_R13_urgent_return_unknown_mapping_and_delayed_boundary(self):
        source=self.live_source(pts=30720)
        # Add an urgent observed action through a separate issued production analysis window.
        manifest=self.f.chunks(self.plan.source)[-1];window=self.f.window(manifest)
        # Existing window result is immutable: use a distinct bounded interval/job key.
        from foundation import operation_key
        interval=Interval(start=30720,end=61440)
        window=window.model_copy(update={'native':interval,'job_key':operation_key('urgent',self.plan.run_id),
            'chunk_ids':[manifest.chunk_id],'deadline_utc':time.time()+8,'snapshot':self.f.reviewed_snapshot()})
        self.f.enqueue(window)
        observations=asyncio.run(self.f.registry.analyze(window,[manifest]))
        observations=[o.model_copy(update={'urgent_live':True,'association_key':'urgent-action'}) for o in observations]
        self.f.ingest(window,observations,trusted_origin='fixture')
        self.app.program.actual='REPLAY';self.app.program.requested='REPLAY'
        context=self.app.direction._context('director',self.plan.source,self.f.reviewed_snapshot())
        intent={'op':'urgent_return','slot':1,'evidence_ids':[observations[0].evidence_id],'reason':'Staged urgent action'}
        result=LLMResult(text=json.dumps(intent),snapshot=context['snapshot'],origin='fixture',model_id='fixture',model_version='1')
        with self.assertRaisesRegex(ValueError,'mapping_unknown'):self.app.direction.validate(result,'director',context,time.time()+8)
        mapping=TimeMapping(source=self.plan.source,revision=1,valid=Interval(start=0,end=61440),origin_pts=0,
            offset_event_ms=0,uncertainty_ms=3.,calibration_evidence=['shared-visible-markers'])
        self.f.current_mappings={source.path:mapping}
        context=self.app.direction._context('director',self.plan.source,self.f.reviewed_snapshot())
        from foundation_records import DecisionSnapshot
        result=result.model_copy(update={'snapshot':DecisionSnapshot.model_validate(context['snapshot'])})
        _,deps=self.app.direction.validate(result,'director',context,time.time()+8)
        self.assertTrue(deps['urgent_return']);self.assertLessEqual(deps['return_not_before'],time.monotonic()+.01)
        record=self.app.direction.dispatch(result,'director',context,time.time()+8,action_id='urgent')
        self.assertEqual(record['state'],'Scheduled',record)
        self.assertLessEqual(record['not_before'],time.monotonic()+.01)

    def test_R15_R16_archive_commentary_uses_session_without_active_lease(self):
        replay=self.render();self.app.replays[replay.id]=replay
        ticket=self.work.preload(replay,'play-archive')
        self.app.program.command('replay',0,replay=replay)
        self.app.program.replay_ticket=ticket
        self.app.program.actual='REPLAY';self.app.program.actual_target={'kind':'replay','id':replay.id,
            'command_revision':self.app.program.replay_revision,'source_path':self.plan.source.source_id,'epoch':1,
            'native':replay.report['source_map'][0]['native_frames'][-1]['native'],'archive_source':self.plan.source.model_dump(),
            'output_frame':59}
        newer=self.plan.source.model_copy(update={'source_id':'camera/replaced','epoch':2})
        self.f.register_source(newer)
        context=self.app.direction._context('commentator',self.plan.source,self.f.reviewed_snapshot())
        self.assertFalse(context['facts']);self.assertFalse(context['event']['participants'])
        phrase=self.f.registry.labels['speech']['variants'][0]['text']
        result=LLMResult(text=json.dumps({'op':'commentary','text':phrase,'evidence_ids':[],'reason':'Fixture narration'}),
            snapshot=context['snapshot'],origin='fixture',model_id='fixture',model_version='1')
        _,deps=self.app.direction.validate(result,'commentator',context,time.time()+8)
        self.assertEqual(deps['sources'],{});self.assertTrue(self.app.direction.check(deps))
        self.app.program.actual_target['archive_source']['epoch']=2
        with self.assertRaisesRegex(ValueError,'source interval'):self.app.direction.check(deps)
        self.app.program.actual_target['archive_source']['epoch']=1
        self.app.program.actual_target['archive_source']['time_base']='1/90000'
        with self.assertRaisesRegex(ValueError,'source interval'):self.app.direction.check(deps)
        self.app.program.actual_target['archive_source']['time_base']=self.plan.source.time_base
        self.app.program.replay_revision+=1
        with self.assertRaisesRegex(ValueError,'session'):self.app.direction.check(deps)

    def test_R20_cancel_reaps_encoder_and_releases_source_pins(self):
        resolved=resolve_plan(self.app,self.plan,'cancel-render',time.time()+10)
        event=threading.Event();event.set()
        with self.assertRaisesRegex(ValueError,'canceled'):render_plan(self.app.cfg,'canceled',resolved,event)
        self.assertFalse((self.app.cfg.runtime/'replays/canceled.mp4').exists())
        self.assertEqual(self.f.diagnostics()['pins'],0)

    def test_R09_R20_cancel_during_resolution_cannot_start_render(self):
        import replay_work
        entered=threading.Event();release=threading.Event()
        original=replay_work.resolve_plan
        def paused(*args,**kwargs):
            result=original(*args,**kwargs);entered.set();release.wait(4);return result
        self.f.start();self.work.start()
        request=PublicSearchRequest(id='cancel-query',run_id=self.app.control.run_id,text='yellow ball')
        hit=self.work.search(request)['hits'][0]
        args={'search_id':request.id,'scene_id':hit['scene_id'],'scene_revision':hit['scene_revision']}
        try:
            with patch('replay_work.resolve_plan',side_effect=paused),patch.object(self.app,'render') as render:
                self.work.prepare_hit(args,'cancel-preparation')
                self.assertTrue(entered.wait(5))
                self.app.cancel_render('cancel-preparation');release.set()
                end=time.monotonic()+5
                while self.work.planning and time.monotonic()<end:time.sleep(.02)
                self.assertFalse(self.work.planning)
                self.assertEqual(self.app.jobs['cancel-preparation']['state'],'canceled')
                render.assert_not_called()
                self.assertEqual(self.f.diagnostics()['pins'],0)
        finally:release.set()

    def test_R02_R20_cancel_during_model_wait_keeps_terminal_state(self):
        entered=threading.Event();release=threading.Event()
        async def waiting(role,context,snapshot,deadline):
            entered.set();release.wait(4)
            return SegmentorResult(payload={'op':'wait','source':self.plan.source,
                'required':{'start':self.scene.native.start,'end':self.scene.native.end+1},
                'reason':'Wait for more aftermath'},snapshot=snapshot,origin='fixture',model_id='fixture',model_version='1')
        self.f.start();self.work.start()
        hit=self.work.search({'id':'cancel-model-query','run_id':self.app.control.run_id,'text':'yellow ball'})['hits'][0]
        args={'search_id':'cancel-model-query','scene_id':hit['scene_id'],'scene_revision':hit['scene_revision']}
        try:
            with patch.object(self.f.registry,'llm',side_effect=waiting),patch.object(self.app,'render') as render:
                self.work.prepare_hit(args,'cancel-model')
                self.assertTrue(entered.wait(5));self.app.cancel_render('cancel-model');release.set()
                end=time.monotonic()+5
                while self.work.planning and time.monotonic()<end:time.sleep(.02)
                self.assertFalse(self.work.planning)
                self.assertEqual(self.app.jobs['cancel-model']['state'],'canceled')
                candidate=next(c for c in self.work.candidates.values() if c.preparation_id=='cancel-model')
                self.assertEqual(candidate.state,'canceled');render.assert_not_called()
                self.assertEqual(self.f.diagnostics()['pins'],0)
        finally:release.set()

    def test_R17_R20_pending_search_capacity_returns_429_and_preserves_first(self):
        results=[]
        request={'id':'pending-search','run_id':self.app.control.run_id,'text':'yellow ball'}
        thread=threading.Thread(target=lambda:results.append(self.work.search(request)))
        thread.start()
        try:
            end=time.monotonic()+2
            while 'search' not in self.f.role_pending and time.monotonic()<end:time.sleep(.01)
            self.assertIn('search',self.f.role_pending)
            response=self.client.post('/api/search',json={**request,'id':'second-pending'})
            self.assertEqual(response.status_code,429,response.text)
            self.assertEqual(response.json()['reason'],'capacity_reached')
            with self.f.lock:work=self.f.role_pending.pop('search')
            work();thread.join(2)
            self.assertFalse(thread.is_alive());self.assertEqual(results[0]['id'],request['id'])
            self.assertEqual(self.work.query_count,0)
            self.assertEqual(self.work.search(request),results[0])
        finally:thread.join(6)

    def test_R02_waiting_recall_and_automatic_requeue_on_new_scene_without_new_deadline(self):
        for purpose in ('recall','automatic'):
            candidate=ReplayCandidate(candidate_id='waiting-'+purpose,scene_id=self.scene.scene_id,
                scene_revision=self.scene.revision,source=self.plan.source,purpose=purpose,
                admitted_utc=time.time(),deadline_utc=time.time()+20,preparation_id='wait-'+purpose,state='waiting')
            self.work._admit(candidate);self.work.pending[purpose]=None
        original={key:c.deadline_utc for key,c in self.work.candidates.items()}
        self.f.registry.labels['cosmos'][0]['end_s']=6.
        manifest=self.f.finalize(self.record['files'][0]['path'],self.plan.source,2,time.time(),closed=True,
            geometry=self.record['manifests'][0]['geometry'],timeline_offset_pts=61440,provenance='sample')
        window=self.f.window(manifest);self.f.enqueue(window)
        results=asyncio.run(self.f.registry.analyze(window,self.f.chunks(self.plan.source,window.native)))
        self.f.ingest(window,results,trusted_origin='fixture')
        self.assertEqual(self.work.scene(self.scene.scene_id,self.f.scene_versions[self.scene.scene_id]).native.end,92160)
        self.work._reconcile()
        for purpose in ('recall','automatic'):
            candidate=self.work.candidates['waiting-'+purpose]
            self.assertEqual(candidate.state,'queued');self.assertGreater(candidate.scene_revision,self.scene.revision)
            self.assertEqual(candidate.deadline_utc,original[candidate.candidate_id])
            self.assertEqual(self.work.pending[purpose],candidate.candidate_id)

    def test_R13_normal_scheduler_keeps_director_active_during_replay(self):
        source=self.live_source(pts=30720)
        mapping=TimeMapping(source=self.plan.source,revision=1,valid=Interval(start=0,end=61440),origin_pts=0,
            offset_event_ms=0,uncertainty_ms=3.,calibration_evidence=['fixture-visible-markers'])
        self.f.current_mappings={source.path:mapping}
        replay=self.render();self.app.replays[replay.id]=replay
        self.app.program.command('replay',self.app.program.revision,replay=replay)
        self.app.program.actual='REPLAY';self.app.program.actual_target={'kind':'replay','id':replay.id,
            'source_path':source.path,'epoch':1,'native':source.frames[-1].native_provenance,
            'command_revision':self.app.program.replay_revision}
        calls=[];original=self.f.registry.llm
        async def record(role,*args,**kwargs):
            calls.append(role);return await original(role,*args,**kwargs)
        self.f.registry.llm=record
        self.f.start();self.app.direction.start()
        end=time.monotonic()+4
        while 'director' not in calls and time.monotonic()<end:time.sleep(.02)
        self.assertIn('director',calls)
        self.assertEqual(self.app.program.actual,'REPLAY')
        self.assertTrue(self.app.control.program_started)

    def test_R01_public_canonical_review_and_prepare_use_shared_compiler(self):
        response=self.client.post('/api/replay-validate',json=self.plan.model_dump(mode='json'))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['plan']['schema_version'],'1.2')
        self.assertEqual(self.f.diagnostics()['pins'],0)
        result=self.app.control.submit({'id':'canonical-human','op':'prepare','args':{'plan':response.json()['plan']},
            'expected':self.app.control.expected({})})
        self.assertEqual(result['state'],'Preparing',result)
        self.app.render_thread.join(10)
        self.assertEqual(self.app.jobs[result['job_id']]['state'],'ready')
        self.assertFalse(self.app.control.crew_paused)

    def test_R05_rotated_original_has_explicit_padding_transform(self):
        record=retained_action(self.app,self.root/'rotated',camera=2,rotation=90)
        replay=self.render(ReplayPlan12.model_validate(record['plan']))
        geometry=replay.report['source_map'][0]['native_frames'][0]['native']['geometry']
        self.assertEqual(geometry['rotation'],90)
        self.assertEqual(geometry['pad_x'],219)
        with av.open(str(replay.path)) as media:image=next(media.decode(video=0)).to_image()
        self.assertLess(sum(image.getpixel((20,150))),10)
        self.assertGreater(sum(image.getpixel((320,150))),30)

    def test_R02_duplicate_notifications_share_one_automatic_job_and_action(self):
        self.app.control.program_started=True
        other=retained_action(self.app,self.root/'same-action-other-view',camera=2)
        self.work._reconcile();first=list(self.app.jobs)
        self.work._reconcile();self.work._reconcile()
        self.assertEqual(list(self.app.jobs),first)
        self.assertEqual(len(first),1)
        self.assertEqual(self.app.control.actions[first[0]]['actor'],'Provider crew')
        self.assertEqual(self.app.control.actions[first[0]]['state'],'Preparing')
        self.assertEqual(len(self.work.candidates),1)
        self.assertEqual(self.work._automatic_key(self.scene),self.work._automatic_key(self.work.scene(other['scene_id'],other['scene_revision'])))

    def test_segmentor_visual_context_trims_to_context_bytes(self):
        from foundation import canonical
        # Oversized fake frames must be trimmed instead of failing prepare.
        fat='A'*8000
        context={'target':{'visual_windows':[
            {'source':{'slot':1},'frames':[{'image_base64':fat} for _ in range(6)]},
            {'source':{'slot':2},'frames':[{'image_base64':fat} for _ in range(6)]},
        ]}}
        self.assertGreater(len(canonical(context).encode()),self.f.settings.limits.context_bytes)
        self.work._fit_segmentor_context(context)
        self.assertLessEqual(len(canonical(context).encode()),self.f.settings.limits.context_bytes)
        self.assertTrue(any(window['frames'] for window in context['target']['visual_windows']))
