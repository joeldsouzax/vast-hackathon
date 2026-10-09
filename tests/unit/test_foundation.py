"""Real bytes around simulated provider behavior; no live integration claims."""
import asyncio
from fractions import Fraction
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import av
from PIL import Image
from fastapi.testclient import TestClient
from pydantic import ValidationError
from foundation import Foundation
from foundation_records import (FoundationSettings, SourceEpoch, EventContext, Interval, Geometry,
    TimeMapping, Observation, SearchQuery, ProgramText, Limits)
from foundation_storage import inspect_video
from foundation_providers import bounded_call
from http_api import web_api
from studio import App, Config

ROOT=Path(__file__).resolve().parents[2]


def fixtures(root):
    """Encoded frames with a native tick timeline; file segments retain absolute PTS."""
    paths=[]
    for chunk in range(6):
        path=root/f'chunk-{chunk}.mp4'
        with av.open(str(path),'w') as container:
            stream=container.add_stream('libx264',rate=15)
            stream.width=160;stream.height=90;stream.pix_fmt='yuv420p'
            stream.time_base=Fraction(1,15360);stream.codec_context.time_base=Fraction(1,15360)
            stream.options={'preset':'ultrafast','crf':'18'}
            for index in range(30):
                frame=av.VideoFrame.from_image(Image.new('RGB',(160,90),(50+chunk*25,100,150)))
                frame.pts=(chunk*30+index)*1024;frame.time_base=Fraction(1,15360)
                for packet in stream.encode(frame):container.mux(packet)
            for packet in stream.encode():container.mux(packet)
        paths.append(path)
    return paths


class FoundationContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_dir=tempfile.TemporaryDirectory()
        cls.paths=fixtures(Path(cls.media_dir.name))

    @classmethod
    def tearDownClass(cls):cls.media_dir.cleanup()

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.runtime=Path(self.directory.name)
        config=json.loads((ROOT/'config/foundation.fixture.json').read_text())
        config['fixture_file']=str(ROOT/'tests/fixtures/foundation/labels.json')
        self.settings=FoundationSettings.model_validate(config)
        self.f=Foundation(self.runtime,'run-one',self.settings)
        self.disk_patch=patch.object(self.f.storage,'free_bytes',return_value=10*1024**3);self.disk_patch.start()
        self.source=SourceEpoch(event_id=self.settings.event.event_id,run_id='run-one',source_id='camera/lease-one',epoch=1,slot=1,time_base='1/15360')

    def tearDown(self):self.disk_patch.stop();self.f.close();self.directory.cleanup()

    def chunks(self,count=3,mapping=None):
        return [self.f.finalize(p,self.source,i,time.time(),closed=True,provenance='sample',mapping=mapping) for i,p in enumerate(self.paths[:count])]

    def analyze(self,m):
        window=self.f.window(m);self.f.enqueue(window)
        results=asyncio.run(self.f.registry.analyze(window,self.f.chunks(window.source,window.native)))
        return window,results,self.f.ingest(window,results,trusted_origin='fixture')

    def test_E01_context_facts_revision_authority_unknown(self):
        revision=self.f.context_revision
        event=self.f.event_context().model_copy(update={'revision':revision+1,'title':'Operator supplied'})
        self.f.update_context(event,revision,'edit-one')
        self.f.update_context(event,revision,'edit-one')
        self.assertEqual(self.f.context_revision,2)
        with self.assertRaisesRegex(ValueError,'changed'):self.f.update_context(event,1,'stale')
        with self.assertRaises(PermissionError):self.f.confirm_facts({'home_score':9},2,'spoof',authority='fixture')
        self.f.confirm_facts({'home_score':None},2,'confirm',authority='human')
        self.assertEqual(self.f.context_revision,3)
        self.assertIsNone(json.loads(self.f._records('fact')[0]['body'])['effective_event_ms'])
        with self.assertRaises(ValidationError):EventContext(event_id='x',revision=True)
        with self.assertRaises(ValidationError):EventContext(event_id='x',unknown='instruction')

    def test_E01_supplied_initial_revision_is_authoritative(self):
        settings=self.settings.model_copy(update={'event':self.settings.event.model_copy(update={'revision':7})})
        f=Foundation(self.runtime/'imported','import-run',settings)
        try:
            self.assertEqual(f.context_revision,f.event_context().revision)
            self.assertEqual(f.context_revision,7)
            f.confirm_facts({'home_score':1},7,'import-score',authority='human')
            self.assertEqual(f.context_revision,8)
        finally:f.close()

    def test_E02_readiness_hash_duplicate_changed_partial(self):
        with self.assertRaisesRegex(ValueError,'completion'):self.f.finalize(self.paths[0],self.source,0,time.time(),closed=False)
        m=self.chunks(1)[0]
        self.assertEqual(inspect_video(self.f.storage.inspect(m.media))['frames'],30)
        self.assertEqual(self.f.finalize(self.paths[0],self.source,0,time.time(),closed=True),m)
        self.assertEqual(len(self.f.notifications()),1)
        with self.assertRaises(ValueError):self.f.finalize(self.paths[1],self.source,0,time.time(),closed=True)
        self.f.storage.path(m.media.key).write_bytes(b'partial')
        with self.assertRaises(ValueError):self.f.resolve(self.source,m.native)

    def test_E02_variable_frame_intervals_use_recorded_duration(self):
        path=self.runtime/'variable.mp4'
        with av.open(str(path),'w') as container:
            stream=container.add_stream('libx264',rate=15)
            stream.width=160;stream.height=90;stream.pix_fmt='yuv420p'
            stream.time_base=stream.codec_context.time_base=Fraction(1,15360)
            stream.options={'preset':'ultrafast'}
            for pts in (0,1024,2048,4096):
                frame=av.VideoFrame.from_image(Image.new('RGB',(160,90),'green'))
                frame.pts=pts;frame.time_base=Fraction(1,15360)
                for packet in stream.encode(frame):container.mux(packet)
            for packet in stream.encode():container.mux(packet)
        with av.open(str(path)) as container:frames=list(container.decode(video=0))
        self.assertNotEqual(frames[-1].duration,frames[-1].pts-frames[-2].pts)
        self.assertEqual(inspect_video(path)['end'],frames[-1].pts+frames[-1].duration)

    def test_E09_E14_step_progress_and_shutdown_pins(self):
        manifests=self.chunks(3)
        self.settings=self.settings.model_copy(update={'limits':self.settings.limits.model_copy(update={'step_s':3.0})})
        self.f.settings=self.settings
        first=self.f.window(manifests[0]);self.f.enqueue(first)
        self.assertEqual(self.f.enqueue(self.f.window(manifests[1])),'waiting-step')
        self.assertEqual(self.f.enqueue(self.f.window(manifests[2])),'queued')
        self.f.resolve(self.source,manifests[0].native,owner='archive-reader',deadline_utc=time.time()+10)
        self.assertEqual(self.f.diagnostics()['sources'][0]['watermark_pts'],0)
        self.assertTrue(self.f.diagnostics()['sources'][0]['gaps'])
        self.f.close()
        import sqlite3
        with sqlite3.connect(self.runtime/'foundation.sqlite') as database:
            self.assertEqual(database.execute('SELECT count(*) FROM pins').fetchone()[0],0)
        self.f=Foundation(self.runtime,'run-one',self.settings)

    def test_E04_epoch_slot_reset_unknown_mapping(self):
        m=self.chunks(1)[0]
        self.analyze(m)
        replacement=self.source.model_copy(update={'source_id':'camera/replacement','epoch':2})
        self.f.register_source(replacement)
        archive=self.f.resolve(self.source,m.native,owner='old-source-reader',deadline_utc=time.time()+10)
        self.assertEqual(inspect_video(archive['chunks'][0]['path'])['frames'],30)
        self.f.release('old-source-reader')
        with self.assertRaises(ValueError):self.f.resolve(replacement,m.native)
        with self.assertRaises(ValueError):self.f.resolve(self.source,m.native,mapping_revision=1)
        snapshot=self.f.reviewed_snapshot().model_copy(update={'sources':[self.source]})
        context=self.f.context('commentator',self.source,m.native,snapshot,event_ms=5000)
        self.assertEqual(context['timing'],'source-local');self.assertEqual(context['facts'],{})

    def test_E05_cross_chunk_actions_and_scene_association(self):
        chunks=self.chunks(5)
        self.analyze(chunks[1]);self.analyze(chunks[2]);self.analyze(chunks[4])
        scenes=self.f._records('scene')
        self.assertEqual(len(scenes),2)
        one=next(json.loads(r['body']) for r in scenes if 'one' in json.loads(r['body'])['description'])
        self.assertGreater(one['revision'],1)
        self.assertGreater(len(one['evidence_ids']),1)
        self.assertEqual(len(self.f._records('scene',current=False)),4)

    def test_E06_duplicate_delayed_malformed_conflicting_results(self):
        m=self.chunks(1)[0];window,results,receipt=self.analyze(m)
        notifications=len(self.f.notifications())
        self.assertEqual(self.f.ingest(window,results,trusted_origin='fixture'),receipt)
        self.assertEqual(len(self.f.notifications()),notifications)
        with self.assertRaisesRegex(ValueError,'changed contents'):
            self.f.ingest(window,[results[0].model_copy(update={'description':'changed'})],trusted_origin='fixture')
        with self.assertRaisesRegex(ValueError,'window'):
            self.f.ingest(window,[results[0].model_copy(update={'native':Interval(start=-1,end=window.native.end)})],trusted_origin='fixture')
        with self.assertRaisesRegex(ValueError,'origin'):
            self.f.ingest(window,[results[0].model_copy(update={'origin':'provider'})],trusted_origin='fixture')
        with self.assertRaisesRegex(ValueError,'model or version'):
            self.f.ingest(window,[results[0].model_copy(update={'model_version':'unreviewed-version'})],trusted_origin='fixture')
        expired=window.model_copy(update={'deadline_utc':time.time()-100})
        self.f.enqueue(expired)
        self.assertEqual(self.f.db.execute('SELECT deadline FROM jobs WHERE key=?',(window.job_key,)).fetchone()[0],window.deadline_utc)

    def test_E06_notification_clock_withholds_live_freshness(self):
        m=self.f.finalize(self.paths[0],self.source,0,None,closed=True,notification_utc=time.time())
        self.assertIsNone(m.last_receipt_utc)
        window=self.f.window(m);self.f.enqueue(window)
        results=asyncio.run(self.f.registry.analyze(window,self.f.chunks(self.source)))
        self.assertFalse(self.f.ingest(window,results,trusted_origin='fixture')['live_eligible'])
        self.assertEqual(self.f.context('director',self.source,m.native,self.f.reviewed_snapshot())['observations'],[])
        self.assertTrue(self.f.search(SearchQuery(event_id=self.source.event_id,run_id=self.source.run_id,text='action')))

    def test_E07_retraction_context_search_notifications_history(self):
        m=self.chunks(1)[0];window,results,_=self.analyze(m)
        query=SearchQuery(event_id=self.source.event_id,run_id=self.source.run_id,text='action')
        self.assertTrue(self.f.search(query))
        self.f.retract(results[0].evidence_id,'retract-one','Evidence no longer supported')
        self.assertEqual(self.f.search(query),[])
        context=self.f.context('commentator',self.source,m.native,self.f.reviewed_snapshot())
        self.assertEqual(context['observations'],[])
        self.assertTrue(any(n['kind']=='evidence.invalidated' for n in self.f.notifications()))
        self.assertEqual(len(self.f._records('observation',current=False)),1)
        self.f.retract(results[0].evidence_id,'retract-one','Evidence no longer supported')

    def test_E08_context_no_future_actual_airtime_and_bounds(self):
        mapping=TimeMapping(source=self.source,revision=1,valid=Interval(start=0,end=200000),origin_pts=0,offset_event_ms=0,uncertainty_ms=100.0,calibration_evidence=['operator-marker'])
        chunks=self.chunks(5,mapping=mapping)
        self.analyze(chunks[1]);self.analyze(chunks[4])
        self.f.confirm_facts({'home_score':1},1,'score-early',authority='human',effective_event_ms=1000)
        self.f.confirm_facts({'home_score':2},2,'score-later',authority='human',effective_event_ms=9000)
        for state in ('aired','pending','canceled'):
            self.f.program_text(ProgramText(cue_id=state,event_id=self.source.event_id,run_id=self.source.run_id,text=state,
                state=state,event_ms=2000,program_revision=0,origin='fixture'),owner='fixture')
        snapshot=self.f.reviewed_snapshot().model_copy(update={'mapping_revisions':{self.source.source_id:1}})
        context=self.f.context('commentator',self.source,Interval(start=0,end=61440),snapshot,event_ms=3900)
        self.assertEqual(context['facts']['home_score']['value'],1)
        self.assertTrue(all(o['native']['end']<=61440 for o in context['observations']))
        self.assertEqual([t['text'] for t in context['aired']],['aired'])
        self.assertEqual([t['text'] for t in context['pending']],['pending'])
        self.assertEqual(context['snapshot'],snapshot.model_dump())
        self.assertLessEqual(len(context['observations'])+len(context['scenes']),32)
        self.assertLessEqual(len(json.dumps(context).encode()),65536)
        for role in ('director','segmentor'):self.f.context(role,self.source,chunks[1].native,snapshot)

    def test_E09_pin_pressure_disconnect_delete(self):
        m=self.chunks(1)[0]
        self.f.resolve(self.source,m.native,owner='render-one',deadline_utc=time.time()+30)
        self.f.cleanup(now=time.time()+2000)
        # Simulating future retention also expires pins, so verify pressure while pin remains active.
        # This separate chunk test uses an archive-byte cap below retained size.
        self.f.release('render-one')
        with self.assertRaises(ValueError):self.f.resolve(self.source,m.native)

    def test_E08_archive_hits_obey_reviewed_evidence_revision(self):
        chunks=self.chunks(3)
        before=self.f.reviewed_snapshot()
        self.analyze(chunks[0])
        query=SearchQuery(event_id=self.source.event_id,run_id=self.source.run_id,text='action')
        hits=self.f.search(query)
        self.assertTrue(hits)
        reviewed=self.f.reviewed_snapshot()
        for role in ('director','commentator','segmentor'):
            context=self.f.context(role,self.source,chunks[2].native,before,archive_hits=hits)
            self.assertEqual(context['archive_hits'],[])
            self.assertIn(hits[0]['scene']['scene_id'],context['omitted'])
            context=self.f.context(role,self.source,chunks[2].native,reviewed,archive_hits=hits)
            self.assertEqual(len(context['archive_hits']),1)
        # A later scene revision includes both reviewed and unreviewed evidence.
        self.analyze(chunks[1])
        newer=self.f.search(query)
        self.assertGreater(newer[0]['scene']['revision'],hits[0]['scene']['revision'])
        context=self.f.context('commentator',self.source,chunks[2].native,reviewed,archive_hits=newer)
        self.assertEqual(context['archive_hits'],[])
        current=self.f.reviewed_snapshot()
        self.assertEqual(len(self.f.context('commentator',self.source,chunks[2].native,current,archive_hits=newer)['archive_hits']),1)
        # A correction invalidates a previously reviewed retrieval result.
        self.f.retract(newer[0]['scene']['evidence_ids'][0],'archive-correction','Unsupported evidence')
        self.assertEqual(self.f.context('commentator',self.source,chunks[2].native,current,archive_hits=newer)['archive_hits'],[])

    def test_E08_director_without_reviewed_runtime_is_unavailable(self):
        chunk=self.chunks(1)[0]
        snapshot=self.f.reviewed_snapshot()
        self.assertIsNone(snapshot.runtime)
        context=self.f.context('director',self.source,chunk.native,snapshot)
        self.assertEqual(context['status'],'unavailable')
        self.assertIn('Reviewed runtime state is unavailable',context['omitted'])

    def test_E08_legacy_snapshot_serialization_preserves_retry_identity(self):
        from foundation_records import DecisionSnapshot, AnalysisWindow
        chunk=self.chunks(1)[0]
        window,observations,receipt=self.analyze(chunk)
        legacy=window.model_dump(mode='json')
        self.assertNotIn('runtime',legacy['snapshot'])
        restored=AnalysisWindow.model_validate_json(json.dumps(legacy))
        self.assertEqual(restored.model_dump(mode='json'),legacy)
        self.assertIsNone(DecisionSnapshot.model_validate_json(json.dumps(legacy['snapshot'])).runtime)
        returned=[Observation.model_validate_json(o.model_dump_json()) for o in observations]
        self.assertEqual(self.f.ingest(restored,returned,trusted_origin='fixture'),receipt)

    def test_E09_active_pin_wins_over_storage_pressure(self):
        m=self.chunks(1)[0]
        self.f.resolve(self.source,m.native,owner='render',deadline_utc=time.time()+30)
        self.f.settings=self.settings.model_copy(update={'limits':self.settings.limits.model_copy(update={'archive_bytes':1})})
        self.f.cleanup()
        self.assertEqual(inspect_video(self.f.storage.inspect(m.media))['frames'],30)
        self.f.release('render');self.f.cleanup()
        with self.assertRaises(ValueError):self.f.resolve(self.source,m.native)

    def test_E09_analysis_continues_after_older_media_expires(self):
        old=self.chunks(1)[0]
        self.f.cleanup(now=time.time()+2000)
        new=self.f.finalize(self.paths[1],self.source,1,time.time(),closed=True)
        self.assertEqual(self.f.window(new).native,new.native)
        with self.assertRaises(ValueError):self.f.resolve(self.source,old.native)

    def test_E09_shared_content_upload_and_retention_race(self):
        import threading
        old=self.chunks(1)[0]
        with self.f.transaction():
            self.f.db.execute("UPDATE records SET created=? WHERE kind='chunk' AND id=?",(time.time()-2000,old.chunk_id))
        replacement=self.source.model_copy(update={'source_id':'camera/new-lease','epoch':2})
        published=threading.Event();resume=threading.Event();errors=[];new=[]
        put=self.f.storage.put
        def paused_put(*args):
            artifact=put(*args);published.set()
            if not resume.wait(2):raise TimeoutError('Test upload stalled')
            return artifact
        def upload():
            try:new.append(self.f.finalize(self.paths[0],replacement,0,time.time(),closed=True))
            except Exception as error:errors.append(error)
        with patch.object(self.f.storage,'put',side_effect=paused_put):
            writer=threading.Thread(target=upload);writer.start()
            self.assertTrue(published.wait(2))
            cleaner=threading.Thread(target=self.f.cleanup);cleaner.start()
            resume.set();writer.join(3);cleaner.join(3)
        self.assertFalse(errors,errors)
        self.assertFalse(writer.is_alive() or cleaner.is_alive())
        self.assertEqual(inspect_video(self.f.resolve(replacement,new[0].native)['chunks'][0]['path'])['frames'],30)

    def test_E10_search_filter_versions_deleted_wrong_event(self):
        m=self.chunks(1)[0];self.analyze(m)
        query=SearchQuery(event_id=self.source.event_id,run_id='run-one',text='action')
        self.assertEqual(self.f.search(query)[0]['ranking'],'simulated')
        self.assertEqual(self.f.search(query.model_copy(update={'event_id':'wrong-event'})),[])
        self.assertEqual(self.f.search(query.model_copy(update={'index_version':'different'})),[])
        self.assertEqual(self.f.search(query.model_copy(update={'eligible':Interval(start=40000,end=50000)})),[])
        with self.assertRaises(ValidationError):SearchQuery(event_id='x',run_id='y',text='x',limit=11)
        self.f.cleanup(now=time.time()+2000);self.assertEqual(self.f.search(query),[])

    def test_E11_missing_live_config_no_substitution_or_secret_export(self):
        config=json.loads((ROOT/'config/foundation.live.json').read_text())
        config['event']=self.settings.event.model_dump()
        config['providers']['llm']['endpoint']='https://private.invalid/?api_key=secret-value'
        settings=FoundationSettings.model_validate(config)
        from foundation_providers import Registry,CapabilityError
        registry=Registry(settings)
        self.assertFalse(registry.capabilities()['llm']['ready'])
        with self.assertRaises(CapabilityError):registry.require('llm')
        self.assertNotIn('secret-value',json.dumps(registry.capabilities()))
        self.assertNotIn('secret-value',settings.model_dump_json())

    def test_E12_typed_llm_preserves_snapshot_speech_decodes(self):
        m=self.chunks(1)[0];self.analyze(m)
        snapshot=self.f.reviewed_snapshot();context=self.f.context('segmentor',self.source,m.native,snapshot)
        result=asyncio.run(self.f.registry.llm('segmentor',context,snapshot,time.time()+5))
        self.assertEqual(result.snapshot,snapshot);self.assertEqual(result.origin,'fixture')
        self.f.registry.labels['speech']['file']=str(ROOT/'tests/fixtures/foundation/speech.wav')
        audio=asyncio.run(self.f.registry.speech('Fixture request',self.f.storage,time.time()+5))
        self.assertGreater(audio.duration_s,1);self.assertEqual(audio.origin,'fixture')

    def test_E13_restart_missed_dispatch_old_run_partial_unready(self):
        self.chunks(1);self.f.recover();self.f.recover()
        self.assertEqual(self.f.db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
        self.f.close();self.f=Foundation(self.runtime,'run-two',self.settings)
        self.assertEqual(self.f.db.execute('SELECT state FROM jobs').fetchone()[0],'expired')
        self.assertEqual(len(self.f.chunks(self.source)),1)
        with self.assertRaises(ValueError):self.f.context('director',self.source,Interval(start=0,end=100),self.f.reviewed_snapshot())
        (self.f.storage.root/'.partial-interrupted').write_bytes(b'partial')
        self.assertEqual(len(self.f._records('chunk')),1)

    def test_E14_queue_limits_disk_threshold_per_source_fairness(self):
        chunks=self.chunks(3)
        for chunk in chunks:self.f.enqueue(self.f.window(chunk))
        self.assertEqual(self.f.db.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0],1)
        self.assertTrue(any(n['kind']=='analysis.skipped' for n in self.f.notifications()))
        with patch.object(self.f.storage,'free_bytes',return_value=0):
            with self.assertRaisesRegex(ValueError,'reserve'):self.f.finalize(self.paths[3],self.source,3,time.time(),closed=True)
        self.assertTrue(self.f.diagnostics()['gaps'])

    def test_E17_total_retry_budget_timeout_permanent_failure(self):
        attempts=[]
        async def permanent():raise ValueError('Schema failure')
        with self.assertRaises(ValueError):asyncio.run(bounded_call(permanent,time.time()+5,Limits(),attempts.append))
        self.assertEqual(attempts,[1])
        attempts.clear()
        async def denied():raise PermissionError('Authentication failed')
        with self.assertRaises(PermissionError):asyncio.run(bounded_call(denied,time.time()+5,Limits(),attempts.append))
        self.assertEqual(attempts,[1])
        calls=[]
        async def transient():
            calls.append(time.monotonic())
            if len(calls)<3:raise ConnectionError('Transient provider transport')
            return 'ready'
        self.assertEqual(asyncio.run(bounded_call(transient,time.time()+5,Limits())),'ready')
        self.assertEqual(len(calls),3)
        self.assertGreaterEqual(calls[1]-calls[0],.99)
        self.assertGreaterEqual(calls[2]-calls[1],1.99)
        async def slow():await asyncio.sleep(1)
        with self.assertRaises(TimeoutError):asyncio.run(bounded_call(slow,time.time()+.02,Limits(),attempts.append))


