"""Bounded director and commentator work on the one application foundation."""
from __future__ import annotations
import asyncio
from collections import deque
from dataclasses import replace
from fractions import Fraction
import json
import math
import os
import threading
import time
import uuid
from pydantic import TypeAdapter
from foundation_records import (Abstention, DirectorIntent, CommentatorIntent, EventContext, Interval,
    DecisionSnapshot, ProgramText)
from direction_media import PreparedCue, caption_layer, decode_speech
from provider_errors import public_failure

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
        self.provider_failures={}
        self.cursor=0;self.last_trigger=None;self.last_commentary=None;self.traces=deque(maxlen=256)
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
                'provider_failures':dict(self.provider_failures),
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
        camera=dependencies.get('commentary_camera')
        microphone=dependencies.get('microphone')
        if microphone:
            if (app.program.audio_source_path!=microphone['source_path'] or
                    app.program.audio_muted!=microphone['muted'] or
                    app.program.audio_epoch!=microphone['epoch']):
                raise ValueError('Reviewed microphone changed')
        if camera:
            # Captions stay valid across graphics or other program changes while
            # the same camera feed remains on air.
            target=app.program.actual_target
            if (app.program.requested!='LIVE' or target.get('kind')!='camera' or
                    target.get('source_path')!=camera['source_path'] or target.get('epoch')!=camera['epoch']):
                raise ValueError('Commentary camera changed')
        elif program and not dependencies.get('archive_session') and snapshot.program_revision!=app.program.revision:raise ValueError('Reviewed program cue changed')
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
        for source_id,(slot,epoch,revision) in ({} if camera and not self.foundation.registry.gemini else dependencies['sources']).items():
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
        if role=='commentator' and refs and mode=='live' and not self.foundation.registry.gemini:
            # Resolve truncated IDs by unique prefix, then drop invented IDs.
            # The line must still rest on at least one reviewed observation.
            resolved=[]
            for eid in refs:
                matches=[a for a in allowed if a==eid] or ([a for a in allowed if a.startswith(eid[:12])] if len(eid)>=12 else [])
                if len(matches)==1 and matches[0] not in resolved:resolved.append(matches[0])
            if not resolved and context['observations']:
                # Small models often echo the schema placeholder; cite the newest reviewed observation.
                resolved=[context['observations'][0]['evidence_id']]
            intent=intent.model_copy(update={'evidence_ids':resolved})
            refs=intent.evidence_ids
        if any(eid not in allowed for eid in refs):
            raise ValueError('Intent references evidence outside reviewed context: '+','.join(e[:16] for e in refs if e not in allowed)[:120])
        if role=='commentator':
            if intent.speaker not in self._speaker_options(context):
                raise ValueError('The lead commentator must carry the next turn')
            if intent.delivery=='source_caption' and intent.speaker!='lead':
                raise ValueError('An attendee quote is not a co-commentator turn')
            # Empty references permit only the exact fixture disclosure. Live claims need evidence.
            speech=self.foundation.registry.labels.get('speech',{})
            disclosures=[speech.get('text')]+[v['text'] for v in speech.get('variants',[])]
            event_bridge=(self.foundation.registry.gemini and intent.basis=='event_context' and
                bool(context['event'].get('title')) and not context['target'].get('archive_session'))
            if intent.basis=='event_context' and not event_bridge:
                raise ValueError('Event commentary requires the current event brief and live view')
            if event_bridge and refs:
                raise ValueError('Event commentary must not claim camera evidence')
            if not refs and not event_bridge and not (result.origin=='fixture' and intent.text in disclosures):
                raise ValueError('Commentary requires reviewed evidence')
            if any(row['text'] in (intent.text,'Heard: '+intent.text) for row in context['aired']+context['pending']):
                raise ValueError('Commentary repeats pending or delivered text')
            microphone=context['target'].get('microphone',{})
            heard=[o for o in context['observations'] if o.get('audio') and
                o['source']['source_id']==microphone.get('source_path') and
                o['source']['epoch']==microphone.get('epoch') and not microphone.get('muted',True)]
            if intent.delivery=='source_caption':
                quotes=[allowed[eid] for eid in refs if eid in {o['evidence_id'] for o in heard}]
                if (intent.basis!='action' or len(refs)!=1 or len(quotes)!=1 or not intent.text.strip() or len(intent.text)>96 or
                        intent.text not in quotes[0]['audio']['transcript']):
                    raise ValueError('Source captions require an exact excerpt from the selected microphone')
        if intent.op=='graphics' and self.foundation.registry.gemini:
            if intent.preset not in {g['id'] for g in context.get('prepared_graphics',[])}:
                raise ValueError('Automatic overlay requires reviewed evidence and an eligible prepared purpose')
            if any(cue.prepared.title==intent.title for cue in self.app.program.graphics.active.values()):
                raise ValueError('Action label is already visible')
            entry=next(g for g in context['prepared_graphics'] if g['id']==intent.preset)
            if not refs and (entry['text_binding']!='prepared' or entry['slot']=='stinger'):
                raise ValueError('Action overlays require reviewed evidence')
            if entry['slot']=='stinger' and (intent.duration_s>1 or self._transition_unavailable()):
                raise ValueError('Scene transitions need a live view, a short duration and spacing')
            spoken=[allowed[eid] for eid in refs if allowed[eid].get('audio')]
            if spoken and entry['slot']!='stinger':
                microphone=context['target'].get('microphone',{})
                if (len(refs)!=1 or not spoken[0]['audio'].get('meaning') or intent.subtitle!='Heard meaning' or
                        intent.title!=spoken[0]['audio']['meaning'] or
                        intent.preset not in ('headline','wide-banner','lower-split') or microphone.get('muted',True) or
                        spoken[0]['source']['source_id']!=microphone.get('source_path') or
                        spoken[0]['source']['epoch']!=microphone.get('epoch')):
                    raise ValueError('Speech graphics require the selected microphone meaning, labeled as a paraphrase')
            if entry['slot']=='screen' and self.app.program.cue is not None:
                raise ValueError('Full-screen cards must wait for commentary')
            if entry['text_binding']=='evidence' and not intent.title:
                raise ValueError('Action overlay needs a cited short title')
            if entry['text_binding']!='evidence' and (intent.title is not None or intent.subtitle is not None):
                raise ValueError('This template uses prepared or official text')
            if any(slot in self.app.program.graphics.active for slot in ('screen','stinger','lower','banner','ticker')):
                raise ValueError('An information overlay is already active')
        if intent.op=='crop':
            if not intent.rect or not refs or not any(d['confidence']>=.6 for eid in refs for d in allowed[eid]['detections']):
                raise ValueError('Live crop requires geometry and subject evidence')
        dependencies={'snapshot':snapshot,'evidence_ids':refs,'deadline':deadline,'sources':{}}
        if any(allowed[eid].get('audio') for eid in refs):
            dependencies['microphone']=context['target'].get('microphone')
        archive_session=role=='commentator' and context['target'].get('archive_session')
        if archive_session:dependencies['archive_session']=context['target']['archive_session']
        elif role=='commentator':
            reviewed=context['target']['source']
            dependencies['commentary_camera']={'source_path':reviewed['source_id'],'epoch':reviewed['epoch']}
            if context['target'].get('microphone'):
                dependencies['microphone']=context['target']['microphone']
        for source in (() if archive_session else snapshot.sources):
            if role=='commentator' and source.source_id not in (
                    context['target']['source']['source_id'],dependencies.get('microphone',{}).get('source_path')):continue
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
                    if role=='director' or self.foundation.registry.gemini and role=='commentator' and not archive_session:
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
            self.traces.append({'role':role,'stage':'abstain','utc':time.time(),'reason':intent.reason})
            self.state[role]='Holding';self.reason=intent.reason;return None
        if role=='commentator':
            snapshot=dependencies['snapshot']
            reservation=ProgramText(cue_id=(action_id or uuid.uuid4().hex)+'-intent',event_id=snapshot.event_id,
                run_id=snapshot.run_id,text=intent.text,state='pending',event_ms=context['target'].get('event_ms'),
                program_revision=snapshot.program_revision,evidence_ids=intent.evidence_ids,origin='controller',
                basis=intent.basis,speaker=intent.speaker,context_revision=snapshot.context_revision,
                channel='intent',session_id=f'{snapshot.run_id}:{snapshot.program_revision}')
            self.foundation.program_text(reservation,owner='controller',reserve=True)
            self.state[role]='Ready'
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
        elif op=='replay':args={'replay_id':intent.replay_id,**({'transition':intent.transition} if self.foundation.registry.gemini else {})}
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
            args={'graphics':{'op':'cue','preset':intent.preset,'title':intent.title or prepared.title,
                'subtitle':intent.subtitle if intent.subtitle is not None else '' if intent.title else prepared.subtitle,
                'duration_s':intent.duration_s}}
        aid=action_id or uuid.uuid4().hex
        request={'id':aid,'op':op,'args':args,'expected':self.expected(snapshot,args),'expires_at':dependencies['deadline']}
        return self.app.control.propose(request,actor='Provider crew',dependencies=dependencies)

    def _end_reservation(self, reservation, reason):
        self.foundation.program_text(reservation.model_copy(update={'state':'canceled','reason':reason}),owner='controller')

    def _speech(self, intent, dependencies, event):
        """Return (pcm, asset, fallback). BREADCAST_SPEECH=off keeps commentary caption only."""
        if os.environ.get('BREADCAST_SPEECH','on')=='off':return b'',None,'Speech disabled; caption only'
        asset=None
        try:
            if intent.speaker=='co_commentator':
                if not event.co_commentator:raise ValueError('Co-commentator voice is not configured')
                event=event.model_copy(update={'voice_id':event.co_commentator.voice_id,
                    'commentary_style':event.co_commentator.style})
            async def prepare():
                async with asyncio.timeout(min(self.settings.role_timeout_s,dependencies['deadline']-time.time())):
                    return await self.foundation.registry.speech(intent.text,self.foundation.storage,dependencies['deadline'],event_context=event)
            speech=asyncio.run(prepare())
            asset=speech.media
            return decode_speech(speech,self.foundation.storage,intent.text,event,self.foundation.settings),asset,None
        except Exception as error:
            failure=public_failure(error,'speech');self.provider_failures['speech']=failure
            return b'',asset,'Speech unavailable: '+failure['reason']

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
            source_caption=intent.delivery=='source_caption'
            pcm,asset,fallback=(b'',None,None) if source_caption else self._speech(intent,dependencies,event)
            display_text=('Heard: '+intent.text) if source_caption else intent.text
            caption_text=display_text
            if not source_caption and event.co_commentator:
                caption_text=('Co-commentator: ' if intent.speaker=='co_commentator' else 'Cabbie: ')+display_text
            self.check(dependencies)
            layer=None
            try:layer=caption_layer(self.app.program.graphics,caption_text)
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
                if source_caption:remaining=min(remaining,4.0)
                frames=math.floor(remaining*self.app.cfg.fps)
                if program.requested=='REPLAY' and program.replay:
                    frames=min(frames,len(program.replay.frames)-program.replay_index-2)
                if frames<=0:raise ValueError('Commentary window expired before preparation')
                if pcm and math.ceil(len(pcm)/self.app.cfg.audio_size)>frames:
                    if self.foundation.registry.gemini:raise ValueError('Speech cannot fit its original window; prepare a fresh line')
                    pcm=b'';fallback='Speech cannot fit its original window'
                if not pcm and layer is None:raise ValueError('No eligible commentary media')
                target={key:value for key,value in program.actual_target.items() if key in ('kind','source_path','epoch','id','command_revision')}
                aid=reservation.cue_id.removesuffix('-intent')
                session_revision=dependencies.get('archive_session',{}).get('revision',snapshot.program_revision)
                cue=PreparedCue(aid,f'{snapshot.run_id}:{session_revision}',display_text,pcm,layer,start,start+frames,
                    dependencies['deadline'],snapshot.program_revision,target,
                    lambda:self._guard(dependencies),intent.evidence_ids,context['target'].get('event_ms'),asset,
                    basis=intent.basis,speaker=intent.speaker,context_revision=snapshot.context_revision)
            with self.lock:self.prepared[cue.id]=cue
            self._history(cue,'prepared')
            self._history(cue,'pending')
            expected=self.app.control.expected({}) if dependencies.get('commentary_camera') or dependencies.get('archive_session') else self.expected(snapshot,{})
            record=self.app.control.propose({'id':cue.id,'op':'commentary','args':{'cue_id':cue.id},
                'expected':expected,'expires_at':dependencies['deadline']},
                actor='Provider crew',dependencies=dependencies)
            if record['state'] not in ('Scheduled','Applying'):
                self.discard(cue.id,record['reason']);self.drain_receipts()
                raise ValueError('Prepared commentary was rejected')
            self.state['speech']='Listening' if source_caption else 'Prepared' if pcm else 'Caption only'
            self.reason='Showing heard words; source audio stays clear' if source_caption else fallback or 'Prepared speech and captions'
            if pcm:self.provider_failures.pop('speech',None)
            self.traces.append({'cue_id':cue.id,'stage':'speech-ready','speaker':intent.speaker,'utc':time.time(),'samples':len(pcm)//2,'fallback':fallback})
        except Exception as error:
            self.state['speech']='Unavailable';self.reason=str(error) if isinstance(error,ValueError) else type(error).__name__
            self.traces.append({'role':'speech','stage':'error','utc':time.time(),'reason':type(error).__name__+': '+str(error)[:200]})
            if cue:self.discard(cue.id,'Preparation failed')
            elif asset:
                with self.lock:
                    if not any(c.asset and c.asset.key==asset.key for c in self.prepared.values()):self.foundation.storage.delete(asset)
        finally:
            self._end_reservation(reservation,'Preparation transferred to media channels' if cue else 'Preparation failed or expired')

    def _guard(self, dependencies):
        try:return self.check(dependencies)
        except ValueError as error:
            self.traces.append({'role':'speech','stage':'cue-invalid','utc':time.time(),'reason':str(error)[:200]})
            return False

    def _history(self, cue, state, channel=None, receipt=None, reason=None):
        channels=[channel] if channel else (['speech'] if cue.pcm else [])+(['caption'] if cue.caption is not None else [])
        for kind in channels:
            r=receipt or {};first=r.get('first');last=r.get('last')
            self.foundation.program_text(ProgramText(cue_id=cue.id+'-'+kind,event_id=self.foundation.settings.event.event_id,
                run_id=self.app.control.run_id,text=cue.text,state=state,event_ms=cue.event_ms,
                program_revision=cue.program_revision,evidence_ids=cue.evidence_ids,origin='controller',channel=kind,
                basis=cue.basis,speaker=cue.speaker,context_revision=cue.context_revision,
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
            self.app.log('commentary_delivery',cue_id=cue.id,session_id=cue.session_id,speaker=cue.speaker,channel=receipt['channel'],
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
        if self.foundation.registry.gemini and not archive:
            self._microphone_context(context,snapshot)
        if archive:
            context['facts']={}
            context['event']['participants']=[]
            context['target']['archive_session']={'asset_id':self.app.program.replay.id,'revision':self.app.program.replay_revision,
                'source':source.model_dump(mode='json'),'reviewed_pts':point,'output_frame':self.app.program.actual_target.get('output_frame')}
        elif role=='director':
            context['ready_replays']=self.app.replay_work.ready()
            if self.foundation.registry.gemini:
                from graphics import CATALOG, PURPOSES
                package=self.app.program.graphics.event_package
                context['prepared_graphics']=[]
                if package and package['manifest']['context_revision']==snapshot.context_revision:
                    latest=next((o for o in context['observations'] if o.get('view')),None)
                    quiet=bool(latest and latest.get('replay_opportunity') in ('quiet','stoppage','recap'))
                    context['graphics_catalog']=[]
                    for key,spec in CATALOG.items():
                        reason=None
                        if spec['slot']=='stinger':reason=self._transition_unavailable()
                        elif key in ('opening','countdown','closing'):reason='Requires an explicit event phase or start time; not inferred from camera motion'
                        elif spec['slot']=='score' and self.app.program.graphics.score.get('authority')!='operator-confirmed':reason='Official score is unknown'
                        elif spec['slot']=='screen' and (not quiet or self.app.program.actual=='REPLAY'):reason='Needs a quiet live interval'
                        elif key=='matchup' and not context['event'].get('participants'):reason='Participant identities are unknown'
                        binding='prepared' if key=='brand-bug' or spec['slot']=='stinger' else 'official' if spec['slot']=='score' else 'evidence'
                        entry={'id':key,'name':spec['name'],'slot':spec['slot'],'motion':spec['motion'],
                            'purpose':PURPOSES[key],'text_binding':binding,'unavailable_reason':reason}
                        if spec['slot']=='stinger':entry['max_duration_s']=1.0
                        context['graphics_catalog'].append(entry)
                        if reason is None:context['prepared_graphics'].append(entry)
                context['active_graphics']=[cue.summary() for cue in self.app.program.graphics.active.copy().values()]
                context['recent_graphics']=[{'preset':r['args']['graphics'].get('preset'),
                    'title':r['args']['graphics'].get('title'),'state':r['state']}
                    for r in tuple(self.app.control.actions.values()) if r['op']=='graphics' and
                    r.get('cue_ids') and r['state'] in ('On air','Finished')][-20:]
        if role=='commentator':context['allowed_speakers']=self._speaker_options(context)
        return context

    @staticmethod
    def _speaker_options(context):
        if not context['event'].get('co_commentator'):return ['lead']
        if any(r.get('speaker')=='co_commentator' for r in context['pending']):return ['lead']
        leads=0
        # Only delivered speech establishes a conversation. A caption, pending
        # line or failed preparation must never become a fictional reply target.
        for row in reversed(context['aired']):
            if row.get('channel')!='speech':continue
            if row.get('speaker','lead')=='co_commentator':break
            if row['state']=='completed':leads+=1
        return ['lead','co_commentator'] if leads>=2 else ['lead']

    def _transition_unavailable(self):
        if self.app.program.actual!='LIVE' or self.app.program.requested!='LIVE':
            return 'Scene transitions require a live view'
        for action in tuple(self.app.control.actions.values()):
            if (action['op']=='graphics' and action.get('args',{}).get('graphics',{}).get('preset') in self.STINGERS and
                    action['state'] in ('Scheduled','Applying','On air','Finished') and
                    time.time()-action['created_at']<20):
                return 'Leave at least 20 seconds between scene transitions'
        return None

    def _microphone_context(self,context,snapshot):
        """Review the selected microphone on its own clock, even across camera cuts."""
        program=snapshot.runtime.program
        microphone=next((s for s in snapshot.sources if s.source_id==program.audio_source_path),None)
        context['target']['microphone']={'slot':program.audio_slot,'source_path':program.audio_source_path,
            'epoch':self.app.program.audio_epoch,'muted':program.audio_muted}
        # Audio from other cameras must not become captions for the on-air microphone.
        context['observations']=[o for o in context['observations'] if not o.get('audio')]
        if not microphone or program.audio_muted or microphone.epoch!=self.app.program.audio_epoch:return
        current=self.app.get_source(microphone.slot)
        if not current or current.path!=microphone.source_id or current.epoch!=microphone.epoch:return
        frame=current.at(time.monotonic()-self.app.cfg.delay)
        native=frame.native_provenance if frame else None
        if not native:return
        point=round(native['native_pts']*float(Fraction(native['native_time_base'])/Fraction(microphone.time_base)))
        end=point+1-math.ceil(native.get('uncertainty_ms',1000/self.app.cfg.fps)/1000/float(Fraction(microphone.time_base)))
        eligible=Interval(start=end-round(12/float(Fraction(microphone.time_base))),end=end)
        # Listening needs no TTS reserve. Original evidence deadlines still apply.
        heard=self.foundation.context('director',microphone,eligible,snapshot)['observations']
        latest=max((o for o in heard if o.get('audio') and o['native']['end']<=end),
            key=lambda o:o['native']['end'],default=None)
        if latest:context['observations'].append(latest)

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
            event_bridge=(role=='commentator' and self.foundation.registry.gemini and
                self.app.program.actual=='LIVE' and bool(context['event'].get('title')))
            if context['status']=='unavailable' or not context['observations'] and not event_bridge:
                self.traces.append({'role':role,'stage':'skip','utc':time.time(),'reason':'no eligible observations'});return
            # Give the role the full role_timeout for the model call. Evidence
            # job deadlines only gate eligibility; shrinking the HTTP budget to
            # a near-expiry job caused constant deadline_missed on W&B.
            minimum_budget=min(5.0,self.settings.role_timeout_s/2)
            deadline=time.time()+self.settings.role_timeout_s
            if role=='director':
                with self.foundation.lock:
                    deadlines=[self.foundation.db.execute('SELECT deadline FROM jobs WHERE key=?',(o['job_key'],)).fetchone()[0] for o in context['observations']]
                if not deadlines or max(deadlines)<=time.time()+minimum_budget:return
                if self.foundation.registry.gemini:deadline=min(deadline,max(deadlines))
            elif self.app.program.actual=='LIVE':
                with self.foundation.lock:
                    jobs=[self.foundation.db.execute('SELECT deadline,body FROM jobs WHERE key=?',(o['job_key'],)).fetchone() for o in context['observations']]
                fresh=[job['deadline'] for job in jobs if job and json.loads(job['body']).get('deadline_basis')=='frame-receipt' and job['deadline']>time.time()+minimum_budget]
                if not fresh and not event_bridge:
                    self.traces.append({'role':role,'stage':'skip','utc':time.time(),'reason':'no fresh live evidence'});return
                # Cited action deadlines still apply in validate(). Event talk
                # depends on the reviewed brief, not expired camera evidence.
                if self.foundation.registry.gemini and fresh and not event_bridge:deadline=min(deadline,max(fresh))
            self.state[role]='Thinking'
            self.traces.append({'role':role,'stage':'model-start','utc':time.time(),'deadline_utc':deadline})
            async def call():
                for attempt in range(self.settings.attempts):
                    try:return await self.foundation.registry.llm(role,context,snapshot,deadline)
                    except (ConnectionError,OSError) as error:
                        if isinstance(error,TimeoutError) or attempt+1==self.settings.attempts:raise
            result=asyncio.run(call())
            self.provider_failures.pop(role,None)
            self.traces.append({'role':role,'stage':'model-end','utc':time.time(),'model_id':result.model_id,'version':result.model_version,'origin':result.origin})
            try:self.dispatch(result,role,context,deadline)
            except ValueError as error:
                # A current-state rejection is not a provider response-format failure.
                self.state[role]='Waiting';self.reason=str(error)
                self.traces.append({'role':role,'stage':'rejected','utc':time.time(),'reason':str(error)[:200]})
        except ValueError as error:
            self.state[role]='Waiting';self.reason=str(error)[:200]
            self.traces.append({'role':role,'stage':'waiting','utc':time.time(),'reason':self.reason})
        except Exception as error:
            failure=public_failure(error,'llm');self.provider_failures[role]=failure
            self.state[role]='Unavailable';self.reason=failure['reason']
            self.traces.append({'role':role,'stage':'error','utc':time.time(),'reason':type(error).__name__+': '+str(error)[:200]})

    GRAPHICS_S=14.0
    BANNERS=(('headline','A fresh take.','You’re watching Breadcast.'),
             ('toast-note','A little something fresh.','Stay for the good stuff.'),
             ('wide-banner','Made to be shared.','Good views bring people together.'))
    STINGERS=('toast-wipe','ribbon-sweep','iris-reveal','crumb-burst')
    LOWERS=('lower-classic','lower-pill','lower-split','lower-portrait')

    def _graphic(self, preset, title, subtitle, duration_s):
        args={'graphics':{'op':'cue','preset':preset,'title':title,'subtitle':subtitle,'duration_s':duration_s}}
        record=self.app.control.propose({'id':uuid.uuid4().hex,'op':'graphics','args':args,
            'expected':self.app.control.expected({}),'expires_at':time.time()+5},actor='Provider crew')
        self.traces.append({'role':'graphics','stage':'propose','utc':time.time(),'preset':preset,'state':record.get('state'),'reason':record.get('reason')})
        return record

    def _policy_graphics(self):
        """Deterministic on-air dressing from prepared presets. Text is preset copy or the camera number only."""
        program=self.app.program
        if self.app.control.crew_paused or not self.app.control.program_started or program.actual!='LIVE':return
        if any(r['state'] in ('Scheduled','Applying') for r in self.app.control.actions.values()):return
        active=program.graphics.active
        now=time.monotonic();state=self.__dict__.setdefault('_gfx',{'slot':None,'cuts':0,'banner':0,'next':0.0,'tried':0.0})
        if now<state['tried']+1:return
        state['tried']=now
        if 'bug' not in active:
            self._graphic('brand-bug','breadcast.','',600);return
        if program.cue is not None or 'screen' in active or 'stinger' in active:return
        slot=program.slot
        if slot!=state['slot']:
            state['slot']=slot;state['cuts']+=1
            if state['cuts']%3==0:self._graphic(self.STINGERS[state['cuts']//3%len(self.STINGERS)],'breadcast.','',2)
            else:self._graphic(self.LOWERS[state['cuts']%len(self.LOWERS)],f'Camera {slot}','Live on Breadcast',3)
            state['next']=now+self.GRAPHICS_S;return
        if now>=state['next'] and not any(s in active for s in ('lower','banner','ticker')) and not self._commentary_busy():
            preset,title,subtitle=self.BANNERS[state['banner']%len(self.BANNERS)];state['banner']+=1
            self._graphic(preset,title,subtitle,4);state['next']=now+self.GRAPHICS_S

    def _showcase_graphics(self):
        """Explicit event demo: prepared facts keep graphics moving without model work."""
        control=self.app.control;program=self.app.program
        if (control.crew_paused or not control.program_started or control.rehearsal['state']=='Running' or
                program.actual!='LIVE' or program.requested!='LIVE'):return
        event=self.foundation.event_context()
        if event.editorial_policy.get('graphics_mode')!='showcase' or not event.title:return
        package=program.graphics.event_package
        if not package or package['manifest']['context_revision']!=event.revision:return
        actions=[r for r in tuple(control.actions.values()) if r['op']=='graphics']
        if any(r['state'] in ('Scheduled','Applying') for r in actions):return
        now=time.time()
        if any(now-r['created_at']<1 for r in actions):return
        accepted=[r for r in actions if r.get('cue_ids') and r['state'] in ('On air','Finished')]
        if any(now-r['created_at']<8 for r in accepted):return
        if any(slot in program.graphics.active or slot in program.graphics.retiring
                for slot in ('screen','stinger','lower','banner','ticker')):return
        title=event.branding.get('short_title') or event.title
        organizer=event.branding.get('organizer') or event.title
        venue=event.branding.get('venue','')
        # No invented people, scores, breaks, event phase or countdown. Replay
        # playback stays separate. These are event facts, not camera observations.
        entries=[('headline',title,venue),('ribbon-sweep',title,''),
            ('lower-classic',organizer,event.title),('toast-note',title,venue),
            ('iris-reveal',title,''),('lower-pill',title,organizer),
            ('wide-banner',event.title,venue),('toast-wipe',title,''),
            ('lower-split',organizer,venue),('caption',title,''),
            ('crumb-burst',title,''),('ticker',title+(' · '+venue if venue else ''),''),
            ('corner-label',organizer,''),('status-bug','LIVE',''),
            ('brand-bug',event.branding.get('broadcast','Breadcast'),'')]
        if self._transition_unavailable():entries=[e for e in entries if e[0] not in self.STINGERS]
        def usage(entry):
            previous=[r for r in accepted if r['args'].get('graphics',{}).get('preset')==entry[0]]
            return len(previous),max((r['created_at'] for r in previous),default=0)
        preset,title,subtitle=min(entries,key=usage)
        record=self._graphic(preset,title[:80],subtitle[:120],.8 if preset in self.STINGERS else 5)
        self.traces.append({'role':'graphics','stage':'showcase','utc':now,'preset':preset,
            'context_revision':event.revision,'state':record['state'],'reason':'Prepared event facts; no new camera claim'})

    def _policy_replay(self):
        """Air any ready automatic replay after the controller cooldown; no opportunity evidence required."""
        control=self.app.control;program=self.app.program;policy=control.policy
        if control.crew_paused or not control.program_started or not policy.get('replays_enabled'):return
        if program.actual!='LIVE' or program.requested!='LIVE' or program.cue is not None:return
        now=time.monotonic()
        if now<control.last_replay+policy.get('replay_cooldown_s',30):return
        if now<control.last_shot+policy.get('minimum_shot_s',2):return
        if now<getattr(self,'_replay_tried',0)+3:return
        if any(r['state'] in ('Scheduled','Applying') or (r['op']=='replay' and r['state']=='On air') for r in control.actions.values()):return
        ready=sorted(self.app.replay_work.ready(),key=lambda r:r['expires_at'])
        if not ready:return
        self._replay_tried=now
        asset=ready[0];args={'replay_id':asset['id']}
        record=control.propose({'id':uuid.uuid4().hex,'op':'replay','args':args,'expected':control.expected(args),
            'expires_at':min(asset['expires_at'],time.time()+10)},actor='Provider crew')
        self.traces.append({'role':'replay','stage':'propose','utc':time.time(),'state':record.get('state'),'reason':record.get('reason')})
        if record.get('state') in ('Scheduled','Applying','On air'):self.reason='Policy replay '+asset['id'][:8]

    COMMENTARY_S=4.0
    COMMENTARY_HOLD=3

    def _commentary_busy(self):
        pending=self.foundation.role_pending;active=self.foundation.role_active
        with self.lock:prepared=bool(self.prepared)
        return prepared or 'commentator' in active or 'speech' in active or 'speech' in pending

    def _policy_rotate(self):
        """Cut to the next healthy live slot on policy.rotate_s for unrelated feeds."""
        policy=self.app.control.policy
        interval=policy.get('rotate_s')
        if not interval or interval<=0:return
        if self.app.control.crew_paused or not self.app.control.program_started:return
        if self.app.program.actual not in ('LIVE','HOLDING'):return
        # Wait until an on-air live shot exists, then rotate on the policy cadence.
        if self.app.control.last_shot<=0:return
        gate=max(policy.get('minimum_shot_s',0),interval)
        now_mono=time.monotonic()
        if now_mono<self.app.control.last_shot+gate:return
        # A cut invalidates commentary for the outgoing camera. Hold the shot while a
        # line is being written or on air, but never longer than the hold cap.
        held=now_mono-self.app.control.last_shot
        if self.app.program.cue is not None and held<gate+self.settings.speech_max_s+1:return
        if held<gate*self.COMMENTARY_HOLD and self._commentary_busy():return
        if now_mono<getattr(self,'_last_rotate_mono',0)+gate:return
        snapshot=self.foundation.reviewed_snapshot()
        if not snapshot.runtime:return
        healthy=[h for h in snapshot.runtime.source_health
            if h.buffer_ready and h.last_frame_age_s is not None and h.last_frame_age_s<=.75]
        slots=sorted({h.slot for h in healthy})
        if len(slots)<2:return
        current=snapshot.runtime.program.primary_slot
        nxt=slots[(slots.index(current)+1)%len(slots)] if current in slots else slots[0]
        if nxt==current:return
        args={'slot':nxt,'independent':True}
        try:
            record=self.app.control.propose({
                'id':uuid.uuid4().hex,'op':'live','args':args,
                'expected':self.app.control.expected(args),
                'expires_at':time.time()+self.settings.role_timeout_s,
            },actor='Provider crew')
            self._last_rotate_mono=now_mono
            if record.get('state') in ('Scheduled','Applying','On air','Finished'):
                self.reason='Rotated to camera '+str(nxt)
            else:
                self.reason='Policy rotate: '+(record.get('reason') or record.get('state') or 'rejected')
        except Exception as error:
            self.reason='Policy rotate: '+type(error).__name__+': '+str(error)[:120]

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
                if not self.foundation.registry.gemini:
                    try:self._policy_replay()
                    except Exception as error:self.traces.append({'role':'replay','stage':'error','utc':time.time(),'reason':type(error).__name__+': '+str(error)[:160]})
                    self._policy_rotate()
                    try:self._policy_graphics()
                    except Exception as error:self.traces.append({'role':'graphics','stage':'error','utc':time.time(),'reason':type(error).__name__+': '+str(error)[:160]})
                else:
                    try:self._showcase_graphics()
                    except Exception as error:self.traces.append({'role':'graphics','stage':'error','utc':time.time(),'reason':type(error).__name__+': '+str(error)[:160]})
                live=self.app.program.actual!='HOLDING' and self.app.program.requested!='HOLDING'
                gemini=bool(self.foundation.registry.gemini)
                commentary_tick=(int(time.monotonic()/(1 if gemini else self.COMMENTARY_S)),self.app.program.revision)
                cue=self.app.program.cue
                near_end=gemini and cue is not None and bool(cue.pcm) and (len(cue.pcm)-cue.offset)/96000<=4
                queued=any(c is not cue for c in self.prepared.values())
                preparing=bool({'commentator','speech'} & (self.foundation.role_active | self.foundation.role_pending.keys()))
                if live and commentary_tick!=self.last_commentary and (cue is None or near_end) and not queued and not preparing:
                    self.last_commentary=commentary_tick
                    self.foundation.submit_role('commentator',lambda:self._decide('commentator'))
                # While replay prepare owns a worker, skip director calls so the
                # segmentor is not stuck behind 60s director timeouts.
                if not self.foundation.registry.gemini and (self.app.replay_work.planning or 'segmentor' in self.foundation.role_pending):
                    continue
                snapshot=self.foundation.reviewed_snapshot()
                trigger=(snapshot.evidence_revision,snapshot.program_revision,snapshot.control_revision,
                         tuple((h.source_id,h.epoch,h.buffer_ready) for h in snapshot.runtime.source_health) if snapshot.runtime else ())
                # Include wall cadence so rotate and director keep waking while LIVE.
                cadence=int(time.monotonic()/(self.app.control.policy.get('rotate_s') or 6))
                if self.app.program.actual=='REPLAY':cadence=int(time.monotonic())
                trigger=(*trigger,cadence,tuple(r['id'] for r in self.app.replay_work.ready()))
                if trigger==self.last_trigger:continue
                self.last_trigger=trigger
                self.foundation.submit_role('director',lambda:self._decide('director'))
            except Exception as error:self.reason='Crew coordinator: '+type(error).__name__
        self.app.program.cancel_commentary('Studio stopped')
        for cue in tuple(self.prepared.values()):self.discard(cue.id,'Studio stopped')
        self.drain_receipts()
