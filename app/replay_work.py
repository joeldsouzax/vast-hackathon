"""Bounded replay preparation and recall; no authority to grant airtime."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
from foundation import canonical, operation_key
from foundation_providers import CapabilityError
from foundation_records import (Abstention, Interval, PublicSearchRequest, PublicSearchHit, PublicSearchResult,
    ReplayCandidate, ReplayPlan12, SceneEvent, SearchQuery, SegmentPlan, SegmentWait, SegmentorResult,
    ShotIntent, SourceEpoch)
from replay_inputs import check_budget, resolve_plan, verify_dependencies, published_dependencies
from provider_errors import public_failure


class ReplayError(ValueError):
    def __init__(self, code, message=None, status=410):
        self.code=code;self.status=status
        super().__init__(message or code.replace('_',' '))


def fixture_segment(labels, context):
    """Scripted edits refer only to labeled, inspected fixture evidence."""
    observations=context.get('observations',[])
    scenes=context.get('scenes',[])
    selected=context['target'].get('scene_id')
    scene=next((s for s in scenes if s['scene_id']==selected),None)
    if not scene:return {'op':'abstain','reason':'Selected scene has no reviewed view evidence'}
    evidence=[o for o in observations if o['evidence_id'] in scene['evidence_ids']]
    script=next((labels.get('segmentor',{}).get(o.get('association_key')) for o in evidence
                 if o.get('association_key') in labels.get('segmentor',{})),None)
    if not script:return {'op':'abstain','reason':'No labeled fixture edit for this action'}
    source=scene['source'];base=float(Fraction(source['time_base']))
    required={k:round(v/base) for k,v in script['required_s'].items()}
    action={k:round(v/base) for k,v in script['action_s'].items()}
    if scene['native']['end']<required['end']:
        return {'op':'wait','source':source,'required':required,'reason':'Wait for labeled action aftermath'}
    # Long edits split into bounded consecutive shots. No repair or inferred boundary.
    shots=[];start=required['start'];step=max(1,round(6/base))
    while start<required['end']:
        end=min(start+step,required['end'])
        refs=[o['evidence_id'] for o in evidence if o['native']['start']<end and o['native']['end']>start]
        shots.append({'source':source,'native':{'start':start,'end':end},
            'scene_revisions':{scene['scene_id']:scene['revision']},'evidence_ids':refs,
            'speed':script.get('speed',1.),'reason':'Labeled lead-in, action and aftermath'})
        start=end
    return {'op':'plan','source':source,'required':required,'action':action,'shots':shots,
            'reason':'Explicit retained-media fixture edit'}


def deterministic_segment(context, scene, snapshot, chunks=None):
    """Build a simple retained-media plan when the segmentor LLM format fails."""
    observations=context.get('observations',[])
    evidence=[o for o in observations if o['evidence_id'] in scene.evidence_ids]
    if not evidence:return None
    source=scene.source
    base=float(Fraction(source.time_base))
    # Align required to retained chunk ends so last-frame receipt verification matches.
    if chunks:
        native=Interval(start=min(m.native.start for m in chunks),end=max(m.native.end for m in chunks))
    else:
        native=scene.native
    if (native.end-native.start)*base<0.2-1e-9:return None
    # Shots must lie inside usable view intervals; uncovered time fails as coverage_gap.
    views=sorted((max(o['view']['native']['start'],native.start),min(o['view']['native']['end'],native.end),o['evidence_id'])
        for o in evidence if o.get('view') and o['view'].get('subject_visible') and o['view'].get('quality')=='usable')
    segments=[]
    for start,end,eid in views:
        if end<=start:continue
        if segments and start<=segments[-1][1]:
            segments[-1][1]=max(segments[-1][1],end);segments[-1][2].append((start,end,eid))
        else:segments.append([start,end,[(start,end,eid)]])
    shots=[];step=max(1,round(6/base))
    for seg_start,seg_end,members in segments:
        start=seg_start
        while start<seg_end and len(shots)<16:
            end=min(start+step,seg_end)
            if (end-start)*base<0.2-1e-9:break
            refs=list(dict.fromkeys(eid for a,b,eid in members if a<end and b>start))
            shots.append(ShotIntent(source=source,native=Interval(start=start,end=end),
                scene_revisions={scene.scene_id:scene.revision},evidence_ids=refs[:16],
                reason='Deterministic edit inside usable observed views'))
            start=end
    if not shots:return None
    covered=Interval(start=shots[0].native.start,end=shots[-1].native.end)
    plan=SegmentPlan(op='plan',source=source,action=covered,required=covered,shots=shots,
        reason='Deterministic retained-media edit after segmentor format failure')
    return SegmentorResult(payload=plan,snapshot=snapshot,origin='provider',
        model_id='deterministic-segmentor',model_version='1')


@dataclass
class PlaybackTicket:
    owner: str
    replay_id: str
    plan: ReplayPlan12
    chunk_ids: tuple[str, ...]
    deadline: float
    output_hash: str


class ReplayWork:
    def __init__(self, app):
        self.app=app;self.foundation=app.foundation;self.settings=self.foundation.settings.replay
        self.lock=threading.RLock();self.candidates={};self.query_waiters={};self.query_count=0
        self.pending={'recall':None,'automatic':None};self.planning=False
        self.tickets={};self.output_readers={};self.aired=set()
        self.reason='Awaiting current source evidence' if self.settings.enabled else 'Automatic replay preparation is disabled'
        self.cursor=self.foundation.cursor('replays');self.last_reconcile=0.
        self.thread=threading.Thread(target=self._run,name='replay-coordinator',daemon=True)
        with self.foundation.lock:
            for row in self.foundation._records('replay_candidate',run=app.control.run_id):
                self.candidates[row['id']]=ReplayCandidate.model_validate_json(row['body'])

    def start(self):self.thread.start()

    def status(self):
        with self.lock:
            automatic=[c for c in self.candidates.values() if c.purpose=='automatic']
            ready=sum(c.state=='ready' and c.deadline_utc>time.time() and c.candidate_id not in self.aired for c in automatic)
            scheduling=('disabled' if not self.settings.enabled or not self.app.control.policy['replays_enabled'] else
                'waiting_start' if not self.app.control.program_started else
                'paused' if self.app.control.crew_paused else
                'playing' if self.app.program.actual=='REPLAY' else
                'ready' if ready else 'preparing' if self.planning or self.pending['automatic'] else 'waiting_evidence')
            return {'enabled':self.settings.enabled,'reason':self.reason,'planning':self.planning,
                'scheduling':scheduling,'automatic_ready':ready,'automatic_prepared':len(automatic),'automatic_aired':len(self.aired),
                'pending':{k:v for k,v in self.pending.items()},'limits':self.settings.model_dump(),
                'candidates':[c.model_dump(mode='json') for c in self.candidates.values()][-32:]}

    def _save(self, candidate):
        with self.foundation.transaction():
            row=self.foundation.db.execute("SELECT max(revision) FROM records WHERE kind='replay_candidate' AND id=?",(candidate.candidate_id,)).fetchone()
            self.foundation.db.execute("UPDATE records SET active=0 WHERE kind='replay_candidate' AND id=?",(candidate.candidate_id,))
            self.foundation._put('replay_candidate',candidate.candidate_id,(row[0] or 0)+1,candidate)
        with self.lock:self.candidates[candidate.candidate_id]=candidate

    def _update(self, candidate, **changes):
        updated=ReplayCandidate.model_validate({**candidate.model_dump(),**changes})
        self._save(updated);return updated

    def scene(self, scene_id, revision):
        with self.foundation.lock:
            row=self.foundation.db.execute("SELECT body FROM records WHERE kind='scene' AND id=? AND active=1 AND run=?",
                (scene_id,self.app.control.run_id)).fetchone()
            if not row:raise ReplayError('scene_changed')
            scene=SceneEvent.model_validate_json(row[0])
            if scene.status=='retracted':raise ReplayError('evidence_retracted')
            if scene.revision!=revision:raise ReplayError('scene_changed')
            if self.foundation.invalidated_evidence.intersection(scene.evidence_ids):raise ReplayError('evidence_retracted')
            return scene

    def search(self, raw):
        request=PublicSearchRequest.model_validate(raw)
        if self.app.stop.is_set() or request.run_id!=self.app.control.run_id:raise ReplayError('wrong_run',status=409)
        key='search-'+request.run_id+'-'+request.id
        signature=operation_key(request.model_dump());deadline=time.time()+self.settings.query_s
        with self.lock,self.foundation.transaction():
            old=self.foundation.db.execute('SELECT * FROM operations WHERE key=?',(key,)).fetchone()
            if old:
                if old['signature']!=signature:raise ReplayError('id_conflict','Search ID was reused with changed content',409)
                stored=json.loads(old['result'])
                if stored['state']=='complete':return stored['result']
                if stored['state']=='failed':raise ReplayError(**stored['error'])
                event=self.query_waiters.get(key)
                if event is None:raise ReplayError('provider_unavailable','Previous search was interrupted',503)
            else:
                try:self.foundation.registry.require('search')
                except CapabilityError as error:raise ReplayError('provider_unavailable','Search capability is unavailable',503) from error
                count=self.foundation.db.execute("SELECT count(*) FROM operations WHERE key LIKE ?",('search-'+request.run_id+'-%',)).fetchone()[0]
                if count>=self.settings.query_records or self.query_count>=2:raise ReplayError('capacity_reached','Search capacity reached',429)
                event=threading.Event();self.query_waiters[key]=event;self.query_count+=1
                self.foundation.db.execute('INSERT INTO operations VALUES (?,?,?)',
                    (key,signature,canonical({'state':'pending','request':request.model_dump(),'deadline':deadline})))
                def work():
                    outcome=None
                    try:
                        check_budget(deadline)
                        provider=self.foundation.registry.require('search')
                        query=SearchQuery(event_id=self.foundation.settings.event.event_id,run_id=request.run_id,text=request.text,
                            limit=request.limit,index_version=provider.version if provider.adapter=='live' else self.settings.index_version,
                            embedding_version=provider.model_id if provider.adapter=='live' else self.settings.embedding_version)
                        internal=self.foundation.search(query,deadline_utc=deadline)
                        hits=[]
                        for hit in internal:
                            scene=SceneEvent.model_validate(hit['scene']);event_interval=None
                            mapping=hit['media']['chunks'][0]['manifest'].get('event_mapping')
                            if mapping:
                                from foundation_records import TimeMapping
                                m=TimeMapping.model_validate(mapping)
                                if m.valid.start<=scene.native.start<scene.native.end<=m.valid.end:
                                    event_interval=Interval(start=round(m.event_ms(scene.native.start)),
                                        end=round(m.offset_event_ms+(scene.native.end-m.origin_pts)*float(Fraction(scene.source.time_base))*1000*m.rate_correction))
                            hits.append(PublicSearchHit(scene_id=scene.scene_id,scene_revision=scene.revision,source=scene.source,
                                native=scene.native,event=event_interval,description=scene.description[:2048],score=hit['score'],
                                claim_kind='inferred' if any(self.foundation._observation(eid).kind=='inferred' for eid in scene.evidence_ids) else 'observed',
                                ranking=hit['ranking'],available=True,index_version=hit['index_version'],embedding_version=hit['embedding_version']))
                        with self.foundation.lock:
                            entries=[json.loads(r['body']) for r in self.foundation._records('index',run=request.run_id)]
                        latest={}
                        for entry in entries:
                            source=entry['scene']['source']
                            latest[(source['source_id'],source['epoch'])]={'source':source,'watermark':entry['watermark']}
                        result=PublicSearchResult(id=request.id,run_id=request.run_id,hits=hits,
                            freshness=list(latest.values())[-5:],ranking='simulated' if self.foundation.registry.require('search').adapter=='fixture' else 'provider')
                        if len(result.model_dump_json().encode())>65536:raise ReplayError('capacity_reached','Search response exceeds 64 KiB',429)
                        check_budget(deadline)
                        outcome={'state':'complete','result':result.model_dump(mode='json')}
                    except Exception as error:
                        failure=public_failure(error,'search')
                        outcome={'state':'failed','error':{'code':failure['code'],'message':failure['reason'],'status':503},
                            'provider_failure':failure}
                    finally:
                        with self.lock,self.foundation.transaction():
                            self.foundation.db.execute('UPDATE operations SET result=? WHERE key=?',(canonical(outcome),key))
                            self.query_count-=1;event.set();self.query_waiters.pop(key,None)
                try:self.foundation.submit_role('search',work)
                except BaseException as error:
                    self.query_count-=1;self.query_waiters.pop(key,None)
                    self.foundation.db.execute('DELETE FROM operations WHERE key=?',(key,))
                    if isinstance(error,ValueError) and str(error).startswith('capacity_reached'):
                        raise ReplayError('capacity_reached','Search queue is full',429) from error
                    raise
        if not event.wait(max(0,deadline-time.time())):raise ReplayError('deadline_missed','Search timed out',503)
        with self.foundation.lock:
            stored=json.loads(self.foundation.db.execute('SELECT result FROM operations WHERE key=?',(key,)).fetchone()[0])
        if stored['state']=='failed':raise ReplayError(**stored['error'])
        return stored['result']

    def selected_hit(self, search_id, scene_id, revision):
        key='search-'+self.app.control.run_id+'-'+search_id
        with self.foundation.lock:
            row=self.foundation.db.execute('SELECT result FROM operations WHERE key=?',(key,)).fetchone()
        if not row:raise ReplayError('missing_reference','Search result not found',404)
        record=json.loads(row[0])
        if record['state']!='complete':raise ReplayError('provider_unavailable',status=503)
        hit=next((h for h in record['result']['hits'] if (h['scene_id'],h['scene_revision'])==(scene_id,revision)),None)
        if not hit:raise ReplayError('missing_reference','Selected hit was not in this query',404)
        provider=self.foundation.registry.require('search')
        expected=(provider.version,provider.model_id) if provider.adapter=='live' else (self.settings.index_version,self.settings.embedding_version)
        if (hit['index_version'],hit['embedding_version'])!=expected:
            raise ReplayError('index_changed','Search configuration changed',409)
        scene=self.scene(scene_id,revision)
        if scene.source.model_dump()!=hit['source']:raise ReplayError('scene_changed')
        try:self.foundation.resolve(scene.source,scene.native)
        except ValueError as error:raise ReplayError('media_hash_changed' if 'hash' in str(error) or 'size' in str(error) else 'media_unavailable') from error
        return scene

    def prepare_hit(self, args, preparation_id):
        scene=self.selected_hit(args['search_id'],args['scene_id'],args['scene_revision'])
        now=time.time();key=operation_key(scene.source.event_id,scene.source.run_id,scene.scene_id,'recall',preparation_id)
        candidate=ReplayCandidate(candidate_id=key,scene_id=scene.scene_id,scene_revision=scene.revision,
            source=scene.source,purpose='recall',admitted_utc=now,deadline_utc=now+self.settings.recall_s,
            preparation_id=preparation_id,state='queued')
        self._admit(candidate)
        return {'id':preparation_id,'state':'queued'}

    def _admit(self, candidate):
        with self.lock,self.app.render_lock:
            if self.pending[candidate.purpose] is not None:raise ReplayError('capacity_reached','Replay queue is full',429)
            if len(self.app.jobs)>=self.settings.jobs:raise ReplayError('capacity_reached','Replay job cap reached',429)
            self._reserve_asset()
            self._save(candidate)
            self.app.jobs[candidate.preparation_id]={'id':candidate.preparation_id,'state':'queued','slot':candidate.source.slot,
                'candidate_id':candidate.candidate_id,'reason':'Waiting for the shared replay worker','deadline_utc':candidate.deadline_utc,
                'admitted_utc':candidate.admitted_utc,'stages':dict.fromkeys((
                    'required_media_finalized_utc','last_included_frame_receipt_utc','receipt_uncertainty_ms',
                    'evidence_available_utc','model_start_utc','model_end_utc','plan_validated_utc','media_pinned_utc',
                    'render_start_utc','render_end_utc','output_validated_utc','asset_ready_utc',
                    'opportunity_reviewed_utc','proposal_accepted_utc','first_encoder_frame','last_encoder_frame',
                    'return_submitted_utc','return_applied_monotonic_s','viewer_replay_utc','viewer_return_utc'))}
            self.app.jobs[candidate.preparation_id]['stages']['candidate_admitted_utc']=candidate.admitted_utc
            self.pending[candidate.purpose]=candidate.candidate_id
        if candidate.purpose=='automatic':
            try:self.app.control.note_preparation(candidate)
            except ValueError:
                with self.lock:
                    self.pending['automatic']=None
                    self._update(candidate,state='failed',reason='capacity_reached: action history')
                    self.app.jobs[candidate.preparation_id].update(state='failed',error='capacity_reached: action history')
                raise

    def cancel(self, job_id):
        with self.lock:
            candidate=next((c for c in self.candidates.values() if c.preparation_id==job_id),None)
            if not candidate:return False
            if candidate.state not in ('queued','preparing','waiting'):raise ValueError('This preparation is no longer running')
            if self.pending[candidate.purpose]==candidate.candidate_id:self.pending[candidate.purpose]=None
            self._update(candidate,state='canceled',reason='Preparation canceled')
            self.app.jobs[job_id].update(state='canceled',canceled=True,error='Preparation canceled')
            if self.app.jobs[job_id].get('worker_started'):self.app.render_cancel.set()
            return True

    def _reserve_asset(self):
        with self.app.render_lock:
            usage=sum(r.path.stat().st_size+sum(map(len,r.frames)) for r in self.app.replays.values() if r.path.exists())
            while len(self.app.replays)>=self.settings.ready_assets or usage>=self.settings.ready_bytes:
                protected={t.replay_id for t in self.tickets.values()} | set(self.output_readers)
                if self.app.program.replay:protected.add(self.app.program.replay.id)
                victim=next((r for r in self.app.replays.values() if r.id not in protected),None)
                if victim is None:raise ReplayError('capacity_reached','All ready replay assets are in use',429)
                usage-=victim.path.stat().st_size+sum(map(len,victim.frames)) if victim.path.exists() else 0
                victim.path.unlink(missing_ok=True);victim.path.with_suffix('.json').unlink(missing_ok=True)
                self.app.replays.pop(victim.id,None)
                self.app.jobs[victim.id].update(available=False,reason='media_unavailable: ready asset evicted')

    def _prepare(self, candidate_id):
        candidate=self.candidates[candidate_id];job=self.app.jobs[candidate.preparation_id]
        canceled=lambda:self.app.stop.is_set() or job.get('canceled',False)
        try:
            check_budget(candidate.deadline_utc,canceled)
            if candidate.state=='canceled':return
            scene=self.scene(candidate.scene_id,candidate.scene_revision)
            snapshot=self.foundation.reviewed_snapshot()
            context=self.foundation.context('segmentor',scene.source,scene.native,snapshot,archive=True)
            context['target']['scene_id']=scene.scene_id
            reviewed_mappings={}
            initial_mapping=context['target'].pop('archive_mapping',None)
            if initial_mapping:reviewed_mappings[operation_key(scene.source.model_dump())]=initial_mapping
            if context['status']=='unavailable':raise ValueError('Segmentor context unavailable')
            with self.foundation.lock:
                associations={self.foundation._observation(eid).association_key for eid in scene.evidence_ids}
                peers=[SceneEvent.model_validate_json(r['body']) for r in self.foundation._records('scene',run=self.app.control.run_id)]
                peers=[p for p in peers if p.source!=scene.source and p.status!='retracted' and any(
                    self.foundation._observation(eid).association_key in associations-{None} for eid in p.evidence_ids)]
            selected=[scene]
            for peer in peers[:4]:
                other=self.foundation.context('segmentor',peer.source,peer.native,snapshot,archive=True)
                if other['status']=='unavailable':continue
                if len(context['observations'])+len(context['scenes'])+len(other['observations'])+len(other['scenes'])>self.foundation.settings.limits.context_records:break
                context['observations']+=other['observations'];context['scenes']+=other['scenes'];selected.append(peer)
                if other['target'].get('archive_mapping'):reviewed_mappings[operation_key(peer.source.model_dump())]=other['target']['archive_mapping']
            context['target']['archive_mappings']=reviewed_mappings
            deadline=candidate.deadline_utc
            # Segmentor uses observations plus bounded images from the retained interval.
            visual_chunks={m.chunk_id:m for selected_scene in selected
                for m in self.foundation.chunks(selected_scene.source,selected_scene.native)}
            if len(visual_chunks)>self.settings.input_chunks or sum(m.media.size for m in visual_chunks.values())>self.settings.input_bytes:
                raise ValueError('capacity_reached: combined segmentor media')
            context['target']['visual_windows']=[]
            job['stages']['model_start_utc']=time.time()
            # Automatic demo/workshop replay does not wait on W&B segmentor schema
            # success. Build a deterministic plan first; recall still uses the LLM.
            result=None
            scene_chunks=self.foundation.chunks(scene.source,scene.native)
            if candidate.purpose=='automatic' and not self.foundation.registry.gemini:
                result=deterministic_segment(context,scene,snapshot,chunks=scene_chunks)
                if result is not None:
                    job['stages']['deterministic_fallback_utc']=time.time()
            if result is None:
                # Keep under context_bytes. Multi-source windows and JPEG frames grow fast.
                frame_budget=max(2,min(12,24//len(selected)))
                for selected_scene in selected:
                    visual=self._visual_context(context,selected_scene,deadline,max_frames=frame_budget,canceled=canceled)
                    context['target']['visual_windows'].append({'source':selected_scene.source.model_dump(),
                        'frames':visual['target'].pop('visual_frames')})
                self._fit_segmentor_context(context)
                async def plan_call():
                    for attempt in range(self.settings.attempts):
                        job['model_attempts']=attempt+1
                        try:return await self.foundation.registry.llm('segmentor',context,snapshot,deadline)
                        except (ConnectionError,OSError) as error:
                            if isinstance(error,TimeoutError) or attempt+1>=self.settings.attempts:raise
                            check_budget(deadline)
                try:
                    result=SegmentorResult.model_validate(asyncio.run(plan_call()))
                except Exception as error:
                    failure=public_failure(error,'llm');job['provider_failure']=failure
                    if self.foundation.registry.gemini:raise
                    result=deterministic_segment(context,scene,snapshot,chunks=scene_chunks)
                    if result is None:raise
                    job['stages']['deterministic_fallback_utc']=time.time()
            job['stages']['model_end_utc']=time.time()
            mode=self.foundation.registry.require('llm').adapter
            if result.snapshot!=snapshot or result.origin!=('fixture' if mode=='fixture' else 'provider'):
                raise ValueError('Segmentor changed reviewed snapshot or origin')
            check_budget(deadline,canceled)
            intent=result.payload
            if isinstance(intent,Abstention):
                with self.lock:
                    check_budget(deadline,canceled)
                    self._update(candidate,state='skipped',reason=intent.reason);job.update(state='failed',error=intent.reason)
                return
            if isinstance(intent,SegmentWait):
                if intent.source!=scene.source or intent.required.end<=scene.native.end:raise ValueError('Unsupported wait interval')
                with self.lock:
                    check_budget(deadline,canceled)
                    self._update(candidate,state='waiting',reason=intent.reason)
                    job.update(state='queued',reason=intent.reason)
                return
            if intent.source!=scene.source:raise ValueError('Segmentor action source changed')
            reviewed={o['evidence_id'] for o in context['observations']}
            scenes_reviewed={(s['scene_id'],s['revision']) for s in context['scenes']}
            if any(eid not in reviewed for shot in intent.shots for eid in shot.evidence_ids) or any(
                (sid,revision) not in scenes_reviewed for shot in intent.shots for sid,revision in shot.scene_revisions.items()):
                raise ValueError('Segmentor referenced evidence outside reviewed context')
            if any(shot.mapping_revision is not None and
                reviewed_mappings.get(operation_key(shot.source.model_dump()),{}).get('revision')!=shot.mapping_revision for shot in intent.shots):
                raise ValueError('Segmentor mapping was not reviewed')
            # resolve_plan rejects expiry more than 60s ahead (pin window).
            pin_horizon=time.time()+min(60,self.foundation.settings.limits.pin_seconds)
            expiry=candidate.admitted_utc+self.settings.recall_expiry_s if candidate.purpose=='recall' else candidate.deadline_utc
            expiry=min(expiry,pin_horizon,deadline)
            plan=ReplayPlan12(plan_id=candidate.preparation_id,event_id=snapshot.event_id,run_id=snapshot.run_id,
                context_revision=snapshot.context_revision,configuration_revision=snapshot.configuration_revision,
                snapshot=snapshot,input_kind='archive',source=intent.source,action=intent.action,required=intent.required,
                shots=intent.shots,expires_at=expiry,selection_reason=intent.reason)
            chunks=self.foundation.chunks(scene.source,intent.required)
            job['stages']['required_media_finalized_utc']=max((m.finalized_utc for m in chunks),default=None)
            verified=[m for m in chunks if m.native.end==intent.required.end and
                m.last_receipt_utc is not None and m.receipt_basis=='frame-receipt']
            job['stages']['last_included_frame_receipt_utc']=max((m.last_receipt_utc for m in verified),default=None)
            job['stages']['receipt_uncertainty_ms']=max((m.receipt_uncertainty_ms for m in verified),default=None)
            if candidate.purpose=='automatic':
                if not chunks or any(m.last_receipt_utc is None or m.receipt_basis!='frame-receipt' for m in chunks):
                    raise ValueError('Unknown receipt allows explicit recall only')
                if not verified:raise ValueError('mapping_unknown: last included frame receipt; use explicit recall')
                final=max(m.finalized_utc for m in chunks)
                # Keep the admit-time budget. Receipt age may only shorten when it
                # still leaves enough time to render after finalization.
                receipt_bound=max(m.last_receipt_utc-m.receipt_uncertainty_ms/1000 for m in verified)+self.settings.candidate_s
                final_bound=final+self.settings.preparation_s
                if self.foundation.registry.gemini or min(receipt_bound,final_bound)>time.time()+5:
                    deadline=min(deadline,receipt_bound,final_bound)
                expiry=min(expiry,deadline)
                plan=plan.model_copy(update={'expires_at':expiry})
                job['stages']['required_media_finalized_utc']=final
            check_budget(deadline,canceled)
            if self.candidates[candidate_id].state=='canceled':return
            owner='render-'+candidate.preparation_id
            job['stages']['plan_validated_utc']=time.time()
            resolved=resolve_plan(self.app,plan,owner,deadline,canceled=canceled)
            job['stages']['media_pinned_utc']=time.time()
            try:
                with self.lock:
                    if self.candidates[candidate_id].state=='canceled' or self.app.stop.is_set():
                        raise ValueError('Preparation canceled')
                    self._update(candidate,state='preparing')
                    job.update(state='preparing',reason='Rendering validated retained media',worker_started=True)
                    self.app.render(resolved=resolved,replay_id=candidate.preparation_id)
            except BaseException:
                resolved.release();raise
        except Exception as error:
            with self.lock:
                if self.candidates[candidate_id].state!='canceled':
                    from pydantic import ValidationError
                    reason=('Invalid segmentor fields or values' if isinstance(error,ValidationError) else
                        'deadline_missed' if isinstance(error,TimeoutError) else str(error) if isinstance(error,ValueError) else type(error).__name__)
                    if job['stages'].get('model_start_utc') and not job['stages'].get('model_end_utc'):
                        failure=public_failure(error,'llm');job['provider_failure']=failure;reason=failure['reason']
                    self._update(candidate,state='failed',reason=reason[:256]);job.update(state='failed',error=reason)
        finally:
            with self.lock:self.planning=False

    def _fit_segmentor_context(self, context):
        """Drop newest visual frames until the segmentor context fits context_bytes."""
        limit=self.foundation.settings.limits.context_bytes
        windows=context.get('target',{}).get('visual_windows') or []
        while len(canonical(context).encode())>limit:
            donors=[window for window in windows if window.get('frames')]
            if not donors:
                raise ValueError('Segmentor context exceeds 64 KiB')
            # Prefer trimming the densest window so each source keeps some coverage.
            max(donors,key=lambda window:len(window['frames']))['frames'].pop()
        if windows and not any(window.get('frames') for window in windows):
            raise ValueError('Segmentor visual context exceeds 64 KiB')

    def _visual_context(self, context, scene, deadline, max_frames=24, canceled=None):
        """At most 24 timestamped inspected frames and ten source seconds."""
        import base64
        import io
        import av
        interval=Interval(start=max(scene.native.start,scene.native.end-round(10/float(Fraction(scene.source.time_base)))),end=scene.native.end)
        owner='segmentor-'+uuid.uuid4().hex
        try:
            resolution=self.foundation.resolve(scene.source,interval,owner=owner,
                deadline_utc=min(deadline,time.time()+min(60,self.foundation.settings.limits.pin_seconds)))
            if sum(c['manifest']['media']['size'] for c in resolution['chunks'])>self.settings.input_bytes:
                raise ValueError('capacity_reached: segmentor media')
            if any(c['manifest']['geometry']['native_width']*c['manifest']['geometry']['native_height']*12>self.settings.memory_bytes
                for c in resolution['chunks']):raise ValueError('capacity_reached: visual decode')
            samples=[];last=None
            step=(interval.end-interval.start)/max_frames
            for chunk in resolution['chunks']:
                manifest=chunk['manifest']
                with av.open(chunk['path']) as media:
                    if Fraction(media.streams.video[0].time_base)!=Fraction(scene.source.time_base):
                        raise ValueError('Native time base changed')
                    for frame in media.decode(video=0):
                        check_budget(deadline,canceled)
                        if frame.width*frame.height*12>self.settings.memory_bytes:raise ValueError('capacity_reached: visual decode')
                        if frame.pts is None or frame.duration<=0:raise ValueError('Frame timing is unknown')
                        pts=frame.pts+manifest['timeline_offset_pts']
                        if not interval.start<=pts<interval.end:continue
                        if last is None or pts-last>=step:
                            image=frame.to_image()
                            if manifest['geometry']['rotation']:image=image.rotate(-manifest['geometry']['rotation'],expand=True)
                            # Compact JPEG so multi-source contexts stay under context_bytes.
                            image.thumbnail((96,54))
                            encoded=io.BytesIO();image.save(encoded,format='JPEG',quality=45,optimize=True)
                            samples.append({'pts':pts,'time_base':scene.source.time_base,
                                'chunk_id':manifest['chunk_id'],'file_pts':frame.pts,'file_time_base':str(frame.time_base),
                                'image_base64':base64.b64encode(encoded.getvalue()).decode()});last=pts
                        if len(samples)>=max_frames:break
                if len(samples)>=max_frames:break
            context['target']['visual_frames']=samples
            limit=self.foundation.settings.limits.context_bytes
            while samples and len(canonical(context).encode())>limit:
                samples.pop();context['target']['visual_frames']=samples
            if len(canonical(context).encode())>limit:
                raise ValueError('Segmentor visual context exceeds 64 KiB')
            return context
        finally:self.foundation.release(owner)

    def eligible(self, replay, *, files=False):
        raw=replay.report['plan']
        if raw.get('schema_version')!='1.2':return False
        plan=ReplayPlan12.model_validate(raw)
        if (plan.event_id,plan.run_id)!=(self.foundation.settings.event.event_id,self.app.control.run_id):raise ValueError('wrong_run')
        if plan.context_revision!=self.foundation.context_revision or time.time()>=plan.expires_at:raise ValueError('deadline_missed')
        chunks=tuple(c for s in replay.report['source_map'] for c in s['retained_media'].get('chunk_ids',[]))
        if not published_dependencies(self.foundation,plan,chunks):raise ValueError('evidence_retracted or scene_changed or media_unavailable')
        if plan.input_kind=='live_buffer':
            for s in plan.shots:
                current=self.app.get_source(s.source.slot)
                if not current or (current.path,current.epoch)!=(s.source.source_id,s.source.epoch):raise ValueError('Live source lease changed')
        if not replay.frames or not replay.report.get('decode_passed'):raise ValueError('media_unavailable')
        if files:
            if not replay.path.is_file():raise ValueError('media_unavailable')
            verify_dependencies(self.foundation,plan)
            for shot in plan.shots:self.foundation.resolve(shot.source,shot.native,mapping_revision=shot.mapping_revision)
            with replay.path.open('rb') as data:
                if hashlib.file_digest(data,'sha256').hexdigest()!=replay.report['sha256']:raise ValueError('media_hash_changed')
        return True

    def preload(self, replay, owner, deadline=None):
        if replay.report['plan'].get('schema_version')!='1.2':return None
        plan=ReplayPlan12.model_validate(replay.report['plan'])
        if plan.input_kind!='archive':self.eligible(replay,files=False);return None
        deadline=min(deadline or plan.expires_at,plan.expires_at,time.time()+min(60,self.foundation.settings.limits.pin_seconds))
        # Pins cover validation, scheduling, actual replay duration and cleanup.
        pin_deadline=min(time.time()+min(60,self.foundation.settings.limits.pin_seconds),deadline+replay.duration+2)
        ids=[]
        try:
            verify_dependencies(self.foundation,plan)
            for shot in plan.shots:
                resolution=self.foundation.resolve(shot.source,shot.native,mapping_revision=shot.mapping_revision,
                    owner=owner,deadline_utc=pin_deadline)
                ids.extend(c['manifest']['chunk_id'] for c in resolution['chunks'])
            self.eligible(replay,files=True)
            # Fully preload checked output outside the controller/media locks.
            import av
            from media import jpeg
            frames=[]
            with av.open(str(replay.path)) as media:
                for frame in media.decode(video=0):
                    check_budget(deadline)
                    frames.append(jpeg(frame.to_image()))
                    if sum(map(len,frames))>self.settings.ready_bytes:raise ValueError('capacity_reached')
            if len(frames)!=replay.report['output_frames']:raise ValueError('media_unavailable')
            replay.frames=frames
            ticket=PlaybackTicket(owner,replay.id,plan,tuple(ids),pin_deadline,replay.report['sha256'])
            with self.lock:self.tickets[owner]=ticket
            return ticket
        except BaseException:
            self.foundation.release(owner);raise

    def ticket_valid(self, ticket):
        return (ticket is not None and time.time()<ticket.deadline and
            self.tickets.get(ticket.owner) is ticket and
            ticket.plan.run_id==self.app.control.run_id and ticket.plan.context_revision==self.foundation.context_revision and
            ticket.plan.configuration_revision==self.foundation.settings.configuration_revision and
            published_dependencies(self.foundation,ticket.plan,ticket.chunk_ids))

    def guard(self, owner):
        ticket=self.tickets.get(owner)
        return self.ticket_valid(ticket)

    def release_ticket(self, owner):
        with self.lock:ticket=self.tickets.pop(owner,None)
        if ticket:self.foundation.release(owner)

    def preview(self, replay):
        owner='preview-'+uuid.uuid4().hex
        with self.lock:self.output_readers[replay.id]=self.output_readers.get(replay.id,0)+1
        try:
            self.preload(replay,owner,deadline=min(time.time()+5,replay.report['plan'].get('expires_at',time.time()+5)))
            if replay.path.stat().st_size>self.settings.ready_bytes:raise ValueError('capacity_reached')
            return replay.path.read_bytes()
        finally:
            self.release_ticket(owner)
            with self.lock:
                self.output_readers[replay.id]-=1
                if not self.output_readers[replay.id]:self.output_readers.pop(replay.id)

    def ready(self):
        assets=[]
        for replay in tuple(self.app.replays.values()):
            try:
                if not self.eligible(replay):continue
                candidate=next((c for c in self.candidates.values() if c.preparation_id==replay.id),None)
                if not candidate or candidate.purpose!='automatic' or candidate.candidate_id in self.aired:continue
                if candidate.deadline_utc<=time.time():continue
                assets.append({'id':replay.id,'candidate_id':candidate.candidate_id,'scene_id':candidate.scene_id,
                    'scene_revision':candidate.scene_revision,'duration_s':replay.duration,'expires_at':candidate.deadline_utc,
                    'status':'ready'})
            except ValueError:continue
        return assets[:2]

    def mark_aired(self, replay_id):
        with self.lock:
            candidate=next((c for c in self.candidates.values() if c.preparation_id==replay_id and c.purpose=='automatic'),None)
            if candidate:self.aired.add(candidate.candidate_id)
        # Persistence is done by the replay coordinator, never the encoder.

    def _automatic_key(self, scene):
        with self.foundation.lock:
            links={self.foundation._observation(eid).association_key for eid in scene.evidence_ids}
        identity=('associated_action',next(iter(links))) if len(links)==1 and None not in links else ('source_scene',scene.scene_id)
        return operation_key(scene.source.event_id,scene.source.run_id,identity,'automatic')

    def _reconcile(self):
        for candidate in tuple(self.candidates.values()):
            job=self.app.jobs.get(candidate.preparation_id,{})
            if candidate.candidate_id in self.aired and candidate.state!='aired':self._update(candidate,state='aired',reason='First encoder frame applied')
            elif candidate.state=='preparing' and job.get('state') in ('ready','failed','canceled'):
                self._update(candidate,state=job['state'],reason=job.get('error','Checked replay ready')[:256])
            elif candidate.state in ('waiting','queued') and time.time()>=candidate.deadline_utc:
                if self.pending[candidate.purpose]==candidate.candidate_id:self.pending[candidate.purpose]=None
                self._update(candidate,state='skipped',reason='deadline_missed')
                if job:job.update(state='failed',error='deadline_missed')
            elif candidate.state=='waiting' and self.pending[candidate.purpose] is None:
                revision=self.foundation.scene_versions.get(candidate.scene_id,candidate.scene_revision)
                if revision>candidate.scene_revision:
                    try:self.scene(candidate.scene_id,revision)
                    except ReplayError:continue
                    with self.lock:
                        if self.candidates[candidate.candidate_id].state=='waiting' and self.pending[candidate.purpose] is None:
                            newer=self._update(candidate,scene_revision=revision,state='queued')
                            self.pending[candidate.purpose]=newer.candidate_id
                            if job:job.update(state='queued',reason='New reviewed aftermath evidence')
        with self.foundation.lock:
            scenes=[SceneEvent.model_validate_json(r['body']) for r in self.foundation._records('scene',run=self.app.control.run_id)]
        for scene in scenes:
            key=self._automatic_key(scene)
            old=self.candidates.get(key)
            # Failed/skipped segmentor work must not permanently block the scene.
            if old and old.state in ('failed','skipped') and time.time()-old.admitted_utc>=20:
                with self.lock:self.candidates.pop(key,None)
                old=None
            if old:
                continue
            if not self.settings.enabled or not self.app.control.program_started or self.app.control.crew_paused or not self.app.control.policy['replays_enabled']:continue
            if scene.status=='retracted' or len(self.app.jobs)>=self.settings.jobs or self.pending['automatic'] is not None:continue
            with self.foundation.lock:
                observations=[self.foundation._observation(eid) for eid in scene.evidence_ids]
            if not any(o.view and o.view.subject_visible and o.view.quality=='usable' for o in observations):continue
            now=time.time();deadline=now+self.settings.candidate_s
            try:
                chunks=self.foundation.chunks(scene.source,scene.native)
                if not chunks or any(m.receipt_basis!='frame-receipt' or m.last_receipt_utc is None for m in chunks):continue
                # Prefer the remaining candidate budget from admit time. Receipt age
                # only shortens when the receipt window is still ahead of now.
                receipt_deadline=max(m.last_receipt_utc-m.receipt_uncertainty_ms/1000 for m in chunks)+self.settings.candidate_s
                if self.foundation.registry.gemini or receipt_deadline>now:deadline=min(deadline,receipt_deadline)
                if deadline<=now:continue
                self._admit(ReplayCandidate(candidate_id=key,scene_id=scene.scene_id,scene_revision=scene.revision,
                    source=scene.source,purpose='automatic',admitted_utc=now,deadline_utc=deadline,
                    preparation_id=uuid.uuid4().hex,state='queued'))
            except ValueError as error:self.reason=str(error)

    def _run(self):
        while not self.app.stop.wait(.05):
            try:
                for note in self.foundation.notifications(after=self.cursor):self.cursor=max(self.cursor,note['sequence'])
                now=time.monotonic()
                if now-self.last_reconcile>=.5:
                    self._reconcile();self.foundation.save_cursor('replays',self.cursor);self.last_reconcile=now
                # Release only our own tickets after cancellation/completion/expiry.
                for owner,ticket in tuple(self.tickets.items()):
                    record=self.app.control.actions.get(owner.removeprefix('play-'))
                    if record and record['state'] in ('Rejected','Expired','Canceled','Finished') or not self.ticket_valid(ticket):
                        if self.app.program.replay_ticket is ticket:
                            self.app.control.invalidate_archive(ticket,'Archive dependency unavailable')
                        self.release_ticket(owner)
                with self.lock:
                    if self.planning or self.app.rendering:continue
                    purpose='recall' if self.pending['recall'] is not None else 'automatic'
                    candidate_id=self.pending[purpose]
                    if candidate_id is None:continue
                    candidate=self.candidates[candidate_id]
                    if candidate.state not in ('queued','waiting'):self.pending[purpose]=None;continue
                    if purpose=='automatic' and (self.app.control.crew_paused or not self.app.control.policy['replays_enabled']):continue
                    self.pending[purpose]=None;self.planning=True
                work=lambda candidate_id=candidate_id:self._prepare(candidate_id)
                self.foundation.submit_role('segmentor',work)
            except Exception as error:self.reason='Replay coordinator: '+type(error).__name__
        for owner in tuple(self.tickets):self.release_ticket(owner)