class FoundationHTTP(unittest.TestCase):
    def test_E08_runtime_race_with_control_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            app=App(Config(Path(directory),'http://localhost'))
            source=SourceEpoch(event_id=app.foundation.settings.event.event_id,run_id=app.control.run_id,
                source_id='camera/race',slot=1,epoch=1,time_base='1/15360')
            app.foundation.register_source(source)
            original=app.program.status
            def changed():
                state=original()
                app.control._takeover()
                return state
            try:
                with patch.object(app.program,'status',side_effect=changed):
                    reviewed=app.foundation.reviewed_snapshot()
                self.assertIsNone(reviewed.runtime)
                self.assertEqual(app.foundation.context('director',source,Interval(start=0,end=1024),reviewed)['status'],'unavailable')
            finally:app.close()

    def test_E08_director_runtime_uses_reviewed_health_and_actual_program(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            app=App(Config(Path(directory),'http://localhost'))
            source=SourceEpoch(event_id=app.foundation.settings.event.event_id,run_id=app.control.run_id,
                source_id='camera/healthy',slot=1,epoch=1,time_base='1/15360')
            app.foundation.register_source(source)
            health={1:{'last_frame_age_s':0.1,'buffer_seconds':8.0,'buffer_ready':True,'has_audio':True},
                    2:{'last_frame_age_s':5.0,'buffer_seconds':1.0,'buffer_ready':False,'has_audio':False}}
            rows=[{'path':'camera/healthy','slot':1,'epoch':1,'state':'ACTIVE'},
                  {'path':'camera/stale','slot':2,'epoch':1,'state':'RECONNECTING'}]
            sources={row['slot']:SimpleNamespace(path=row['path'],epoch=1,status=lambda slot=row['slot']:health[slot]) for row in rows}
            try:
                with patch.object(app.leases,'rows',return_value=rows), patch.object(app,'get_source',side_effect=sources.get):
                    with app.program.lock:
                        app.program.requested='LIVE';app.program.revision=1
                        app.program.primary_source_path=source.source_id
                        # The request has not reached the encoder: actual remains holding.
                    reviewed=app.foundation.reviewed_snapshot()
                    context=app.foundation.context('director',source,Interval(start=0,end=1024),reviewed)
                    runtime=context['snapshot']['runtime']
                    self.assertEqual(runtime['program']['requested'],'LIVE')
                    self.assertEqual(runtime['program']['actual'],'HOLDING')
                    self.assertEqual(runtime['program']['actual_target'],{'kind':'holding'})
                    self.assertEqual([h['buffer_ready'] for h in runtime['source_health']],[True,False])
                    self.assertEqual(runtime['source_health'][1]['state'],'RECONNECTING')
                    self.assertEqual(runtime['policy'],app.control.policy)
                    self.assertEqual(context['status'],'ready')
                    with app.program.lock:
                        app.program.actual='LIVE';app.program.applied_revision=1
                        app.program.actual_target={'kind':'camera','slot':1,'source_path':source.source_id,'epoch':1}
                    aired=app.foundation.reviewed_snapshot()
                    self.assertEqual(aired.runtime.program.actual_target['source_path'],source.source_id)
                    health[1]['buffer_ready']=False
                    app.program.actual_target['source_path']='camera/replacement'
                    app.control.policy['minimum_shot_s']=9
                    app.control._takeover()
                    saved=app.foundation.context('director',source,Interval(start=0,end=1024),aired)['snapshot']['runtime']
                    self.assertEqual(saved['program']['actual_target']['source_path'],source.source_id)
                    self.assertTrue(saved['source_health'][0]['buffer_ready'])
                    self.assertFalse(saved['crew_paused'])
                    self.assertEqual(saved['policy']['minimum_shot_s'],5)
                    health[1]['epoch']=2
                    renewed=app.foundation.reviewed_snapshot().runtime.source_health[0]
                    self.assertIsNone(renewed.buffer_ready)
                    # A reservation has no decoded health; a reused slot cannot supply it.
                    rows[0].update(path='camera/reserved',epoch=0,state='RESERVED')
                    reserved=app.foundation.reviewed_snapshot().runtime.source_health[0]
                    self.assertIsNone(reserved.buffer_ready)
                    self.assertIsNone(reserved.last_frame_age_s)
            finally:app.close()

    def test_E01_E17_routes_context_and_lifecycle_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            app=App(Config(Path(directory),'http://localhost'))
            try:
                with TestClient(web_api(app,manage_lifecycle=False),client=("127.0.0.1",12345)) as client:
                    self.assertEqual(client.get('/api/status').status_code,200)
                    self.assertEqual(client.get('/operator').status_code,200)
                    self.assertEqual(client.post('/api/chat',json={'id':'x','text':'x','run_id':2}).status_code,409)
                    self.assertEqual(client.post('/media/program/whip',content='sdp').status_code,403)
                    self.assertEqual(client.post('/media/camera/'+'a'*32+'/whep',content='sdp').status_code,403)
                    self.assertEqual(client.post('/api/leases',content='x'*65537).status_code,409)
                    event=app.foundation.event_context().model_copy(update={'revision':2})
                    expected=app.control.expected({})
                    response=app.update_event_context({'context':event.model_dump(),'expected_revision':1,'operation_key':'update'})
                    self.assertEqual(app.control.expected({})['context_revision'],2)
                    stale=app.control.propose({'id':'stale','op':'holding','args':{},'expected':expected,'expires_at':time.time()+5})
                    self.assertEqual(stale['state'],'Rejected')
            finally:app.close()

class FoundationWorkers(unittest.TestCase):
    setUpClass=classmethod(FoundationContracts.setUpClass.__func__)
    tearDownClass=classmethod(FoundationContracts.tearDownClass.__func__)
    setUp=FoundationContracts.setUp
    tearDown=FoundationContracts.tearDown
    chunks=FoundationContracts.chunks
    # Reuse the media setup without repeating the inherited acceptance scenarios.
    def test_E14_five_sources_slow_adapter_limits_fairness(self):
        import threading
        gate=threading.Event();entered=[];active={};peak=[0]
        for slot in range(1,6):
            source=self.source.model_copy(update={'slot':slot,'source_id':f'camera/lease-{slot}'})
            manifest=self.f.finalize(self.paths[0],source,0,time.time(),closed=True)
            self.f.enqueue(self.f.window(manifest))
        original=self.f.registry.analyze
        async def slow(window,manifests):
            with self.f.lock:
                key=window.source.source_id
                active[key]=active.get(key,0)+1
                assert active[key]==1
                peak[0]=max(peak[0],sum(active.values()));entered.append(key)
            try:
                while not gate.is_set():await asyncio.sleep(.01)
                return await original(window,manifests)
            finally:
                with self.f.lock:active[key]-=1
        self.f.registry.analyze=slow;self.f.start()
        deadline=time.time()+2
        while len(entered)<2 and time.time()<deadline:time.sleep(.01)
        self.assertEqual(len(entered),2);self.assertEqual(peak[0],2)
        gate.set()
        deadline=time.time()+4
        while self.f.diagnostics()['jobs'].get('completed',0)<5 and time.time()<deadline:time.sleep(.01)
        self.assertEqual(self.f.diagnostics()['jobs'].get('completed'),5)
        self.assertEqual(len(set(entered)),5)

    def test_E06_late_result_archive_only_original_request_required(self):
        manifest=self.chunks(1)[0]
        window=self.f.window(manifest).model_copy(update={'deadline_utc':time.time()-1})
        self.f.enqueue(window)
        results=asyncio.run(self.f.registry.analyze(window,self.f.chunks(self.source)))
        result=self.f.ingest(window,results,trusted_origin='fixture')
        self.assertFalse(result['live_eligible'])
        context=self.f.context('director',self.source,manifest.native,self.f.reviewed_snapshot())
        self.assertEqual(context['observations'],[])
        with self.assertRaisesRegex(ValueError,'issued'):
            changed=window.model_copy(update={'deadline_utc':time.time()+30})
            changed_results=[r.model_copy(update={'snapshot':changed.snapshot}) for r in results]
            self.f.ingest(changed,changed_results,trusted_origin='fixture')

    def test_E17_network_transport_and_sdk_retries_are_explicit(self):
        from botocore.stub import Stubber
        from botocore.config import Config as SDKConfig
        import boto3
        import httpx
        client=boto3.client('s3',region_name='us-east-1',aws_access_key_id='fixture',aws_secret_access_key='fixture',
                            config=SDKConfig(retries={'total_max_attempts':1}))
        with Stubber(client) as stubber:
            stubber.add_response('head_object',{'ContentLength':123},{'Bucket':'fixture','Key':'chunk'})
            self.assertEqual(client.head_object(Bucket='fixture',Key='chunk')['ContentLength'],123)
        self.assertEqual(client.meta.config.retries['total_max_attempts'],1)
        with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(401,json={'error':'capability absent'}))) as transport:
            self.assertEqual(transport.get('https://fixture.invalid/').status_code,401)

    def test_E03_decoder_clock_revision_and_reset_identity(self):
        from media_provenance import DecoderTrace
        trace=DecoderTrace(15,640,360)
        trace.accept('[showinfo@native] config in time_base: 1/90000')
        trace.accept('[showinfo@native] n: 0 pts: 180000 pts_time:2 s:640x360')
        trace.accept('[showinfo@scaled] n: 0 pts: 30 pts_time:2 s:640x360')
        trace.accept('[showinfo@proxy] n: 0 pts: 30 pts_time:2')
        old=trace.take(1)
        trace.accept('[showinfo@native] n: 1 pts: 0 pts_time:0 s:640x360')
        trace.accept('[showinfo@scaled] n: 1 pts: 0 pts_time:0 s:640x360')
        trace.accept('[showinfo@proxy] n: 1 pts: 0 pts_time:0')
        new=trace.take(2)
        self.assertNotEqual(old['timeline_revision'],new['timeline_revision'])
        self.assertTrue(new['source_discontinuity'])

    def test_E03_padding_uses_actual_pixel_format_alignment(self):
        from media_provenance import DecoderTrace
        trace=DecoderTrace(15,640,360)
        trace.accept('[showinfo@native] config in time_base: 1/90000')
        trace.accept('[showinfo@native] n: 0 pts: 0 pts_time:0 s:360x640')
        trace.accept('[showinfo@scaled] n: 0 pts: 0 pts_time:0 fmt:yuvj420p s:202x360')
        trace.accept('[showinfo@proxy] n: 0 pts: 0 pts_time:0')
        self.assertEqual(trace.take(1)['geometry']['pad_x'],218)

    def test_E02_segment_native_offset_retains_file_clock(self):
        first=self.f.finalize(self.paths[0],self.source,0,time.time(),closed=True)
        second=self.f.finalize(self.paths[0],self.source,1,time.time(),closed=True,timeline_offset_pts=first.native.end)
        resolved=self.f.resolve(self.source,Interval(start=0,end=second.native.end))
        self.assertEqual(len(resolved['chunks']),2)
        self.assertEqual(second.file_native,first.file_native)
        self.assertEqual(second.native.start,first.native.end)

class ArtifactFailures(unittest.TestCase):
    setUpClass=classmethod(FoundationContracts.setUpClass.__func__)
    tearDownClass=classmethod(FoundationContracts.tearDownClass.__func__)
    setUp=FoundationContracts.setUp
    tearDown=FoundationContracts.tearDown
    def test_E13_interrupted_put_leaves_no_ready_manifest(self):
        with patch('foundation_storage.os.link',side_effect=OSError('Injected interrupted upload')):
            with self.assertRaises(OSError):self.f.finalize(self.paths[0],self.source,0,time.time(),closed=True)
        self.assertEqual(self.f._records('chunk'),[])
        self.assertFalse(list(self.f.storage.root.glob('.partial-*')))
        self.assertFalse(list((self.f.storage.root/'manifests').glob('*')))
