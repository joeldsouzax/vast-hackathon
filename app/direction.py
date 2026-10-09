"""Bounded director and commentator work on the one application foundation."""
from __future__ import annotations
import asyncio
from collections import deque
from dataclasses import replace
from fractions import Fraction
import json
import math
import threading
import time
import uuid
from pydantic import TypeAdapter
from foundation_records import (Abstention, DirectorIntent, CommentatorIntent, EventContext, Interval,
    DecisionSnapshot, ProgramText)
from direction_media import PreparedCue, caption_layer, decode_speech

DIRECTOR=TypeAdapter(DirectorIntent)
COMMENTATOR=TypeAdapter(CommentatorIntent)


class Direction:
    def __init__(self, app):
        self.app=app;self.foundation=app.foundation;self.settings=self.foundation.settings.direction
        if self.settings.enabled and ((app.cfg.width,app.cfg.height,app.cfg.fps)!=(640,360,15) or
                not 0<=app.cfg.delay<=10 or app.cfg.buffer_seconds<app.cfg.delay+self.settings.speech_max_s+1):
            raise ValueError('Direction requires the tested 640×360/15 fps output and sufficient delay buffer')
        app.program.direction_settings=self.settings
        app.program.mixer.settings=self.settings
        self.lock=threading.RLock();self.prepared={};self.dependencies={}
        self.state={'director':'Inactive','commentator':'Inactive','speech':'Inactive'}
        self.reason='Awaiting fresh mapped evidence' if self.settings.enabled else 'Direction is disabled in server configuration'
        self.cursor=0;self.last_trigger=None;self.traces=deque(maxlen=256)
        self.thread=threading.Thread(target=self._run,name='crew-coordinator',daemon=True)

    def start(self):
        self.thread.start()

    def status(self):
        with self.lock:
            package=self.app.program.graphics.event_package
            capabilities=self.foundation.registry.capabilities()
            reason=self.reason
            if self.settings.enabled and not capabilities['llm']['ready']:reason='LLM unavailable: '+capabilities['llm']['reason']
            return {'enabled':self.settings.enabled,'roles':dict(self.state),'reason':reason,
                'fixture':capabilities['llm']['adapter']=='fixture',
                'prepared_assets':len(self.prepared),'prepared_bytes':sum(c.memory_bytes for c in self.prepared.values()),
                'setup':{**package['manifest'],'ready':package['manifest']['context_revision']==self.foundation.context_revision} if package else {'ready':False},
                'provider_capabilities':{k:v for k,v in capabilities.items() if k in ('llm','speech')},
                'traces':list(self.traces),'limits':self.settings.model_dump()}

    def expected(self, snapshot, args):
        slots={args.get('slot',snapshot.runtime.program.primary_slot)}
        return {'run_id':snapshot.run_id,'context_revision':snapshot.context_revision,
            'control_revision':snapshot.control_revision,'program_revision':snapshot.program_revision,
            'sources':[{'slot':s.slot,'source_path':s.source_id,'epoch':s.epoch} for s in sorted(snapshot.sources,key=lambda s:s.slot) if s.slot in slots]}

    def check(self, dependencies, *, program=True):
        """Published revisions and immutable identities only; safe inside controller locks."""
        snapshot=dependencies['snapshot'];app=self.app
        if (snapshot.run_id!=app.control.run_id or snapshot.context_revision!=self.foundation.context_revision or
            snapshot.configuration_revision!=self.foundation.settings.configuration_revision or
            snapshot.control_revision!=app.control.revision or app.control.crew_paused or
            app.control.rehearsal['state']=='Running' or app.stop.is_set()):
            raise ValueError('Reviewed context or control changed')
        if program and snapshot.program_revision!=app.program.revision:raise ValueError('Reviewed program cue changed')
        if time.time()>=dependencies['deadline']:raise ValueError('Original decision deadline expired')
        if self.foundation.invalidated_evidence.intersection(dependencies['evidence_ids']):raise ValueError('Reviewed evidence was corrected')
        if dependencies.get('archive_session'):
            session=dependencies['archive_session'];program_state=app.program
            if (program_state.replay_revision!=session['revision'] or not program_state.replay or
                program_state.replay.id!=session['asset_id'] or not app.replay_work.ticket_valid(program_state.replay_ticket)):
                raise ValueError('Accepted archive session changed')
            target=program_state.actual_target
            native=target.get('native') or {}
            if target.get('archive_source')!=session['source'] or target.get('source_path')!=session['source']['source_id'] or native.get('native_pts',-1)<session['reviewed_pts']:
                raise ValueError('Archive source interval moved backward or changed')
        for source_id,(slot,epoch,revision) in dependencies['sources'].items():
            current=app.get_source(slot)
            if not current or current.path!=source_id or current.epoch!=epoch or getattr(current,'timeline_revision',1)!=revision:
                raise ValueError('Reviewed source lease, epoch, or mapping changed')
            mapping=self.foundation.current_mappings.get(source_id)
            if (mapping.revision if mapping else None)!=snapshot.mapping_revisions.get(source_id):
                raise ValueError('Reviewed event mapping changed')
        return True

    def validate(self, result, role, context, deadline):
        snapshot=DecisionSnapshot.model_validate(context['snapshot'])
        if result.snapshot!=snapshot:raise ValueError('LLM result does not match reviewed snapshot')
        mode=self.foundation.registry.capabilities()['llm']['adapter']
        if result.origin!=('fixture' if mode=='fixture' else 'provider'):raise ValueError('LLM origin differs from trusted adapter')
        if role=='director':
            runtime=snapshot.runtime
            if not runtime or time.time()-runtime.sampled_utc>self.settings.role_timeout_s:
                raise ValueError('Reviewed runtime is unavailable or stale')
        intent=(DIRECTOR if role=='director' else COMMENTATOR).validate_json(result.text)
        if isinstance(intent,Abstention):return intent,None
        refs=intent.evidence_ids
        allowed={o['evidence_id']:o for o in context['observations']}
        if any(eid not in allowed for eid in refs):raise ValueError('Intent references evidence outside reviewed context')
        if role=='commentator':
            # Empty references permit only the exact fixture disclosure. Live claims need evidence.
            speech=self.foundation.registry.labels.get('speech',{})
            disclosures=[speech.get('text')]+[v['text'] for v in speech.get('variants',[])]
            if not refs and not (result.origin=='fixture' and intent.text in disclosures):
                raise ValueError('Commentary requires reviewed evidence')
            if any(row['text']==intent.text for row in context['aired']+context['pending']):
                raise ValueError('Commentary repeats pending or delivered text')
        if intent.op=='crop':
            if not intent.rect or not refs or not any(d['confidence']>=.6 for eid in refs for d in allowed[eid]['detections']):
                raise ValueError('Live crop requires geometry and subject evidence')
        dependencies={'snapshot':snapshot,'evidence_ids':refs,'deadline':deadline,'sources':{}}
        archive_session=role=='commentator' and context['target'].get('archive_session')
        if archive_session:dependencies['archive_session']=context['target']['archive_session']
        for source in (() if archive_session else snapshot.sources):
            current=self.app.get_source(source.slot)
            if current and current.path==source.source_id:
                reviewed=snapshot.decoder_revisions.get(source.source_id)
                if reviewed is None:raise ValueError('Reviewed decoder mapping is unavailable')
                dependencies['sources'][source.source_id]=(source.slot,source.epoch,reviewed)
        if refs:
            with self.foundation.lock:
                for eid in refs:
                    observation=self.foundation._observation(eid)
                    if observation.model_dump(mode='json')!=allowed[eid]:raise ValueError('Evidence changed after review')
                    job=self.foundation.db.execute('SELECT deadline,body FROM jobs WHERE key=?',(observation.job_key,)).fetchone()
                    if role=='director':
                        if not job or json.loads(job['body']).get('deadline_basis')!='frame-receipt':
                            raise ValueError('Notification-only evidence has no live freshness')
                        dependencies['deadline']=min(dependencies['deadline'],job['deadline'])
        self.check(dependencies)
        if intent.op=='replay':
            reviewed=next((r for r in context.get('ready_replays',[]) if r['id']==intent.replay_id),None)
            if not reviewed:raise ValueError('Replay was not in the reviewed ready assets')
            if snapshot.runtime.program.actual!='LIVE':raise ValueError('Replay needs eligible current live return')
            if not any(allowed[eid].get('replay_opportunity') for eid in refs):raise ValueError('No positive replay opportunity evidence')
            if context['event'].get('profile')=='stage':
                policy=context['event'].get('audio_policy',{})
                try:start,end=int(policy['recap_start_event_ms']),int(policy['recap_end_event_ms'])
                except (KeyError,ValueError,TypeError):raise ValueError('Stage replay needs an explicitly supplied recap interval')
                point=snapshot.runtime.program.actual_target.get('event_ms')
                if point is None or not start<=point<end or not any(allowed[eid].get('replay_opportunity')=='recap' for eid in refs):
                    raise ValueError('Stage recap opportunity is unavailable')
                dependencies['deadline']=min(dependencies['deadline'],time.time()+(end-point)/1000)
            if not any(r['id']==intent.replay_id for r in self.app.replay_work.ready()):raise ValueError('Replay opportunity closed')
            dependencies['deadline']=min(dependencies['deadline'],reviewed['expires_at'])
        if intent.op=='urgent_return':
            if snapshot.runtime.program.actual!='REPLAY' or not any(allowed[eid].get('urgent_live') for eid in refs):
                raise ValueError('Urgent return requires fresh positive live evidence during replay')
            source=next((s for s in snapshot.sources if s.slot==intent.slot),None)
            mapping=self.foundation.current_mappings.get(source.source_id) if source else None
            if not mapping or snapshot.mapping_revisions.get(source.source_id)!=mapping.revision:
                raise ValueError('mapping_unknown: urgent return timing')
            current=self.app.get_source(intent.slot)
            delayed=current.at(time.monotonic()-self.app.cfg.delay) if current else None
            native=delayed.native_provenance if delayed else None
            if not native:raise ValueError('mapping_unknown: delayed-live position')
            delayed_pts=round(native['native_pts']*float(Fraction(native['native_time_base'])/Fraction(source.time_base)))
            starts=[allowed[eid]['native']['start'] for eid in refs if allowed[eid]['source']==source.model_dump()]
            if not starts:raise ValueError('Urgent evidence refers to another source')
            health=next((h for h in snapshot.runtime.source_health if h.source_id==source.source_id and h.epoch==source.epoch),None)
            if not health or not health.buffer_ready or health.last_frame_age_s is None or health.last_frame_age_s>.75:
                raise ValueError('Urgent return source is unavailable')
            start=min(starts)
            if not mapping.valid.start<=min(start,delayed_pts)<max(start,delayed_pts)+1<=mapping.valid.end:
                raise ValueError('mapping_invalid: urgent return interval')
            wait=max(0,(start-delayed_pts)*float(Fraction(source.time_base)))
            dependencies.update(urgent_return=True,return_not_before=time.monotonic()+wait)
        if role=='director' and intent.op in ('live','audio'):
            health=next((h for h in snapshot.runtime.source_health if h.slot==intent.slot),None)
            if not health or not health.buffer_ready or health.last_frame_age_s is None or health.last_frame_age_s>.75:
                raise ValueError('Reviewed camera health is stale or unavailable')
            if intent.op=='audio' and not health.has_audio:raise ValueError('Reviewed microphone is unavailable')
        return intent,dependencies

    def dispatch(self, result, role, context, deadline, *, action_id=None):
        intent,dependencies=self.validate(result,role,context,deadline)
        if isinstance(intent,Abstention):
            self.state[role]='Holding';self.reason=intent.reason;return None
        if role=='commentator':
            snapshot=dependencies['snapshot']
            reservation=ProgramText(cue_id=(action_id or uuid.uuid4().hex)+'-intent',event_id=snapshot.event_id,
                run_id=snapshot.run_id,text=intent.text,state='pending',event_ms=context['target'].get('event_ms'),
                program_revision=snapshot.program_revision,evidence_ids=intent.evidence_ids,origin='controller',
                channel='intent',session_id=f'{snapshot.run_id}:{snapshot.program_revision}')
            self.foundation.program_text(reservation,owner='controller',reserve=True)
            def prepare():self._prepare_speech(intent,dependencies,context,reservation)
            prepare.cancel=lambda reason:self._end_reservation(reservation,reason)
            try:self.foundation.submit_role('speech',prepare)
            except Exception:
                prepare.cancel('Preparation could not be queued');raise
            return None
        snapshot=dependencies['snapshot'];op=intent.op
        args={};prepared=None
        if op in ('live','audio'):
            args={'slot':intent.slot,**({'independent':intent.independent} if op=='live' else {'muted':intent.muted})}
            if op=='live' and snapshot.runtime.program.requested=='LIVE' and snapshot.runtime.program.primary_slot==intent.slot:
                self.state[role]='Holding';return None
            if op=='audio' and snapshot.runtime.program.audio_slot==intent.slot and snapshot.runtime.program.audio_muted==intent.muted:
                reviewed=next((s for s in snapshot.sources if s.slot==intent.slot),None)
                if reviewed and snapshot.runtime.program.audio_source_path==reviewed.source_id:
                    self.state[role]='Holding';return None
        elif op=='replay':args={'replay_id':intent.replay_id}
        elif op=='urgent_return':op='live';args={'slot':intent.slot,'independent':True}
        elif op=='return_live':
            if snapshot.runtime.program.requested=='LIVE':
                self.state[role]='Holding';return None
            op='live'
        elif op=='holding' and snapshot.runtime.program.requested=='HOLDING':
            self.state[role]='Holding';return None
        elif op=='reset_crop' and self.app.program.framing is None:
            self.state[role]='Holding';return None
        elif op=='crop':args={'rect':intent.rect.model_dump(),'geometry_revision':snapshot.decoder_revisions.get(snapshot.runtime.program.primary_source_path)}
        elif op=='graphics':
            package=self.app.program.graphics.event_package
            if not package or package['manifest']['context_revision']!=snapshot.context_revision:
                raise ValueError('Event graphics package is not ready for reviewed context')
            prepared=self.app.program.graphics.prepared_graphic(intent.preset,intent.duration_s)
            args={'graphics':{'op':'cue','preset':intent.preset,'title':prepared.title,'subtitle':prepared.subtitle,'duration_s':intent.duration_s}}
        aid=action_id or uuid.uuid4().hex
        request={'id':aid,'op':op,'args':args,'expected':self.expected(snapshot,args),'expires_at':dependencies['deadline']}
        return self.app.control.propose(request,actor='Provider crew',dependencies=dependencies)

    def _end_reservation(self, reservation, reason):
        self.foundation.program_text(reservation.model_copy(update={'state':'canceled','reason':reason}),owner='controller')

    def _prepare_speech(self, intent, dependencies, context, reservation):
        cue=None;asset=None
        try:
            self.state['speech']='Preparing';self.check(dependencies)
            with self.lock:
                pending=[c for c in self.prepared.values() if c is not self.app.program.cue]
            for older in pending:self.discard(older.id,'Replaced by the newest eligible commentary')
            if pending:self.drain_receipts()
            with self.lock:
                if len(self.prepared)>=2 or sum(c.memory_bytes for c in self.prepared.values())+self.settings.speech_asset_bytes>self.settings.speech_total_bytes:
                    raise ValueError('Speech preparation cannot reserve its bounded buffer')
            event=EventContext.model_validate(context['event'])
            pcm=b'';asset=None;fallback=None
            try:
                async def prepare():
                    async with asyncio.timeout(min(self.settings.role_timeout_s,dependencies['deadline']-time.time())):
                        return await self.foundation.registry.speech(intent.text,self.foundation.storage,dependencies['deadline'],event_context=event)
                speech=asyncio.run(prepare())
                asset=speech.media
                pcm=decode_speech(speech,self.foundation.storage,intent.text,event,self.foundation.settings)
            except Exception as error:
                fallback='Speech unavailable: '+type(error).__name__
            self.check(dependencies)
            layer=None
            try:layer=caption_layer(self.app.program.graphics,intent.text)
            except ValueError:
                if not pcm:raise
            with self.lock:
                usage=sum(c.memory_bytes for c in self.prepared.values())
                size=len(pcm)+(asset.size if asset else 0)+(layer.width*layer.height*4 if layer else 0)
                if size>self.settings.speech_asset_bytes or len(self.prepared)>=2 or usage+size>self.settings.speech_total_bytes:
                    raise ValueError('Prepared speech storage limit reached')
            snapshot=dependencies['snapshot'];program=self.app.program
            with program.lock:
                self.check(dependencies)
                start=program.frames_written+2
                remaining=min(self.settings.speech_max_s,dependencies['deadline']-time.time()-2/self.app.cfg.fps)
                frames=math.floor(remaining*self.app.cfg.fps)
                if program.requested=='REPLAY' and program.replay:
                    frames=min(frames,len(program.replay.frames)-program.replay_index-2)
                if frames<=0:raise ValueError('Commentary window expired before preparation')
                if pcm and math.ceil(len(pcm)/self.app.cfg.audio_size)>frames:
                    pcm=b'';fallback='Speech cannot fit its original window'
                if not pcm and layer is None:raise ValueError('No eligible commentary media')
                target={key:value for key,value in program.actual_target.items() if key in ('kind','source_path','epoch','id','command_revision')}
                aid=reservation.cue_id.removesuffix('-intent')
                session_revision=dependencies.get('archive_session',{}).get('revision',snapshot.program_revision)
                cue=PreparedCue(aid,f'{snapshot.run_id}:{session_revision}',intent.text,pcm,layer,start,start+frames,
                    dependencies['deadline'],snapshot.program_revision,target,
                    lambda:self._guard(dependencies),intent.evidence_ids,context['target'].get('event_ms'),asset)
            with self.lock:self.prepared[cue.id]=cue
            self._history(cue,'prepared')
            self._history(cue,'pending')
            record=self.app.control.propose({'id':cue.id,'op':'commentary','args':{'cue_id':cue.id},
                'expected':self.expected(snapshot,{}),'expires_at':dependencies['deadline']},
                actor='Provider crew',dependencies=dependencies)
            if record['state'] not in ('Scheduled','Applying'):
                self.discard(cue.id,record['reason']);self.drain_receipts()
                raise ValueError('Prepared commentary was rejected')
            self.state['speech']='Prepared';self.reason=fallback or 'Prepared speech and captions'
            self.traces.append({'cue_id':cue.id,'stage':'speech-ready','utc':time.time(),'samples':len(pcm)//2,'fallback':fallback})
        except Exception as error:
            self.state['speech']='Unavailable';self.reason=str(error) if isinstance(error,ValueError) else type(error).__name__
            if cue:self.discard(cue.id,'Preparation failed')
            elif asset:
                with self.lock:
                    if not any(c.asset and c.asset.key==asset.key for c in self.prepared.values()):self.foundation.storage.delete(asset)
        finally:
            self._end_reservation(reservation,'Preparation transferred to media channels' if cue else 'Preparation failed or expired')

    def _guard(self, dependencies):
        try:return self.check(dependencies)
        except ValueError:return False

    def _history(self, cue, state, channel=None, receipt=None, reason=None):
        channels=[channel] if channel else (['speech'] if cue.pcm else [])+(['caption'] if cue.caption is not None else [])
        for kind in channels:
            r=receipt or {};first=r.get('first');last=r.get('last')
            self.foundation.program_text(ProgramText(cue_id=cue.id+'-'+kind,event_id=self.foundation.settings.event.event_id,
                run_id=self.app.control.run_id,text=cue.text,state=state,event_ms=cue.event_ms,
                program_revision=cue.program_revision,evidence_ids=cue.evidence_ids,origin='controller',channel=kind,
                session_id=cue.session_id,first_program_ms=round(first/(48 if kind=='speech' else self.app.cfg.fps/1000)) if first is not None else None,
                last_program_ms=round(last/(48 if kind=='speech' else self.app.cfg.fps/1000)) if last is not None else None,
                first_sample=first if kind=='speech' else None,last_sample=last if kind=='speech' else None,reason=reason),owner='controller')

    def drain_receipts(self):
        with self.app.program.lock:
            receipts=list(self.app.program.cue_receipts);self.app.program.cue_receipts.clear()
        for receipt in receipts:
            cue=receipt['cue']
            try:self._history(cue,receipt['state'],receipt['channel'],receipt,receipt['reason'])
            except ValueError as error:self.reason='Cue history failure: '+str(error)
            self.app.log('commentary_delivery',cue_id=cue.id,session_id=cue.session_id,channel=receipt['channel'],
                state=receipt['state'],first=receipt.get('first'),last=receipt.get('last'),reason=receipt['reason'])
        with self.lock:
            for cue in tuple(self.prepared.values()):
                channels=(['speech'] if cue.pcm else [])+(['caption'] if cue.caption is not None else [])
                if all(cue.delivered.get(kind,{}).get('terminal') for kind in channels):self._release(cue)

    def _release(self, cue):
        self.app.control.finish_commentary(cue)
        self.prepared.pop(cue.id,None)
        if cue.asset and not any(c.asset and c.asset.key==cue.asset.key for c in self.prepared.values()):
            self.foundation.storage.delete(cue.asset)

    def discard(self, cue_id, reason):
        with self.lock:cue=self.prepared.get(cue_id)
        if not cue:return
        with self.app.program.lock:
            if self.app.program.cue is cue:self.app.program.cancel_commentary(reason)
            else:
                cue.canceled=True
                for channel in (['speech'] if cue.pcm else [])+(['caption'] if cue.caption is not None else []):
                    self.app.program._cue_receipt(cue,channel,'canceled',reason=reason)

    def _context(self, role, source, snapshot):
        native=(self.app.program.actual_target.get('native') or {}) if role=='commentator' else None
        if role=='director':
            current=self.app.get_source(source.slot)
            with current.lock:native=current.frames[-1].native_provenance if current.frames else None
        if not native:raise ValueError('Source/program native mapping is unavailable')
        point=round(native['native_pts']*float(Fraction(native['native_time_base'])/Fraction(source.time_base)))
        uncertainty=math.ceil(native.get('uncertainty_ms',1000/self.app.cfg.fps)/1000/float(Fraction(source.time_base))) if role=='commentator' else 0
        interval=Interval(start=point-round(self.foundation.settings.limits.context_seconds/float(Fraction(source.time_base))),end=point+1-uncertainty)
        event_ms=self.app.program.actual_target.get('event_ms')
        archive=role=='commentator' and self.app.program.actual=='REPLAY' and self.app.program.replay_ticket is not None
        context=self.foundation.context(role,source,interval,snapshot,event_ms=round(event_ms) if event_ms is not None else None,archive=archive)
        if archive:
            context['facts']={}
            context['event']['participants']=[]
            context['target']['archive_session']={'asset_id':self.app.program.replay.id,'revision':self.app.program.replay_revision,
                'source':source.model_dump(mode='json'),'reviewed_pts':point,'output_frame':self.app.program.actual_target.get('output_frame')}
        elif role=='director':context['ready_replays']=self.app.replay_work.ready()
        return context

    def _decide(self, role):
        try:
            if self.app.control.crew_paused or self.app.control.rehearsal['state']=='Running':return
            snapshot=self.foundation.reviewed_snapshot()
            target=snapshot.runtime.program.actual_target if snapshot.runtime else {}
            source=next((s for s in snapshot.sources if s.source_id==target.get('source_path',snapshot.runtime.program.primary_source_path)),None) if snapshot.runtime else None
            if role=='director' and source is None:source=next(iter(snapshot.sources),None)
            if role=='director' and self.app.program.actual=='REPLAY':
                source=next((s for s in snapshot.sources if s.source_id==snapshot.runtime.program.primary_source_path),None) or next(iter(snapshot.sources),None)
            if role=='commentator' and target.get('archive_source'):
                from foundation_records import SourceEpoch
                source=SourceEpoch.model_validate(target['archive_source'])
            if not source:raise ValueError('Reviewed source is unavailable')
            context=self._context(role,source,snapshot)
            if context['status']=='unavailable' or not context['observations']:return
            deadline=time.time()+self.settings.role_timeout_s
            if role=='director':
                with self.foundation.lock:
                    deadlines=[self.foundation.db.execute('SELECT deadline FROM jobs WHERE key=?',(o['job_key'],)).fetchone()[0] for o in context['observations']]
                deadline=min(deadline,max(deadlines))
            elif self.app.program.actual=='LIVE':
                with self.foundation.lock:
                    jobs=[self.foundation.db.execute('SELECT deadline,body FROM jobs WHERE key=?',(o['job_key'],)).fetchone() for o in context['observations']]
                fresh=[job['deadline'] for job in jobs if job and json.loads(job['body']).get('deadline_basis')=='frame-receipt' and job['deadline']>time.time()]
                if not fresh:return
                deadline=min(deadline,max(fresh))
            self.state[role]='Thinking'
            self.traces.append({'role':role,'stage':'model-start','utc':time.time(),'deadline_utc':deadline})
            async def call():
                for attempt in range(self.settings.attempts):
                    try:return await self.foundation.registry.llm(role,context,snapshot,deadline)
                    except (ConnectionError,OSError) as error:
                        if isinstance(error,TimeoutError) or attempt+1==self.settings.attempts:raise
            result=asyncio.run(call())
            self.traces.append({'role':role,'stage':'model-end','utc':time.time(),'model_id':result.model_id,'version':result.model_version,'origin':result.origin})
            self.dispatch(result,role,context,deadline)
        except Exception as error:
            self.state[role]='Unavailable';self.reason=type(error).__name__

    def _run(self):
        while not self.app.stop.wait(.05):
            try:
                self.drain_receipts()
                notes=self.foundation.notifications(after=self.cursor)
                for note in notes:self.cursor=max(self.cursor,note['sequence'])
                with self.lock:
                    for cue in tuple(self.prepared.values()):
                        record=self.app.control.actions.get(cue.id)
                        if record and record['state'] in ('Rejected','Canceled','Expired'):self.discard(cue.id,record['reason'])
                if not self.settings.enabled or self.app.control.crew_paused or self.app.control.rehearsal['state']=='Running':continue
                if not self.app.control.program_started:continue
                snapshot=self.foundation.reviewed_snapshot()
                trigger=(snapshot.evidence_revision,snapshot.program_revision,snapshot.control_revision,
                         tuple((h.source_id,h.epoch,h.buffer_ready) for h in snapshot.runtime.source_health) if snapshot.runtime else ())
                cadence=int(time.monotonic()) if self.app.program.actual=='REPLAY' else None
                trigger=(*trigger,cadence,tuple(r['id'] for r in self.app.replay_work.ready()))
                if trigger==self.last_trigger:continue
                self.last_trigger=trigger
                roles=('director',) if self.app.program.actual=='HOLDING' or self.app.program.requested=='HOLDING' else ('director','commentator')
                for role in roles:
                    self.foundation.submit_role(role,lambda role=role:self._decide(role))
            except Exception as error:self.reason='Crew coordinator: '+type(error).__name__
        self.app.program.cancel_commentary('Studio stopped')
        for cue in tuple(self.prepared.values()):self.discard(cue.id,'Studio stopped')
        self.drain_receipts()
