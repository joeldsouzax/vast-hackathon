"""PRD 2 contract checks. Media behavior is checked by direction-check."""
from array import array
import asyncio
from collections import deque
from dataclasses import replace
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
from PIL import Image
from pydantic import ValidationError
from studio import App, Config
from foundation_records import (EventContext, FoundationSettings, DirectionSettings, Geometry, SourceEpoch,
    Interval, LLMResult, ProgramText, SpeechResult)
from direction import DIRECTOR, COMMENTATOR
from direction_media import validate_crop, caption_layer, Mixer, decode_speech, PreparedCue
from media import Frame,jpeg

ROOT=Path(__file__).resolve().parents[2]


class DirectionContracts(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name)
        config=json.loads((ROOT/'config/direction.fixture.json').read_text())
        config['fixture_file']=str(ROOT/'tests/fixtures/foundation/labels.json')
        self.config=self.root/'config.json';self.config.write_text(json.dumps(config))
        self.app=App(Config(self.root,'http://localhost',foundation_config=self.config))
        self.f=self.app.foundation;self.d=self.app.direction;self.c=self.app.control
        self.geometry=Geometry(native_width=640,native_height=360,output_width=640,output_height=360,scaled_width=640,scaled_height=360)
        self.native={'native_pts':150,'native_time_base':'1/15','timeline_revision':1,'geometry':self.geometry.model_dump(),'uncertainty_ms':1000/15}
        frame=Frame(151,time.monotonic(),jpeg(Image.new('RGB',(640,360),'green')),10.,150,'1/15',self.native,1,time.time())
        self.source=SimpleNamespace(path='camera/local',slot=1,epoch=1,has_audio=True,timeline_revision=1,frames=deque([frame]),
            at=lambda _:frame,audio_at=lambda _:bytes(6400),status=lambda:{'last_frame_age_s':0.,'buffer_seconds':10.,'buffer_ready':True,'has_audio':True},
            lock=threading.RLock(),close=lambda:None,origin=time.monotonic()-10.)
        self.app.sources[1]=self.source
        self.s=SourceEpoch(event_id=self.f.settings.event.event_id,run_id=self.c.run_id,source_id=self.source.path,slot=1,epoch=1,time_base='1/15')
        self.f.register_source(self.s)
        # Reviewed health has the same shape as the application-owned runtime sample.
        original=self.app.foundation_snapshot
        def snapshot():
            result=original();result['runtime']['source_health']=[{'source_id':self.source.path,'slot':1,'epoch':1,'state':'ACTIVE',
                'last_frame_age_s':0.0,'buffer_seconds':10.0,'buffer_ready':True,'has_audio':True}]
            return result
        self.f.snapshot_reader=snapshot
        self.app.program.command('live',0,1)
        self.c.program_started=True  # This fixture represents an operator-started program.
        self.app.program.actual='LIVE';self.app.program.actual_target={'kind':'camera','source_path':self.source.path,'epoch':1,'native':self.native}
        self.app.program.applied_revision=self.app.program.revision
        self.phrase=self.f.registry.labels['speech']['text']
        self.f.registry.labels['speech']['file']=str(ROOT/'tests/fixtures/foundation/speech.wav')

    def tearDown(self):
        self.app.close();self.folder.cleanup()

    def context(self,role='director'):
        snapshot=self.f.reviewed_snapshot()
        return self.f.context(role,self.s,Interval(start=0,end=151),snapshot)

    def result(self,intent,context):
        return LLMResult(text=json.dumps(intent),snapshot=context['snapshot'],origin='fixture',model_id='fixture',model_version='fixture-1')

    def test_D01_setup_preload_preview_unknown_and_unicode(self):
        event=self.f.event_context().model_dump();event.update(revision=2,title='東京 — Montréal '+('Long name '*20),participants=['Zoë'],branding={'logo':'missing.png'})
        revision=self.app.program.revision
        output=self.app.setup_event({'context':event,'expected_revision':1,'operation_key':'setup'})
        self.assertTrue(output['package']['ready']);self.assertTrue(output['package']['preloaded'])
        self.assertEqual(self.app.program.revision,revision)
        self.assertEqual(self.app.program.graphics.score['home_score'],None)
        self.assertEqual(len(output['package']['assets']),25)
        for asset in output['package']['assets']:self.assertEqual(len(asset['sha256']),64)
        self.assertTrue(self.d.status()['setup']['ready'])

    def test_D02_setup_conflict_failure_and_corrected_context(self):
        event=self.f.event_context().model_dump();event.update(revision=2,title='First')
        self.app.setup_event({'context':event,'expected_revision':1,'operation_key':'first'})
        with self.assertRaisesRegex(ValueError,'revision changed'):
            self.app.setup_event({'context':{**event,'title':'Other'},'expected_revision':1,'operation_key':'other'})
        self.app.update_event_context({'context':{**event,'revision':3,'title':'Corrected'},'expected_revision':2,'operation_key':'correct'})
        with patch.object(self.app.program.graphics,'prepare_package',side_effect=ValueError('Font missing')):
            with self.assertRaises(ValueError):self.app.setup_event({'context':{**event,'revision':4},'expected_revision':3,'operation_key':'bad'})
        self.assertEqual(self.f.event_context().title,'Corrected');self.assertFalse(self.d.status()['setup']['ready'])
        with self.assertRaisesRegex(ValueError,'stopped Studio'):
            self.app.setup_event({'context':{**event,'revision':4,'broadcast_delay_s':8.0},'expected_revision':3,'operation_key':'delay'})

    def test_D03_abstention_and_no_change_preserve_revision(self):
        context=self.context();revision=self.app.program.revision
        for intent in ({'op':'abstain','reason':'Keep view'},{'op':'live','slot':1,'reason':'Same camera'}):
            self.assertIsNone(self.d.dispatch(self.result(intent,context),'director',context,time.time()+5))
        self.assertEqual(self.app.program.revision,revision);self.assertFalse(self.c.actions)

    def test_D04_crop_boundaries_geometry_aspect_and_reset(self):
        valid={'x':.25,'y':.25,'width':.5,'height':.5}
        self.app.program.command('crop',1,rect=valid)
        self.assertIsNotNone(self.app.program.framing)
        for rect in ({**valid,'x':float('nan')},{**valid,'x':.8},{**valid,'width':0.},{**valid,'width':.4},{**valid,'height':.7}):
            with self.subTest(rect=rect),self.assertRaises(ValueError):validate_crop(rect,self.geometry)
        self.app.program.command('reset_crop',2);self.assertIsNone(self.app.program.framing)
        portrait=Geometry(native_width=360,native_height=640,output_width=640,output_height=360,scaled_width=202,scaled_height=360,pad_x=218)
        with self.assertRaises(ValueError):validate_crop(valid,portrait)
        context=self.context();context['observations']=[{'evidence_id':'weak','detections':[{'confidence':.3}]}]
        intent={'op':'crop','rect':valid,'evidence_ids':['weak'],'reason':'Weak fixture subject'}
        with self.assertRaisesRegex(ValueError,'subject evidence'):
            self.d.validate(self.result(intent,context),'director',context,time.time()+5)

    def test_D05_snapshot_changes_before_scheduling_and_commit(self):
        for change in ('context','control','program','lease','epoch','mapping'):
            context=self.context();intent={'op':'audio','slot':1,'muted':True,'reason':'Configured mute'}
            _,deps=self.d.validate(self.result(intent,context),'director',context,time.time()+5)
            if change=='context':self.f._context_revision+=1
            elif change=='control':self.c.revision+=1
            elif change=='program':self.app.program.revision+=1
            elif change=='lease':self.source.path+='-replacement'
            elif change=='epoch':self.source.epoch+=1
            else:self.source.timeline_revision+=1
            with self.assertRaises(ValueError):self.d.check(deps)
            self.f._context_revision=1
            self.source.path='camera/local';self.source.epoch=1;self.source.timeline_revision=1

    def test_D06_forgery_retries_and_authority_allowlist(self):
        for intent in ({'op':'policy','minimum_shot_s':0},{'op':'takeover'},{'op':'shutdown'},
            {'op':'graphics','preset':'opening','reason':'Test','actor':'Human'},
            {'op':'live','slot':1,'reason':'Test','snapshot':{}}, {'op':'audio','slot':6,'reason':'Bad'}):
            with self.subTest(intent=intent),self.assertRaises(ValueError):DIRECTOR.validate_python(intent)
        context=self.context();intent={'op':'audio','slot':1,'muted':True,'reason':'Configured mute'}
        result=self.result(intent,context)
        first=self.d.dispatch(result,'director',context,time.time()+5,action_id='retry')
        # Retry must retain the exact original deadline and dependencies.
        request={key:first[key] for key in ('id','op','args','expected','expires_at')}
        self.assertEqual(self.c.propose(request,actor='Provider crew')['id'],'retry')
        with self.assertRaisesRegex(ValueError,'changed contents'):self.c.propose({**request,'args':{'slot':1,'muted':False}},actor='Provider crew')
        for op in ('policy','takeover','resume','rehearsal'):
            request={'id':op,'op':op,'args':{},'expected':self.c.expected({}),'expires_at':time.time()+5}
            self.assertEqual(self.c.propose(request,actor='Provider crew')['state'],'Rejected')

    def test_D08_commentary_references_future_and_duplicate_history(self):
        context=self.context('commentator')
        bad={'op':'commentary','text':'A named player won.','evidence_ids':['not-reviewed'],'reason':'Unsupported'}
        with self.assertRaisesRegex(ValueError,'outside reviewed'):self.d.validate(self.result(bad,context),'commentator',context,time.time()+5)
        bad['evidence_ids']=[]
        with self.assertRaisesRegex(ValueError,'requires reviewed'):self.d.validate(self.result(bad,context),'commentator',context,time.time()+5)
        disclosure={'op':'commentary','text':self.phrase,'reason':'Fixture disclosure'}
        context['pending']=[{'text':self.phrase}]
        with self.assertRaisesRegex(ValueError,'repeats'):self.d.validate(self.result(disclosure,context),'commentator',context,time.time()+5)

    def test_event_talk_requires_named_context_and_has_no_action_citations(self):
        context=self.context('commentator')
        line={'op':'commentary','basis':'event_context','text':'Welcome to Forever 22.','evidence_ids':[], 'reason':'Event welcome'}
        capabilities=self.f.registry.capabilities()
        with patch.object(self.f.registry,'gemini',object()), patch.object(self.f.registry,'capabilities',return_value=capabilities):
            with self.assertRaisesRegex(ValueError,'current event brief'):
                self.d.validate(self.result(line,context),'commentator',context,time.time()+5)
            context['event']['title']='Forever 22'
            intent,dependencies=self.d.validate(self.result(line,context),'commentator',context,time.time()+5)
            self.assertEqual(intent.basis,'event_context')
            self.assertEqual(dependencies['evidence_ids'],[])
            self.assertEqual(set(dependencies['sources']),{self.s.source_id})
            line['basis']='action'
            with self.assertRaisesRegex(ValueError,'requires reviewed'):
                self.d.validate(self.result(line,context),'commentator',context,time.time()+5)

    def test_gemini_replay_needs_operator_approval(self):
        with patch.object(self.f.registry,'gemini',object()):
            result=self.c.propose({'id':'automatic-replay','op':'replay','args':{'replay_id':'example'},
                'expected':self.c.expected({'replay_id':'example'}),'expires_at':time.time()+5},actor='Provider crew')
        self.assertEqual(result['state'],'Rejected')
        self.assertIn('operator approval',result['reason'])

    def audio_context(self, speech='foreground', meaning=''):
        from foundation_records import Observation, AudioEvidence
        self.app.program.audio_source_path=self.s.source_id
        self.app.program.audio_epoch=self.s.epoch
        observation=Observation(evidence_id='heard-words' if speech=='foreground' else 'heard-background',job_key='heard-job',source=self.s,native=Interval(start=100,end=140),
            chunk_ids=['heard-chunk'],snapshot=self.f.reviewed_snapshot(),configuration_revision=1,model_id='fixture',model_version='fixture-1',
            origin='fixture',description='Heard words',kind='observed',uncertainty=.5,produced_utc=time.time(),
            audio=AudioEvidence(speech=speech,transcript='We built a voice demo.',meaning=meaning))
        with self.f.transaction():
            self.f._put('observation',observation.evidence_id,1,observation)
            self.f.db.execute('INSERT INTO evidence_versions SELECT ?,COALESCE(max(ordinal),0)+1 FROM evidence_versions',(observation.evidence_id,))
        context=self.context('commentator')
        context['observations']=[observation.model_dump(mode='json')]
        context['target']['microphone']={'slot':1,'source_path':self.s.source_id,'epoch':self.s.epoch,'muted':False}
        return context

    def test_speech_graphics_bind_meaning_and_microphone(self):
        context=self.audio_context(meaning='They built a voice demo.')
        context['prepared_graphics']=[{'id':'headline','slot':'banner','text_binding':'evidence'}]
        line={'op':'graphics','preset':'headline','title':'They built a voice demo.','subtitle':'Heard meaning',
            'evidence_ids':['heard-words'],'reason':'Explain the spoken point'}
        caps=self.f.registry.capabilities()
        with patch.object(self.f.registry,'gemini',object()), patch.object(self.f.registry,'capabilities',return_value=caps):
            for change in ({'subtitle':'They won.'},{'title':'Confirmed result'},{'evidence_ids':[]}):
                with self.subTest(change=change),self.assertRaises(ValueError):
                    self.d.validate(self.result({**line,**change},context),'director',context,time.time()+6)
            # Use a real frame-receipt job deadline for the live evidence guard.
            original_execute=self.f.db.execute
            class JobDB:
                def execute(inner,sql,args=()):
                    if sql.startswith('SELECT deadline,body FROM jobs'):
                        return SimpleNamespace(fetchone=lambda:{'deadline':time.time()+6,'body':'{"deadline_basis":"frame-receipt"}'})
                    return original_execute(sql,args)
            with patch.object(self.f,'db',JobDB()):
                _,deps=self.d.validate(self.result(line,context),'director',context,time.time()+6)
            self.app.program.audio_muted=True
            with self.assertRaisesRegex(ValueError,'microphone changed'):self.d.check(deps)

    def test_scene_transition_needs_citation_duration_and_spacing(self):
        context=self.audio_context(meaning='They built a voice demo.')
        context['prepared_graphics']=[{'id':'iris-reveal','slot':'stinger','text_binding':'prepared'}]
        line={'op':'graphics','preset':'iris-reveal','duration_s':.8,'evidence_ids':['heard-words'],'reason':'New topic'}
        caps=self.f.registry.capabilities()
        with patch.object(self.f.registry,'gemini',object()), patch.object(self.f.registry,'capabilities',return_value=caps):
            for change in ({'duration_s':4.0},{'evidence_ids':[]}):
                with self.subTest(change=change),self.assertRaises(ValueError):
                    self.d.validate(self.result({**line,**change},context),'director',context,time.time()+6)
            self.assertIsNone(self.d._transition_unavailable())
            self.c.actions['transition']={'op':'graphics','args':{'graphics':{'preset':'iris-reveal'}},
                'state':'Finished','created_at':time.time()}
            with self.assertRaisesRegex(ValueError,'spacing'):
                self.d.validate(self.result(line,context),'director',context,time.time()+6)
            self.c.actions.clear()

    def test_showcase_covers_templates_during_speech_without_model_work(self):
        event=self.f.event_context().model_copy(update={'title':'Known event',
            'editorial_policy':{'graphics_mode':'showcase'},'branding':{'organizer':'Known organizer','venue':'Known venue'}})
        seen=[];now=time.time()
        self.app.program.cue=self.cue()
        def capture(preset,title,subtitle,duration):
            seen.append(preset)
            row={'op':'graphics','state':'Finished','created_at':time.time(),'cue_ids':['applied'],
                'args':{'graphics':{'preset':preset,'title':title,'subtitle':subtitle}}}
            self.c.actions[str(len(seen))]=row
            return row
        with patch.object(self.f,'event_context',return_value=event),patch.object(self.d,'_graphic',side_effect=capture):
            for i in range(20):
                with patch('direction.time.time',return_value=now+i*8.1):self.d._showcase_graphics()
        self.assertEqual(len(set(seen)),15)
        self.assertTrue(set(self.d.STINGERS).issubset(seen))
        self.assertNotIn('score-wide',seen);self.assertNotIn('closing',seen)
        self.assertIsNotNone(self.app.program.cue)
        self.c.actions.clear();self.app.program.cue=None

    def test_showcase_respects_hold_takeover_package_and_spacing(self):
        event=self.f.event_context().model_copy(update={'title':'Known event','editorial_policy':{'graphics_mode':'showcase'}})
        with patch.object(self.f,'event_context',return_value=event),patch.object(self.d,'_graphic') as graphic:
            self.c.crew_paused=True;self.d._showcase_graphics();graphic.assert_not_called()
            self.c.crew_paused=False;self.app.program.requested='HOLDING';self.d._showcase_graphics();graphic.assert_not_called()
            self.app.program.requested='LIVE'
            self.c.actions['recent']={'op':'graphics','state':'Finished','created_at':time.time(),'cue_ids':['applied']}
            self.d._showcase_graphics();graphic.assert_not_called();self.c.actions.clear()
            with patch.object(self.f,'event_context',return_value=event.model_copy(update={'revision':99})):
                self.d._showcase_graphics();graphic.assert_not_called()

    def test_source_quote_requires_exact_words_and_selected_microphone(self):
        context=self.audio_context()
        line={'op':'commentary','delivery':'source_caption','text':'We built a voice demo.',
            'evidence_ids':['heard-words'],'reason':'Let the speaker explain'}
        self.d.validate(self.result(line,context),'commentator',context,time.time()+6)
        for change in ({'text':'We won the hackathon.'},{'evidence_ids':[]},{'text':' '},{'basis':'event_context'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.d.validate(self.result({**line,**change},context),'commentator',context,time.time()+6)
        for change in ({'muted':True},{'source_path':'camera/other'},{'epoch':2}):
            changed={**context,'target':{**context['target'],'microphone':{**context['target']['microphone'],**change}}}
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'selected microphone'):
                self.d.validate(self.result(line,changed),'commentator',changed,time.time()+6)

    def test_microphone_speech_does_not_veto_narration(self):
        for speech in ('foreground','background'):
            context=self.audio_context(speech)
            line={'op':'commentary','text':self.phrase,'reason':'Fixture commentary'}
            self.d.validate(self.result(line,context),'commentator',context,time.time()+6)

    def test_source_quote_has_no_tts_and_mic_changes_cancel_it(self):
        context=self.audio_context()
        line={'op':'commentary','delivery':'source_caption','text':'We built a voice demo.',
            'evidence_ids':['heard-words'],'reason':'Show heard words'}
        with patch.object(self.d,'_speech',side_effect=AssertionError('Source quote must not call TTS')):
            self.d.dispatch(self.result(line,context),'commentator',context,time.time()+6)
            self.f.role_pending.pop('speech')()
        cue=next(iter(self.d.prepared.values()))
        self.assertFalse(cue.pcm);self.assertIsNotNone(cue.caption)
        self.assertEqual(cue.text,'Heard: We built a voice demo.')
        self.assertLessEqual(cue.end_frame-cue.start_frame,4*self.app.cfg.fps)
        self.assertTrue(cue.valid())
        self.app.program.audio_muted=True
        self.assertFalse(cue.valid())

    def test_D07_unknown_ambiguous_and_discontinuous_mapping(self):
        from live_timing import match_recording
        from PIL import ImageDraw
        import av
        path=self.root/'markers.mp4';images=[]
        with av.open(str(path),'w') as container:
            stream=container.add_stream('libx264',rate=15);stream.width=640;stream.height=360;stream.pix_fmt='yuv420p'
            for index in range(18):
                image=Image.new('RGB',(640,360),(25,45,65));draw=ImageDraw.Draw(image)
                draw.rectangle((index*25,90,index*25+100,250),fill=(180,220,180));images.append(image)
                frame=av.VideoFrame.from_image(image);frame.pts=index;frame.time_base=Fraction(1,15)
                for packet in stream.encode(frame):container.mux(packet)
            for packet in stream.encode():container.mux(packet)
        with av.open(str(path)) as media:
            decoded=list(media.decode(video=0))
        # Receipt values here exercise the contract only. The media suite measures real receipt.
        self.source.frames=deque(Frame(index+1,time.monotonic(),jpeg(frame.to_image()),float(frame.pts*frame.time_base),frame.pts,str(frame.time_base),
            {**self.native,'native_pts':frame.pts,'native_time_base':str(frame.time_base)},1,time.time()) for index,frame in enumerate(decoded))
        result=match_recording(path,self.source,15)
        self.assertEqual(result['timeline_offset_pts'],0);self.assertLessEqual(result['residual_ms'],1000/15)
        with self.assertRaisesRegex(ValueError,'No matching'):match_recording(path,None,15)
        old=self.source.frames[-1]
        self.source.frames[-1]=replace(old,native_provenance={**old.native_provenance,'timeline_revision':2})
        with self.assertRaisesRegex(ValueError,'discontinuity'):match_recording(path,self.source,15)
        self.source.frames[-1]=replace(old,received_utc=None)
        with self.assertRaisesRegex(ValueError,'receipt UTC'):match_recording(path,self.source,15)
        static=jpeg(Image.new('RGB',(640,360),'green'))
        self.source.frames=deque(replace(frame,data=static) for frame in self.source.frames)
        with self.assertRaisesRegex(ValueError,'weak or ambiguous'):match_recording(path,self.source,15)

    def test_D07_small_clock_reset_and_time_base_change_renew_native_revision(self):
        from media_provenance import DecoderTrace
        trace=DecoderTrace(15,640,360)
        trace.accept('[showinfo@native] config in time_base: 1/90000, frame_rate: 15/1')
        trace.accept('[showinfo@native] n: 0 pts: 90000 pts_time:1 fmt:yuv420p s:640x360')
        trace.accept('[showinfo@native] n: 1 pts: 84000 pts_time:0.933333 fmt:yuv420p s:640x360')
        self.assertEqual(trace.revision,2);self.assertTrue(trace.source_discontinuity)
        trace.accept('[showinfo@native] config in time_base: 1/15360, frame_rate: 15/1')
        self.assertEqual(trace.revision,3);self.assertTrue(trace.source_discontinuity)

    def test_D07_waits_for_delayed_trace_without_inventing_missing_mapping(self):
        from media_provenance import DecoderTrace
        trace=DecoderTrace(15,640,360)
        result=[]
        worker=threading.Thread(target=lambda:result.append(trace.take(1,timeout=.2)))
        worker.start();time.sleep(.02)
        trace.accept('[showinfo@native] config in time_base: 1/15')
        trace.accept('[showinfo@native] n: 0 pts: 0 pts_time:0 fmt:yuv420p s:640x360')
        trace.accept('[showinfo@scaled] n: 0 pts: 0 pts_time:0 fmt:yuv420p s:640x360')
        trace.accept('[showinfo@proxy] n: 0 pts: 0 pts_time:0')
        worker.join(.3)
        self.assertFalse(worker.is_alive());self.assertEqual(result[0]['native_pts'],0)
        began=time.monotonic();self.assertIsNone(trace.take(2,timeout=.02))
        self.assertLess(time.monotonic()-began,.2)

    def test_D07_receipt_uncertainty_shortens_original_deadline(self):
        from foundation_storage import inspect_video
        path=ROOT/'tests/fixtures/demo.mp4';info=inspect_video(path)
        source=SourceEpoch(event_id=self.s.event_id,run_id=self.s.run_id,source_id='camera/receipt-fixture',epoch=1,slot=2,time_base=info['time_base'])
        receipt=time.time()
        manifest=self.f.finalize(path,source,0,receipt,closed=True,receipt_uncertainty_ms=75.0)
        window=self.f.window(manifest)
        self.assertEqual(window.deadline_basis,'frame-receipt')
        self.assertAlmostEqual(window.deadline_utc,receipt+self.f.settings.limits.live_deadline_s-.075,places=5)
        # A retry keeps the first immutable receipt and cannot extend its deadline.
        retried=self.f.finalize(path,source,0,time.time()+10,closed=True)
        self.assertEqual(retried.last_receipt_utc,receipt)
        self.assertEqual(retried.receipt_uncertainty_ms,75.0)

    def cue(self,pcm=b''):
        context=self.context('commentator');snapshot=self.f.reviewed_snapshot()
        deps={'snapshot':snapshot,'evidence_ids':['evidence-test'],'deadline':time.time()+6,
            'sources':{self.source.path:(1,1,1)}}
        return PreparedCue('cue',f'{self.c.run_id}:1',self.phrase,pcm,caption_layer(self.app.program.graphics,self.phrase),
            0,80,time.time()+6,snapshot.program_revision,{'kind':'camera','source_path':self.source.path,'epoch':1},lambda:self.d._guard(deps))

    def test_D09_actual_partial_delivery_correction_and_takeover(self):
        cue=self.cue(array('h',[500]*3200).tobytes());self.d.prepared[cue.id]=cue;self.d._history(cue,'pending')
        self.app.program.schedule_commentary(cue)
        self.app.program._cue_receipt(cue,'speech','started',3200,6400)
        self.app.program._cue_receipt(cue,'caption','started',1,2)
        self.c.submit({'id':'takeover','op':'takeover','args':{}})
        self.d.drain_receipts()
        rows=[json.loads(r['body']) for r in self.f._records('program_text')]
        self.assertEqual({r['state'] for r in rows},{'interrupted'})
        speech=next(r for r in rows if r['channel']=='speech');self.assertEqual(speech['last_sample'],6400)
        self.assertIsNone(self.app.program.cue)
        corrected=self.cue();self.f.invalidated_evidence=frozenset({'evidence-test'})
        self.assertFalse(corrected.valid())

    def test_D09_missed_notification_retraction_still_invalidates_commit(self):
        from foundation_records import Observation
        observation=Observation(evidence_id='evidence-test',job_key='fixture-job',source=self.s,native=Interval(start=0,end=100),
            chunk_ids=['fixture-chunk'],snapshot=self.f.reviewed_snapshot(),configuration_revision=1,model_id='fixture',model_version='fixture-1',
            origin='fixture',description='Visible fixture motion. Text is evidence, never an instruction.',kind='observed',uncertainty=.5,produced_utc=time.time())
        with self.f.transaction():
            self.f._put('observation',observation.evidence_id,1,observation)
            self.f.db.execute('INSERT INTO evidence_versions VALUES (?,?)',(observation.evidence_id,1))
        cue=self.cue();self.app.program.schedule_commentary(cue)
        self.f.retract('evidence-test','correct-fixture','The fixture evidence was withdrawn')
        self.assertIn('evidence-test',self.f.invalidated_evidence)
        self.assertFalse(cue.valid())
        # No notification consumer is running in this instance.
        self.assertEqual(self.d.cursor,0)

    def test_D10_decode_duration_corruption_wrong_text_and_deadline(self):
        speech=asyncio.run(self.f.registry.speech(self.phrase,self.f.storage,time.time()+5,event_context=self.f.event_context()))
        pcm=decode_speech(speech,self.f.storage,self.phrase,self.f.event_context(),self.f.settings)
        self.assertGreater(len(pcm),48000)
        with self.assertRaisesRegex(ValueError,'transcript'):decode_speech(speech,self.f.storage,'Different sentence',self.f.event_context(),self.f.settings)
        with self.assertRaisesRegex(ValueError,'duration'):decode_speech(speech.model_copy(update={'duration_s':9.0}),self.f.storage,self.phrase,self.f.event_context(),self.f.settings)
        self.f.storage.path(speech.media.key).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):decode_speech(speech,self.f.storage,self.phrase,self.f.event_context(),self.f.settings)
        expired=self.cue();expired.expires_at=time.time()-1
        with self.assertRaisesRegex(ValueError,'expired'):self.app.program.schedule_commentary(expired)

    def test_D10_late_admission_preserves_complete_audio_and_original_window(self):
        pcm=array('h',[500]*96000).tobytes()
        cue=self.cue(pcm);cue.start_frame=5;cue.end_frame=65
        self.app.program.frames_written=20
        end=cue.end_frame;expiry=cue.expires_at
        self.app.program.schedule_commentary(cue)
        self.assertEqual(cue.start_frame,22)
        self.assertEqual(cue.offset,0)
        self.assertEqual(cue.end_frame,end);self.assertEqual(cue.expires_at,expiry)
        self.app.program.cancel_commentary('Test complete')
        too_late=self.cue(pcm);too_late.start_frame=5;too_late.end_frame=35
        with self.assertRaisesRegex(ValueError,'original window'):self.app.program.schedule_commentary(too_late)
        self.assertIsNone(self.app.program.cue)
        self.assertNotIn('first',too_late.delivered.get('speech',{}))

    def test_D08_duplicate_is_reserved_before_speech_and_old_context_cannot_repeat(self):
        context=self.context('commentator')
        intent={'op':'commentary','text':self.phrase,'reason':'Reservation test'}
        result=self.result(intent,context)
        self.d.dispatch(result,'commentator',context,time.time()+8)
        pending=self.context('commentator')['pending']
        self.assertEqual([(r['text'],r['channel']) for r in pending],[(self.phrase,'intent')])
        with self.assertRaisesRegex(ValueError,'repeats'):self.d.dispatch(result,'commentator',context,time.time()+8)
        self.f.role_pending.pop('speech')()
        first=next(iter(self.d.prepared.values()))
        self.app.program.schedule_commentary(first)
        with self.assertRaisesRegex(ValueError,'repeats'):self.d.dispatch(result,'commentator',context,time.time()+8)
        self.assertEqual(len(self.d.prepared),1)

    def test_D08_replaced_and_failed_preparation_release_reservation(self):
        context=self.context('commentator')
        def dispatch(text):
            self.d.dispatch(self.result({'op':'commentary','text':text,'reason':'Replacement test'},context),'commentator',context,time.time()+8)
        dispatch(self.phrase)
        other=self.f.registry.labels['speech']['variants'][0]['text']
        dispatch(other)
        self.assertEqual([r['text'] for r in self.context('commentator')['pending']],[other])
        self.c._invalidate('Preparation invalidated')
        self.f.role_pending.pop('speech')()
        self.assertFalse(self.context('commentator')['pending'])

    def test_D03_holding_schedules_director_only_after_human_start(self):
        self.app.program.command('holding',self.app.program.revision)
        self.app.program.actual='HOLDING';self.app.program.actual_target={'kind':'holding'}
        self.c.program_started=False
        submitted=[]
        with patch.object(self.f,'submit_role',side_effect=lambda role,work:submitted.append(role)):
            self.d.start();time.sleep(.2)
            self.assertFalse(submitted)
            self.c.program_started=True
            deadline=time.monotonic()+2
            while not submitted and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(submitted,['director'])

    def test_D03_provider_cannot_start_program_and_human_start_enables_recovery(self):
        self.app.program.command('holding',self.app.program.revision)
        self.c.program_started=False
        request={'id':'startup','op':'live','args':{'slot':1},'expected':self.c.expected({}), 'expires_at':time.time()+8}
        record=self.c.propose(request,actor='Provider crew')
        with self.assertRaisesRegex(ValueError,'operator must start'):self.c._media(self.c.actions[record['id']])
        result=self.c.submit({'id':'human-start','op':'live','args':{'slot':1}})
        self.assertNotEqual(result['state'],'Rejected');self.assertTrue(self.c.program_started)

    def test_D11_mixer_duck_headroom_ramps_restore_and_bounds(self):
        config=DirectionSettings(ramp_ms=10)
        mixer=Mixer(config);ambient=array('h',[10000]*4800).tobytes()
        normal=array('h',mixer.mix(ambient))
        duck=array('h',mixer.mix(ambient,array('h',[0]*4800).tobytes(),4800,48000))
        restored=array('h',mixer.mix(ambient))
        self.assertEqual(normal[-1],7000);self.assertLess(duck[-1],2000);self.assertEqual(restored[-1],7000)
        loud=array('h',mixer.mix(array('h',[32767]*4800).tobytes(),array('h',[32767]*4800).tobytes(),4800,48000))
        self.assertLess(max(loud),32767)
        for value in (float('nan'),-1.,1.1):
            with self.assertRaises(ValueError):DirectionSettings(ambient_gain=value)

    def test_D12_session_change_and_replay_return_cancel(self):
        cue=self.cue();self.app.program.schedule_commentary(cue)
        self.app.program.command('holding',1)
        self.assertIsNone(self.app.program.cue);self.assertTrue(cue.canceled)
        cue=self.cue();self.app.program.schedule_commentary(cue)
        self.app.program.command('live',2,1)
        self.assertIsNone(self.app.program.cue)

    def test_D13_caption_two_lines_fit_safe_margin_and_contrast(self):
        layer=caption_layer(self.app.program.graphics,self.phrase)
        bounds=layer.getbbox();self.assertGreaterEqual(bounds[0],32);self.assertLessEqual(bounds[2],609)
        self.assertGreaterEqual(bounds[1],18);self.assertLessEqual(bounds[3],343)
        for text in ('Very long words '*100,'X'*240,'bad\ncaption',''):
            with self.assertRaises(ValueError):caption_layer(self.app.program.graphics,text)

    def test_D15_role_queue_storage_and_history_bounds_urgent_controls(self):
        for i in range(100):self.f.submit_role('director',lambda:None)
        self.assertEqual(len(self.f.role_pending),1)
        self.f.settings.direction=self.f.settings.direction.model_copy(update={'action_records':32})
        for i in range(32):self.c.submit({'id':f'h-{i}','op':'takeover','args':{}})
        with self.assertRaisesRegex(ValueError,'capacity'):self.c.propose({'id':'extra','op':'holding','args':{}})
        self.assertEqual(self.c.submit({'id':'urgent','op':'takeover','args':{}})['state'],'Finished')

    def test_D17_old_run_and_terminal_history_are_immutable(self):
        context=self.context();result=self.result({'op':'audio','slot':1,'reason':'Test'},context)
        self.c.run_id='new-run'
        with self.assertRaises(ValueError):self.d.dispatch(result,'director',context,time.time()+5)
        text=ProgramText(cue_id='old',event_id=self.s.event_id,run_id=self.f.run_id,text='Partial',state='pending',event_ms=None,program_revision=1,origin='controller')
        self.f.program_text(text,owner='controller');self.f.program_text(text.model_copy(update={'state':'started'}),owner='controller')
        self.f.program_text(text.model_copy(update={'state':'interrupted'}),owner='controller')
        with self.assertRaises(ValueError):self.f.program_text(text.model_copy(update={'state':'completed'}),owner='controller')

    def test_D18_missing_access_invalid_schema_and_no_fixture_in_live_mode(self):
        from foundation_providers import Registry,CapabilityError
        live=FoundationSettings.load(ROOT/'config/direction.live.json');registry=Registry(live)
        self.assertFalse(registry.capabilities()['llm']['ready'])
        with self.assertRaises(CapabilityError):asyncio.run(registry.llm('director',{},self.f.reviewed_snapshot(),time.time()+5))
        for text in ('not json','{"op":"abstain","reason":"Test","deadline":99}','{"op":"commentary","text":"Test","reason":"Test","voice_id":"fake"}'):
            with self.assertRaises(ValueError):COMMENTATOR.validate_json(text)


if __name__=='__main__':unittest.main()
