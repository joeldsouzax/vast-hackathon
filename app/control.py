"""Local action boundary. Provider adapters call propose(); HTTP input is always human."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
import uuid

AIR = {'live', 'audio', 'replay', 'holding', 'graphics', 'crop', 'reset_crop', 'commentary'}
TERMINAL = {'Finished', 'Ready', 'Failed', 'Rejected', 'Expired', 'Canceled'}
PENDING = {'Scheduled', 'Applying'}


class Coordinator:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.run_id = uuid.uuid4().hex
        self.crew_paused = getattr(app.cfg, 'crew_mode', 'automatic') == 'human'
        self.program_started = False
        self.auto_start_armed = getattr(app.cfg, 'crew_mode', 'automatic') == 'automatic'
        self.revision = 0
        # rotate_s: when several unrelated live sources are healthy, cut to the
        # next slot on this cadence. minimum_shot_s still gates every live cut.
        self.policy = {'minimum_shot_s': 2, 'rotate_s': 6, 'replay_max_s': 12,
                       'replay_cooldown_s': 20, 'replays_enabled': True}
        self.actions = {}
        self.last_shot = 0
        self.last_replay = 0
        self.rehearsal = {'state': 'Stopped', 'label': 'Local rehearsal', 'generation': 0}
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _save(self, record):
        record['updated_at'] = time.time()
        self.app.log('studio_action', run_id=self.run_id, control_revision=self.revision, record=copy.deepcopy(record))

    def _state(self, record, state, reason=None):
        record['state'] = state
        if reason:
            record['reason'] = reason
        self._save(record)

    def snapshot(self):
        with self.lock:
            self._poll()
            records = list(self.actions.values())
            current = [r for r in records if r['state'] not in TERMINAL]
            failures = [r for r in records if r['state'] in ('Failed', 'Rejected', 'Expired')][-32:]
            recent = [r for r in records if r['state'] in ('Ready', 'Finished', 'Canceled')][-24:]
            return copy.deepcopy({'run_id': self.run_id, 'crew_paused': self.crew_paused, 'program_started': self.program_started, 'auto_start_armed': self.auto_start_armed, 'control_revision': self.revision,
                'context_revision': self.app.foundation.context_revision, 'policy': self.policy,
                'rehearsal': self.rehearsal, 'actions': current + failures + recent})

    def _record(self, request, actor, chat_text=None):
        if not isinstance(request, dict) or set(request) - {'id', 'op', 'args', 'expected', 'expires_at'}:
            raise ValueError('Unsupported action fields; actor and approval belong to the server')
        aid = request.get('id')
        if not isinstance(aid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', aid):
            raise ValueError('A stable action ID is required')
        encoded = json.dumps(request, sort_keys=True, allow_nan=False)
        signature = hashlib.sha256((actor + encoded + json.dumps(chat_text)).encode()).hexdigest()
        if aid in self.actions:
            if self.actions[aid]['signature'] != signature:
                raise ValueError('Action ID was reused with changed contents')
            return self.actions[aid], False
        if actor!='Human' and len(self.actions)>=self.app.foundation.settings.direction.action_records:
            raise ValueError('Crew action history capacity reached; human controls remain available')
        if actor != 'Human' and sum(r['state'] in PENDING | {'Queued', 'Preparing'} for r in self.actions.values()) >= 32:
            raise ValueError('Pending crew work reached its 32-action limit')
        record = {'id': aid, 'run_id': self.run_id, 'actor': actor, 'op': request.get('op'),
                  'args': copy.deepcopy(request.get('args', {})), 'expected': copy.deepcopy(request.get('expected')),
                  'expires_at': request.get('expires_at'), 'signature': signature, 'state': 'Queued',
                  'created_at': time.time(), 'created_monotonic_s': time.monotonic(), 'reason': ''}
        if chat_text is not None:
            record['input_text'] = chat_text
        self.actions[aid] = record
        self._save(record)
        return record, True

    def _invalidate(self, reason):
        self.revision += 1
        for record in self.actions.values():
            if record['actor'] != 'Human' and record['state'] == 'Scheduled':
                self._state(record, 'Canceled', reason)
        if self.rehearsal['state'] == 'Running':
            self.rehearsal.update(state='Canceled', reason=reason)
        self.rehearsal['generation'] += 1
        self.app.program.cancel_commentary(reason)

    def _takeover(self):
        self.auto_start_armed = False
        self.crew_paused = True
        self._invalidate('Human took control; pending airtime canceled')

    def expected(self, args):
        sources = []
        slots = {args.get('slot', self.app.program.slot)}
        replay = self.app.replays.get(args.get('replay_id'))
        if replay:
            plan=replay.report['plan']
            if plan.get('schema_version')=='1.2':
                if plan['input_kind']=='live_buffer':slots.update(s['source']['slot'] for s in plan['shots'])
            else:slots.update(int(s['source_id'].removeprefix('camera-')) for s in plan['shots'])
        for slot in sorted(slots):
            source = self.app.get_source(slot)
            if source:
                sources.append({'slot': slot, 'source_path': source.path, 'epoch': source.epoch})
        return {'run_id': self.run_id, 'context_revision': self.app.foundation.context_revision, 'control_revision': self.revision,
                'program_revision': self.app.program.status()['revision'], 'sources': sources}

    def _check_expected(self, expected):
        if not isinstance(expected, dict) or set(expected) != {'run_id', 'context_revision', 'control_revision', 'program_revision', 'sources'}:
            raise ValueError('Expected run, control, program, context, and sources are required')
        if any(type(expected[k]) is not int for k in ('context_revision', 'control_revision', 'program_revision')):
            raise ValueError('Expected revisions must be integers')
        if not isinstance(expected['sources'], list):
            raise ValueError('Expected sources must be a list')
        for source in expected['sources']:
            if not isinstance(source, dict) or set(source) != {'slot', 'source_path', 'epoch'} or type(source['slot']) is not int or type(source['epoch']) is not int:
                raise ValueError('Expected source lease and integer epoch are required')

    def _fresh(self, record):
        expires = record['expires_at']
        if type(expires) not in (float, int) or not math.isfinite(expires) or time.time() >= expires:
            raise ValueError('Proposal expired')
        expected = record['expected']
        self._check_expected(expected)
        if expected != self.expected(record['args']):
            raise ValueError('Proposal run, control, program, or source revision changed')
        direction=getattr(self.app,'direction',None)
        if direction and record['id'] in direction.dependencies:
            direction.check(direction.dependencies[record['id']])

    def _validate_args(self, op, args):
        fields = {'live': {'slot', 'independent'}, 'audio': {'slot', 'muted'}, 'replay': {'replay_id','transition'},
                  'holding': set(), 'graphics': {'graphics','effective_event_ms'}, 'prepare': {'slot', 'seconds', 'speed', 'zoom', 'plan', 'search_id','scene_id','scene_revision'},
                  'cancel': {'job_id'}, 'takeover': set(), 'resume': set(),
                  'policy': set(self.policy), 'rehearsal': {'slot'},
                  'crop': {'rect','geometry_revision'}, 'reset_crop': set(), 'commentary': {'cue_id'}}
        if op not in fields or not isinstance(args, dict) or set(args) - fields[op]:
            raise ValueError('Unknown operation or unsupported fields')
        if op == 'graphics' and not isinstance(args.get('graphics'), dict):
            raise ValueError('Expected a graphics command')
        if 'transition' in args and args['transition'] not in ('toast-wipe','ribbon-sweep','crumb-burst','iris-reveal'):
            raise ValueError('Unknown replay transition')
        if 'effective_event_ms' in args and (args['graphics'].get('op')!='score' or args['effective_event_ms'] is not None and type(args['effective_event_ms']) is not int):
            raise ValueError('Effective event time belongs to a human-confirmed score')
        for key in ('replay_id', 'job_id'):
            if key in args and not isinstance(args[key], str):
                raise ValueError(f'{key} must be a string')
        if 'slot' in args and (type(args['slot']) is not int or args['slot'] not in range(1, 6)):
            raise ValueError('Camera slot must be an integer from 1 to 5')
        if 'independent' in args and type(args['independent']) is not bool:
            raise ValueError('Independent view acknowledgement must be a boolean')
        if 'muted' in args and type(args['muted']) is not bool:
            raise ValueError('Microphone mute must be a boolean')
        if op == 'prepare' and any(type(args[k]) not in (int, float) or not math.isfinite(args[k]) for k in ('seconds', 'speed', 'zoom') if k in args):
            raise ValueError('Replay parameters must be finite numbers')
        if op == 'prepare' and 'plan' in args and set(args) != {'plan'}:
            raise ValueError('Use either a ReplayPlan or single-camera controls')
        if op=='prepare' and any(k in args for k in ('search_id','scene_id','scene_revision')):
            if set(args)!={'search_id','scene_id','scene_revision'} or not all(isinstance(args[k],str) for k in ('search_id','scene_id')) or type(args['scene_revision']) is not int or args['scene_revision']<1:
                raise ValueError('Use one existing search hit or existing replay controls')
        if op=='crop':
            from foundation_records import CropRect
            CropRect.model_validate(args.get('rect'))
            if type(args.get('geometry_revision')) is not int or args['geometry_revision']<1:
                raise ValueError('Reviewed geometry revision is required for live crop')

    def replay_reason(self, replay):
        if replay.report['plan'].get('schema_version')=='1.2':
            self.app.replay_work.eligible(replay)
            return
        if not replay.path.is_file() or not replay.frames or not replay.report.get('decode_passed'):
            raise ValueError('Replay is not validated and ready')
        plan = replay.report['plan']
        if plan.get('timing_mode') != 'source_only':
            self.app.replay_context.eligible(plan)
        for shot in replay.report['source_map']:
            source = self.app.get_source(int(shot['source_id'].removeprefix('camera-')))
            if not source or source.epoch != shot['source_epoch'] or source.path != shot['retained_media']['source_path']:
                raise ValueError('Replay source lease or epoch changed')

    def _media(self, record, prepared=None):
        if record['op'] == 'graphics' and prepared and prepared.op == 'score':
            if record['actor'] != 'Human': raise ValueError('Only human confirmation can change official facts')
            expected = record['expected']
            if expected is not None:
                current=self.expected(record['args'])
                if any(expected[key]!=current[key] for key in ('run_id','context_revision','program_revision','sources')):
                    raise ValueError('Action run, program, or source revision changed')
            values = {k:v for k,v in prepared.score.items() if k != 'authority'}
            committed = self.app.foundation.confirm_facts(values, self.app.foundation.context_revision,
                'score-'+record['id'], authority='human',effective_event_ms=record['args'].get('effective_event_ms'))
            self.app.program.score_effective_ms=record['args'].get('effective_event_ms')
            prepared = __import__('dataclasses').replace(prepared, score={**committed['values'], 'authority':'operator-confirmed'})
            self._invalidate('Official facts changed')
            # This human action caused the context revision; preserve its reviewed revision in history.
            record['committed_context_revision'] = committed['context_revision']
        with self.app.program.lock, self.app.sources_lock:
            return self._commit_media(record, prepared)

    def _commit_media(self, record, prepared=None):
        if record["actor"] != "Human":
            self._fresh(record)
        elif record['expected'] is not None:
            expected = record['expected']
            self._check_expected(expected)
            current = self.expected(record['args'])
            if record.get('committed_context_revision'):
                current['context_revision'] = expected['context_revision']
            if any(expected[k] != current[k] for k in ('run_id', 'context_revision', 'program_revision', 'sources')):
                raise ValueError('Action run, program, or source revision changed')
        op, args = record['op'], record['args']
        if record['actor']=='Provider crew' and op in ('live','replay') and not self.program_started:
            raise ValueError('An operator must start the program before crew recovery')
        slot = args.get('slot', self.app.program.slot)
        if op in ('live', 'audio'):
            source = self.app.get_source(slot)
            frame=source.at(time.monotonic()-self.app.cfg.delay) if source else None
            if not source or frame is None or getattr(frame,'source_epoch',None) not in (None,source.epoch):
                raise ValueError(f'Camera {slot} is unavailable; delay buffer is not ready')
            if op == 'live' and 'slot' not in args and self.app.program.primary_source_path not in (None, source.path):
                raise ValueError('Primary camera changed; select an independent view')
            if op == 'audio' and not source.has_audio:
                raise ValueError(f'Camera {slot} has no usable audio')
        replay = self.app.replays.get(args.get('replay_id'))
        if op == 'replay':
            if not replay:
                raise ValueError('Replay is not validated and ready')
            self.replay_reason(replay)
            if replay.report['plan'].get('input_kind')=='archive' and not self.app.replay_work.guard('play-'+record['id']):
                raise ValueError('Archive playback ticket is unavailable or expired')
        expected = record['expected']
        revision = expected['program_revision'] if expected else self.app.program.status()['revision']
        if op=='commentary':
            cue=self.app.direction.prepared.get(args.get('cue_id'))
            if not cue or cue.id!=record['id']:raise ValueError('Prepared commentary is unavailable')
            self.app.program.schedule_commentary(cue)
            record['program_revision']=revision
            self._state(record,'Applying')
            return
        result = self.app.program.command(op, revision, slot, replay, args.get('independent', False),
                                          args.get('graphics'), prepared=prepared, muted=args.get('muted', False),rect=args.get('rect'),
                                          geometry_revision=args.get('geometry_revision'),replay_transition=args.get('transition',
                                              'ribbon-sweep' if op=='replay' and self.app.foundation.registry.gemini else None))
        if op=='replay':self.app.program.replay_ticket=self.app.replay_work.tickets.get('play-'+record['id'])
        record['program_revision'] = result['revision']
        if record['actor'] in ('Human','Automatic start') and op in ('live','replay'):
            self.program_started=True
            self.auto_start_armed=False
        if record['actor']=='Human' and op=='holding':self.auto_start_armed=False
        if op == 'live':
            record['target'] = {'kind': 'camera', 'source_path': source.path, 'epoch': source.epoch}
        elif op == 'replay':
            record['target'] = {'kind': 'replay', 'id': replay.id, 'command_revision': result['revision']}
        elif op == 'holding':
            record['target'] = {'kind': 'holding'}
        if op == 'graphics' and args.get('graphics', {}).get('op') == 'cue':
            record['cue_ids'] = [c['cue_id'] for c in result['graphics']['requested'] if c['preset'] == args['graphics']['preset']]
        self._state(record, 'Applying')
        if op == 'live':
            self.last_shot = time.monotonic()
        if op == 'replay':
            self.last_replay = time.monotonic()

    def start_ready_camera(self):
        """One server-owned start after Join; later cameras cannot override the operator."""
        if self.app.examples.status()['configured']:return
        with self.lock:
            if (not self.auto_start_armed or self.program_started or self.crew_paused or
                    self.app.stop.is_set() or self.rehearsal['state']=='Running' or
                    self.app.program.requested!='HOLDING'):return
            for lease in sorted(self.app.leases.rows(),key=lambda row:row['slot']):
                if lease['state']!='ACTIVE':continue
                source=self.app.get_source(lease['slot'])
                if not source or source.path!=lease['path'] or source.epoch!=lease['epoch']:continue
                health=source.status()
                if not health.get('buffer_ready') or health.get('last_frame_age_s') is None or health['last_frame_age_s']>1:continue
                # Bind the initial camera microphone at its ready epoch. Preserve
                # an operator's separate microphone or explicit mute.
                if (source.has_audio and not self.app.program.audio_muted and
                        self.app.program.audio_source_path in (None,source.path)):
                    audio_args={'slot':source.slot,'muted':False}
                    audio,_=self._record({'id':uuid.uuid4().hex,'op':'audio','args':audio_args,
                        'expected':self.expected(audio_args),'expires_at':time.time()+2},'Automatic start')
                    try:self._media(audio)
                    except (ValueError,TypeError,KeyError) as error:
                        self._state(audio,'Rejected',str(error))
                args={'slot':source.slot,'independent':True}
                record,_=self._record({'id':uuid.uuid4().hex,'op':'live','args':args,
                    'expected':self.expected(args),'expires_at':time.time()+2},'Automatic start')
                try:self._media(record)
                except (ValueError,TypeError,KeyError) as error:
                    self._state(record,'Rejected',str(error))
                return

    def submit(self, request, *, chat_text=None):
        """Human path. Recognized direct airtime commands take over before validation."""
        with self.lock:
            record, new = self._record(request, 'Human', chat_text)
            if not new:
                return copy.deepcopy(record)
            op, args = record['op'], record['args']
            expected = record['expected']
            if expected is not None and (not isinstance(expected, dict) or expected.get('run_id') != self.run_id):
                self._state(record, 'Rejected', 'Action belongs to a prior or missing run')
                return copy.deepcopy(record)
            if isinstance(op, str) and op in AIR:
                if getattr(self.app.cfg,'crew_mode','automatic')=='automatic' and self.app.foundation.registry.gemini:
                    self._invalidate('Operator command replaced pending proposals')
                else:
                    self._takeover()
                    record['takeover'] = True
            authority = self.revision
        # Text binding can be expensive. No media or coordinator lock is held.
        try:
            self._validate_args(op, args)
            if op=='commentary':raise ValueError('Prepared commentary belongs to the trusted crew path')
            if record['expected'] is not None:
                self._check_expected(record['expected'])
                if record['expected']['run_id'] != self.run_id or record['expected']['context_revision'] != self.app.foundation.context_revision:
                    raise ValueError('Action belongs to a prior run or context')
            prepared = self.app.program.graphics.prepare(args.get('graphics')) if op == 'graphics' else None
            # Archive resolution, hashing and preload occur before acquiring control/media locks.
            if op=='replay':
                replay=self.app.replays.get(args.get('replay_id'))
                if replay:self.app.replay_work.preload(replay,'play-'+record['id'])
            if op=='prepare' and 'search_id' in args:
                job=self.app.replay_work.prepare_hit(args,record['id'])
                with self.lock:
                    record['job_id']=job['id'];self._state(record,'Preparing')
                return copy.deepcopy(record)
            if op=='prepare' and args.get('plan',{}).get('schema_version')=='1.2':
                job=self.app.render(plan=args['plan'])
                with self.lock:
                    record['job_id']=job['id'];self._state(record,'Preparing')
                return copy.deepcopy(record)
            with self.lock, self.app.replay_context.lock:
                if op in ('resume', 'policy', 'rehearsal') and record['expected'] is not None and record['expected']['control_revision'] != self.revision:
                    raise ValueError('Control revision changed; refresh and retry')
                if op in AIR:
                    if authority != self.revision:
                        raise ValueError('Control changed while the command was prepared')
                    # Validate source ownership inside the same lock as controller commit.
                    self._media(record, prepared)
                elif op == 'prepare':
                    record['job_id'] = self._prepare(record)['id']
                    self._state(record, 'Preparing')
                elif op == 'cancel':
                    self.app.cancel_render(args.get('job_id'))
                    self._state(record, 'Finished', 'Cancellation requested for this job')
                elif op == 'takeover':
                    self._takeover()
                    self._state(record, 'Finished', 'Crew paused; current picture continues')
                elif op == 'resume':
                    self._invalidate('Control released; old proposals canceled')
                    self.crew_paused = False
                    self._state(record, 'Finished', 'Control released; fresh crew proposals can run')
                elif op == 'policy':
                    for key, value in args.items():
                        if key == 'replays_enabled':
                            if type(value) is not bool:
                                raise ValueError('Replay enablement must be a boolean')
                        elif type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= (12 if key == 'replay_max_s' else 300):
                            raise ValueError('Policy value is outside its limit')
                    self._invalidate('Policy changed; old proposals canceled')
                    self.policy.update(args)
                    self._state(record, 'Finished', 'Policy updated')
                elif op == 'rehearsal':
                    self._start_rehearsal(args.get('slot', self.app.program.slot))
                    self._state(record, 'Finished', 'Local rehearsal started; no video recognition')
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            with self.lock:
                self._state(record, 'Rejected', str(error))
            self.app.replay_work.release_ticket('play-'+record['id'])
        return copy.deepcopy(record)

    def _prepare(self, record):
        args = record['args']
        if 'plan' in args:
            return self.app.render(**args)
        source = self.app.get_source(args.get('slot', 1))
        expected = record['expected']
        if expected is not None:
            target = next((s for s in expected['sources'] if s['slot'] == args.get('slot', 1)), None)
            if not source or target != {'slot': source.slot, 'source_path': source.path, 'epoch': source.epoch}:
                raise ValueError('Preparation source lease or epoch changed')
        return self.app.render(**args, source_ref=source)

    def note_preparation(self, candidate):
        """Expose automatic preparation in the existing action history. No airtime."""
        with self.lock:
            record,new=self._record({'id':candidate.preparation_id,'op':'prepare',
                'args':{'scene_id':candidate.scene_id,'scene_revision':candidate.scene_revision},
                'expected':self.expected({}),'expires_at':candidate.deadline_utc},'Provider crew')
            if new:
                record['job_id']=candidate.preparation_id
                self._state(record,'Preparing')
            return copy.deepcopy(record)

    def _policy_time(self, record):
        op = record['op']
        now = time.monotonic()
        dependencies=self.app.direction.dependencies.get(record['id'],{})
        if dependencies.get('urgent_return'):
            if self.app.program.actual!='REPLAY':raise ValueError('Urgent return requires an active replay')
            return max(now,dependencies['return_not_before'])
        if op == 'replay':
            if not self.policy['replays_enabled']:
                raise ValueError('Crew replay playback is disabled by policy')
            replay = self.app.replays.get(record['args'].get('replay_id'))
            if not replay:
                raise ValueError('Replay is not ready')
            self.replay_reason(replay)
            if replay.duration > self.policy['replay_max_s']:
                raise ValueError('Replay exceeds the editorial maximum')
            now = max(now, self.last_replay + self.policy['replay_cooldown_s'])
        program = self.app.program.status()
        source = self.app.get_source(program['primary_slot'])
        source_failed = program['requested'] == 'LIVE' and program['actual'] == 'HOLDING' and (not source or source.path != program['primary_source_path'] or source.at(time.monotonic() - self.app.cfg.delay) is None)
        if op in ('live', 'replay', 'holding') and not (source_failed and op in ('live', 'holding')):
            now = max(now, self.last_shot + self.policy['minimum_shot_s'])
        return now

    def propose(self, request, *, actor='Local rehearsal', dependencies=None):
        """Trusted adapter entry: supply current expected(), expiry, and typed intent.

        Never expose actor selection through HTTP. Future providers get an application-owned label.
        """
        if actor not in ('Local rehearsal', 'Provider crew'):
            raise ValueError('Unknown trusted crew path')
        with self.lock:
            record, new = self._record(request, actor)
            if not new:
                return copy.deepcopy(record)
            if dependencies is not None:
                if actor!='Provider crew':raise ValueError('Provider dependencies require the trusted adapter')
                self.app.direction.dependencies[record['id']]=dependencies
            try:
                self._validate_args(record['op'], record['args'])
                self._fresh(record)
                if record['op'] not in AIR | {'prepare', 'cancel'}:
                    raise ValueError('Crew cannot change human authority or official facts')
                if record['op']=='replay' and self.app.foundation.registry.gemini:
                    raise ValueError('Replay is ready for operator approval; select Play replay')
                if record['op'] in ('live','holding','crop','reset_crop') and self.app.foundation.registry.gemini:
                    raise ValueError('The operator owns live view changes')
                if record['op'] == 'graphics' and record['args'].get('graphics', {}).get('op') == 'score':
                    raise ValueError('Official facts require the human confirmation form')
                if record['op'] == 'cancel':
                    job_id = record['args'].get('job_id')
                    if not any(r.get('job_id') == job_id and r['op'] == 'prepare' and r['actor'] == actor for r in self.actions.values()):
                        raise ValueError('Crew can cancel only its own preparation')
                    self.app.cancel_render(job_id)
                    self._state(record, 'Finished', 'Cancellation requested for this job')
                elif record['op'] == 'prepare':
                    if not self.policy['replays_enabled']:
                        raise ValueError('Crew replay preparation is disabled by policy')
                    if self.crew_paused:
                        raise ValueError('Crew is paused; human preparation remains available')
                    record['job_id'] = self._prepare(record)['id']
                    self._state(record, 'Preparing')
                else:
                    if self.crew_paused:
                        raise ValueError('Crew is paused; release control to allow fresh proposals')
                    record['not_before'] = self._policy_time(record)
                    if time.time()+max(0,record['not_before']-time.monotonic())>=record['expires_at']:
                        self._state(record,'Expired','opportunity_closed: earliest start is after expiry')
                    else:self._state(record, 'Scheduled')
            except (ValueError, TypeError, KeyError) as error:
                self._state(record, 'Rejected', str(error))
            return copy.deepcopy(record)

    def _poll(self):
        p = self.app.program.status()
        receipts = p.get('applied_commands', {})
        for record in self.actions.values():
            if record['op']=='commentary' and record['state'] in ('Applying','On air'):
                cue=self.app.direction.prepared.get(record['id'])
                if cue and any('first' in d for d in cue.delivered.values()) and record['state']=='Applying':
                    self._state(record,'On air','Actual media submission acknowledged')
                continue
            if record['state'] == 'Preparing':
                job = self.app.jobs.get(record['job_id'], {})
                if job.get('state') in ('ready', 'failed', 'canceled'):
                    self._state(record, 'Canceled' if job.get('canceled') else {'ready': 'Ready', 'failed': 'Failed', 'canceled': 'Canceled'}[job['state']], job.get('error'))
            elif record['state'] == 'Applying':
                receipt = receipts.get(str(record['program_revision']))
                if receipt:
                    if record.get('target') and any(receipt['target'].get(k) != v for k, v in record['target'].items()):
                        self._state(record, 'Rejected', 'Requested source was unavailable or changed before its frame applied')
                        continue
                    if record.get('cue_ids'):
                        ids = set(record['cue_ids'])
                        if not ids.intersection(c['cue_id'] for c in p['graphics']['applied']['visible']):
                            requested = {c['cue_id'] for c in p['graphics']['requested']}
                            if not ids.intersection(requested):
                                self._state(record, 'Canceled', 'Graphic cleared or expired before visible application')
                            continue
                    record['applied_at'] = receipt['monotonic_s']
                    if record['op'] == 'live':
                        self.last_shot = max(self.last_shot, receipt['monotonic_s'])
                    if record['op'] == 'replay':
                        self.last_replay = max(self.last_replay, receipt['monotonic_s'])
                        if record['actor']=='Provider crew':self.app.replay_work.mark_aired(record['args']['replay_id'])
                    if record['op'] == 'audio' or (record['op'] == 'graphics' and record['args'].get('graphics', {}).get('op') == 'score'):
                        self._state(record, 'Finished', ('Microphone muted' if record['args'].get('muted') else 'Microphone selected') if record['op'] == 'audio' else 'Official values saved')
                    else:
                        self._state(record, 'On air')
                elif p['applied_revision'] > record['program_revision']:
                    self._state(record, 'Canceled', 'Superseded before an encoder frame applied')
            elif record['state'] == 'On air':
                if record['op'] == 'replay':
                    finished = p['actual_target'].get('command_revision') != record['program_revision']
                    if finished and p['actual'] == 'LIVE':
                        returned = receipts.get(str(p['applied_revision']))
                        if returned:
                            self.last_shot = max(self.last_shot, returned['monotonic_s'])
                elif record['op'] == 'graphics' and record['args'].get('graphics', {}).get('op') == 'cue':
                    ids = set(record['cue_ids'])
                    finished = not ids.intersection(c['cue_id'] for c in p['graphics']['applied']['visible'])
                else:
                    finished = True
                if finished:
                    self._state(record, 'Finished')

    def _run(self):
        while not self.app.stop.wait(.05):
            with self.lock:
                self._poll()
                records = [r for r in self.actions.values() if r['state'] == 'Scheduled']
            for record in records:
                try:
                    # Prepare text outside locks; check all authority again at commit.
                    prepared = self.app.program.graphics.prepare(record['args'].get('graphics')) if record['op'] == 'graphics' else None
                    if record['op']=='replay' and 'play-'+record['id'] not in self.app.replay_work.tickets:
                        replay=self.app.replays.get(record['args'].get('replay_id'))
                        if replay:self.app.replay_work.preload(replay,'play-'+record['id'],record['expires_at'])
                    with self.lock, self.app.replay_context.lock:
                        if record['state'] != 'Scheduled':
                            continue
                        self._fresh(record)
                        if self.crew_paused:
                            self._state(record, 'Canceled', 'Crew is paused')
                            continue
                        due = max(record['not_before'], self._policy_time(record))
                        if time.monotonic() < due:
                            continue
                        if any(r['state'] == 'Applying' for r in self.actions.values()):
                            continue
                        if record['op']=='commentary' and self.app.program.cue is not None:continue
                        self._media(record, prepared)
                except (ValueError, TypeError, KeyError) as error:
                    with self.lock:
                        if record['state'] == 'Scheduled':
                            self._state(record, 'Expired' if 'expired' in str(error).lower() else 'Rejected', str(error))

    def invalidate_archive(self, ticket, reason):
        """Deterministic invalidation uses the same controller; no model is needed."""
        with self.lock,self.app.program.lock:
            if self.app.program.replay_ticket is not ticket:return
            source=self.app.get_source(self.app.program.slot)
            eligible=source and source.path==self.app.program.primary_source_path and source.at(time.monotonic()-self.app.cfg.delay)
            self.app.program.command('live' if eligible else 'holding',self.app.program.revision)
            self.app.program.cancel_commentary(reason)

    def finish_commentary(self, cue):
        with self.lock:
            record=self.actions.get(cue.id)
            if not record:return
            completed=any(d.get('terminal') and 'first' in d for d in cue.delivered.values())
            self._state(record,'Finished' if completed else 'Canceled',
                'Partial delivery' if cue.canceled and completed else 'Actual media delivery finished' if completed else 'Cue canceled before media delivery')

    def _start_rehearsal(self, slot):
        if self.crew_paused:
            raise ValueError('Release control before starting local rehearsal')
        source = self.app.get_source(slot)
        if not source or source.at(time.monotonic() - self.app.cfg.delay) is None:
            raise ValueError(f'Camera {slot} is unavailable')
        self._invalidate('New local rehearsal; old work canceled')
        generation = self.rehearsal['generation']
        self.rehearsal.update(state='Running', reason='', slot=slot)
        threading.Thread(target=self._rehearse, args=(generation, slot), daemon=True).start()

    def _rehearse(self, generation, slot):
        def active():
            return not self.app.stop.is_set() and self.rehearsal['generation'] == generation and self.rehearsal['state'] == 'Running'
        def step(op, args):
            with self.lock:
                if not active():
                    return None
                record = self.propose({'id': uuid.uuid4().hex, 'op': op, 'args': args,
                                      'expected': self.expected(args), 'expires_at': time.time() + 180})
                aid = record['id']
            while active():
                with self.lock:
                    record = copy.deepcopy(self.actions[aid])
                if record['state'] in TERMINAL or record['state'] == 'On air':
                    if record['state'] in ('Ready', 'Finished', 'On air'):
                        return record
                    raise ValueError(record['reason'] or record['state'])
                self.app.stop.wait(.1)
            return None
        try:
            for op, args in [('live', {'slot': slot, 'independent': True}),
                             ('graphics', {'graphics': {'op': 'cue', 'preset': 'corner-label', 'title': 'LOCAL REHEARSAL', 'subtitle': '', 'duration_s': 0}})]:
                if not step(op, args):
                    return
            # Ensure actual retained coverage, not a UI timer or invented scene interval.
            deadline = time.monotonic() + 30
            while active():
                source = self.app.get_source(slot)
                if not source:
                    raise ValueError('Rehearsal camera disappeared')
                if source.status().get('buffer_seconds', 0) >= 6 + self.app.cfg.delay:
                    break
                if time.monotonic() > deadline:
                    raise ValueError('Rehearsal camera has insufficient retained media')
                self.app.stop.wait(.1)
            if not active():
                return
            prepared = step('prepare', {'slot': slot, 'seconds': 4, 'speed': .5, 'zoom': 1})
            if not prepared or not step('graphics', {'graphics': {'op': 'clear-all'}}):
                return
            replay = step('replay', {'replay_id': prepared['job_id']})
            if not replay:
                return
            while active() and self.app.program.status()['replay_id'] == prepared['job_id']:
                self.app.stop.wait(.1)
            with self.lock:
                if active():
                    self.rehearsal.update(state='Complete', reason='Encoded replay completed; controller returned to live')
        except (ValueError, TypeError, KeyError) as error:
            with self.lock:
                if active():
                    self.rehearsal.update(state='Failed', reason=str(error))
                    self.app.log('rehearsal_failed', run_id=self.run_id, reason=str(error))

    def chat(self, aid, text, run_id=None):
        """Exact grammar only. Unsupported text never takes over."""
        if not isinstance(text, str) or len(text) > 500:
            raise ValueError('Use a command of at most 500 characters')
        if run_id is not None and run_id != self.run_id:
            with self.lock:
                record, new = self._record({'id': aid, 'op': 'chat', 'args': {'text': text}, 'expected': {'run_id': run_id}}, 'Human', text)
                if new:
                    self._state(record, 'Rejected', 'Chat belongs to a prior run')
                return copy.deepcopy(record)
        parts = text.strip().split()
        args, op = {}, None
        if len(parts) == 2 and parts[0] in ('/camera', '/stay') and parts[1].isdigit():
            op, args = 'live', {'slot': int(parts[1]), 'independent': True}
        elif parts in (['/live'], ['/hold'], ['/clear'], ['/takeover']):
            op = {'/live': 'live', '/hold': 'holding', '/clear': 'graphics', '/takeover': 'takeover'}[parts[0]]
            if op == 'graphics':
                args = {'graphics': {'op': 'clear-all'}}
        elif len(parts) == 4 and parts[0] == '/prepare':
            try:
                op, args = 'prepare', {'slot': int(parts[1]), 'seconds': float(parts[2]), 'speed': float(parts[3]), 'zoom': 1}
            except ValueError:
                pass
        elif len(parts) == 2 and parts[0] == '/play':
            op, args = 'replay', {'replay_id': parts[1]}
        elif len(parts) == 2 and parts[0] == '/audio' and parts[1].isdigit():
            op, args = 'audio', {'slot': int(parts[1])}
        elif len(parts) >= 3 and parts[0] == '/graphic':
            op, args = 'graphics', {'graphics': {'op': 'cue', 'preset': parts[1], 'title': ' '.join(parts[2:]), 'subtitle': '', 'duration_s': 8}}
        if op is None:
            reason = 'Use the documented slash commands. Speaker names need supplied text; goal identification is unavailable. Official facts need the confirmation form.'
            with self.lock:
                record, new = self._record({'id': aid, 'op': 'chat', 'args': {'text': text}}, 'Human', text)
                if new:
                    self._state(record, 'Rejected', reason)
                return copy.deepcopy(record)
        return self.submit({'id': aid, 'op': op, 'args': args}, chat_text=text)
