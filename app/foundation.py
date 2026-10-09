"""Single-event ledger, retained-media resolver, and bounded analysis coordinator.

The program controller is deliberately absent from worker and adapter interfaces.
"""
from __future__ import annotations
import asyncio
from collections import deque
from contextlib import contextmanager
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from foundation_records import (EventContext, OfficialFact, SourceEpoch, Geometry, Interval,
    ChunkManifest, DecisionSnapshot, AnalysisWindow, Observation, SceneEvent, SearchQuery, ProgramText, ArchiveResolution, SearchHit, ContextSlice, TimeMapping)
from foundation_storage import FileStorage, inspect_video
from foundation_providers import Registry, bounded_call
from foundation_worker import analyze_artifacts


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def operation_key(*values):
    return hashlib.sha256(canonical(values).encode()).hexdigest()


def validate_operation(operation):
    if not isinstance(operation,str) or not 0<len(operation)<=192:
        raise ValueError('A bounded operation key is required')


class Foundation:
    def __init__(self, runtime, run_id, settings, snapshot=None, *, readonly=False):
        self.runtime, self.run_id, self.settings = Path(runtime), run_id, settings
        self.runtime.mkdir(parents=True, exist_ok=True)
        root = Path(settings.storage_directory)
        self.storage = FileStorage(root if root.is_absolute() else self.runtime/root)
        self.registry = Registry(settings)
        self.lock = threading.RLock()
        self.readonly=readonly
        self.db = sqlite3.connect(f"file:{self.runtime/'foundation.sqlite'}?mode=ro" if readonly else self.runtime/'foundation.sqlite',
                                  uri=readonly,check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if not readonly:self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        if not readonly:self.db.executescript('''
        CREATE TABLE IF NOT EXISTS records (
            kind TEXT, id TEXT, revision INTEGER, event TEXT, run TEXT, body TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, available INTEGER NOT NULL DEFAULT 1,
            created REAL NOT NULL, PRIMARY KEY(kind,id,revision));
        CREATE TABLE IF NOT EXISTS jobs (
            key TEXT PRIMARY KEY, source TEXT, run TEXT, body TEXT, state TEXT,
            deadline REAL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS outbox (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, kind TEXT, body TEXT,
            acknowledged INTEGER NOT NULL DEFAULT 0, created REAL);
        CREATE TABLE IF NOT EXISTS pins (
            owner TEXT, chunk TEXT, deadline REAL, PRIMARY KEY(owner,chunk));
        CREATE TABLE IF NOT EXISTS evidence_versions (id TEXT PRIMARY KEY, ordinal INTEGER UNIQUE);
        CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, signature TEXT, result TEXT);
        CREATE INDEX IF NOT EXISTS records_owner ON records(kind,event,run,active);
        ''')
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.active_sources = set()
        self.pin_owners = set()
        self.threads = []
        self.snapshot_reader = snapshot
        self.last_failure = None
        self.stage_times = deque(maxlen=256)
        self.role_pending = {}
        self.role_active = set()
        self.invalidated_evidence = frozenset()
        self.current_mappings = {}
        self.unavailable_chunks = frozenset()
        self.scene_versions = {}
        self.invalidated_mappings = frozenset()
        self.consumer_cursors = {}
        if readonly:
            self._context_revision=self.db.execute("SELECT max(revision) FROM records WHERE kind='context'").fetchone()[0]
            return
        with self.transaction():
            existing = self.db.execute("SELECT DISTINCT event FROM records WHERE kind='context'").fetchall()
            if existing and {r['event'] for r in existing} != {settings.event.event_id}:
                raise ValueError('A runtime ledger supports one event')
            if not existing:
                self._put('context', settings.event.event_id, settings.event.revision, settings.event, run='')
            # Lost completion notifications are recoverable from committed job states.
            self.db.execute("UPDATE jobs SET state='queued' WHERE state='running' AND run=?", (run_id,))
            self.db.execute("UPDATE jobs SET state='expired',error='Prior run',updated=? WHERE run<>? AND state IN ('queued','running')", (time.time(),run_id))
            self.db.execute('DELETE FROM pins WHERE deadline<=?', (time.time(),))
        self.unavailable_chunks=frozenset(r['id'] for r in self.db.execute("SELECT id FROM records WHERE kind='chunk' AND available=0"))
        self.invalidated_evidence=frozenset(r['id'] for r in self.db.execute("SELECT id FROM records WHERE kind='observation' AND active=0"))
        self.scene_versions={r['id']:r['revision'] for r in self._records('scene',run=self.run_id)}

    def save_cursor(self, consumer, sequence):
        """Each consumer retains its own cursor in the existing ledger."""
        with self.transaction():
            key='cursor-'+consumer
            old=self.db.execute('SELECT result FROM operations WHERE key=?',(key,)).fetchone()
            sequence=max(sequence,json.loads(old[0]) if old else 0)
            self.db.execute('INSERT OR REPLACE INTO operations VALUES (?,?,?)',(key,'cursor',canonical(sequence)))

    def cursor(self, consumer):
        with self.lock:
            row=self.db.execute('SELECT result FROM operations WHERE key=?',('cursor-'+consumer,)).fetchone()
            return json.loads(row[0]) if row else 0

    def register_mapping(self, mapping):
        mapping=TimeMapping.model_validate(mapping)
        self.register_source(mapping.source)
        if not .98<=mapping.rate_correction<=1.02:
            raise ValueError('Excessive calibration drift')
        key=operation_key(mapping.source.model_dump())
        with self.transaction():
            old=self.db.execute("SELECT body FROM records WHERE kind='mapping' AND id=? AND revision=?",(key,mapping.revision)).fetchone()
            if old:
                if TimeMapping.model_validate_json(old[0])!=mapping:raise ValueError('Mapping revision changed content')
                return mapping
            latest=self.db.execute("SELECT max(revision) FROM records WHERE kind='mapping' AND id=?",(key,)).fetchone()[0] or 0
            if mapping.revision!=latest+1:raise ValueError('Mapping revision must advance by one')
            self._put('mapping',key,mapping.revision,mapping,run=mapping.source.run_id)
        return mapping

    def mapping(self, source, revision):
        key=operation_key(source.model_dump())
        if (key,revision) in self.invalidated_mappings:raise ValueError('mapping_invalid')
        with self.lock:
            row=self.db.execute("SELECT body FROM records WHERE kind='mapping' AND id=? AND revision=? AND active=1",(key,revision)).fetchone()
            if row:return TimeMapping.model_validate_json(row[0])
            embedded=[m.event_mapping for m in self.chunks(source) if m.event_mapping and m.event_mapping.revision==revision]
            if embedded and all(m==embedded[0] for m in embedded):return embedded[0]
        raise ValueError('mapping_unknown')

    def invalidate_mapping(self, source, revision, reason):
        key=operation_key(source.model_dump())
        with self.transaction():
            self.db.execute("UPDATE records SET active=0 WHERE kind='mapping' AND id=? AND revision=?",(key,revision))
            self.invalidated_mappings=self.invalidated_mappings | {(key,revision)}
            self._notify('mapping.invalidated',key+'-'+str(revision),{'source':source.model_dump(),'revision':revision,'reason':reason})

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.commit()
                row=self.db.execute("SELECT max(revision) FROM records WHERE kind='context'").fetchone()
                self._context_revision=row[0] if row and row[0] else 1
            except BaseException:
                self.db.rollback(); raise

    def _put(self, kind, rid, revision, record, run=None, active=True):
        body = record.model_dump(mode='json') if hasattr(record,'model_dump') else record
        event = body.get('event_id', body.get('source',{}).get('event_id',self.settings.event.event_id))
        if self.db.execute('SELECT count(*) FROM records').fetchone()[0] >= self.settings.limits.ledger_records:
            raise ValueError('Ledger capacity reached; archive admission is degraded')
        self.db.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)',
            (kind,rid,revision,event,self.run_id if run is None else run,canonical(body),int(active),1,time.time()))

    def _notify(self, kind, key, body):
        if self.db.execute('SELECT count(*) FROM outbox').fetchone()[0] >= self.settings.limits.ledger_records:
            raise ValueError('Notification capacity reached; archive admission is degraded')
        self.db.execute('INSERT OR IGNORE INTO outbox (key,kind,body,created) VALUES (?,?,?,?)',
                        (key,kind,canonical(body),time.time()))

    def _records(self, kind, *, current=True, run=None):
        sql = 'SELECT * FROM records WHERE kind=? AND event=?'
        args = [kind,self.settings.event.event_id]
        if current: sql += ' AND active=1'
        if run is not None: sql += ' AND run=?'; args.append(run)
        return self.db.execute(sql+' ORDER BY created,revision', args).fetchall()

    @property
    def context_revision(self):
        # Published only after a successful transaction. Controls never wait on archive hashing.
        return self._context_revision

    def event_context(self):
        with self.lock:
            row = self._records('context')[-1]
            return EventContext.model_validate_json(row['body'])

    def update_context(self, context, expected_revision, operation):
        validate_operation(operation)
        context = EventContext.model_validate(context)
        signature = operation_key(context.model_dump(),expected_revision)
        with self.transaction():
            old = self.db.execute('SELECT * FROM operations WHERE key=?',(operation,)).fetchone()
            if old:
                if old['signature'] != signature: raise ValueError('Operation key reused with changed context')
                return json.loads(old['result'])
            revision = self.context_revision
            if type(expected_revision) is not int or expected_revision != revision: raise ValueError('Context revision changed')
            if context.event_id != self.settings.event.event_id or context.revision != revision+1:
                raise ValueError('Event ownership or context revision is invalid')
            self.db.execute("UPDATE records SET active=0 WHERE kind='context'")
            self._put('context', context.event_id,context.revision,context,run='')
            result=context.model_dump(mode='json')
            self.db.execute('INSERT INTO operations VALUES (?,?,?)',(operation,signature,canonical(result)))
            self._notify('context.invalidated',operation,{'context_revision':context.revision})
        return result

    def confirm_facts(self, values, expected_revision, operation, *, authority, effective_event_ms=None):
        if authority != 'human': raise PermissionError('Only the human confirmation path owns official facts')
        validate_operation(operation)
        allowed = {'home','away','home_score','away_score','period','clock_seconds','clock_running','session'}
        if not isinstance(values,dict) or set(values)-allowed: raise ValueError('Unsupported official fact')
        if type(expected_revision) is not int: raise ValueError('Expected context revision must be an integer')
        for name,value in values.items():
            if name in ('home_score','away_score','clock_seconds') and value is not None:
                if type(value) is not int or not 0<=value<=(359999 if name=='clock_seconds' else 999):raise ValueError('Official numeric fact is invalid')
            if name=='clock_running' and type(value) is not bool:raise ValueError('Official running clock must be boolean')
            if name in ('home','away','period','session') and value is not None and (not isinstance(value,str) or len(value)>256):raise ValueError('Official text fact is invalid')
        signature=operation_key(values,expected_revision,effective_event_ms)
        with self.transaction():
            old=self.db.execute('SELECT * FROM operations WHERE key=?',(operation,)).fetchone()
            if old:
                if old['signature']!=signature: raise ValueError('Operation key reused with changed facts')
                return json.loads(old['result'])
            if self.context_revision!=expected_revision: raise ValueError('Context revision changed')
            revision=expected_revision+1
            context=self.event_context().model_copy(update={'revision':revision})
            self.db.execute("UPDATE records SET active=0 WHERE kind='context'")
            self._put('context',context.event_id,revision,context,run='')
            for name,value in values.items():
                fact=OfficialFact(event_id=context.event_id,revision=revision,name=name,value=None if value=='' else value,
                                  operation_key=operation,effective_event_ms=effective_event_ms)
                self.db.execute("UPDATE records SET active=0 WHERE kind='fact' AND id=?",(name,))
                self._put('fact',name,revision,fact,run='')
            result={'context_revision':revision,'values':values}
            self.db.execute('INSERT INTO operations VALUES (?,?,?)',(operation,signature,canonical(result)))
            self._notify('context.invalidated',operation,{'context_revision':revision,'facts':list(values)})
            return result

    def register_source(self, source):
        source=SourceEpoch.model_validate(source)
        if source.event_id!=self.settings.event.event_id: raise ValueError('Wrong event')
        key=operation_key(source.event_id,source.run_id,source.source_id,source.epoch)
        with self.transaction():
            row=self.db.execute("SELECT body FROM records WHERE kind='source' AND id=?",(key,)).fetchone()
            if row:
                if json.loads(row['body'])!=source.model_dump(): raise ValueError('Source epoch cannot change')
            else: self._put('source',key,1,source,run=source.run_id)
        return source

    def reviewed_snapshot(self):
        with self.lock:
            evidence_revision=self.db.execute("SELECT COALESCE(max(ordinal),0) FROM evidence_versions").fetchone()[0]
            sources=[SourceEpoch.model_validate_json(r['body']) for r in self._records('source',run=self.run_id)]
            # Multiple epochs are historical; the snapshot pins only latest source epochs.
            current={s.slot:s for s in sources}
            base=self.snapshot_reader() if self.snapshot_reader else {}
            if base.get('runtime') is not None:
                live={h['slot']:(h['source_id'],h['epoch']) for h in base['runtime']['source_health']}
                current={s.slot:s for s in sources if live.get(s.slot)==(s.source_id,s.epoch)}
            return DecisionSnapshot(event_id=self.settings.event.event_id,run_id=self.run_id,
                context_revision=self.context_revision,configuration_revision=self.settings.configuration_revision,
                evidence_revision=evidence_revision,program_revision=base.get('program_revision',0),
                control_revision=base.get('control_revision',0),sources=list(current.values())[-5:],mapping_revisions=base.get('mapping_revisions',{}),
                runtime=base.get('runtime'),decoder_revisions=base.get('decoder_revisions',{}))

    def finalize(self,path,source,sequence,last_receipt_utc,*,closed,provenance='sample',geometry=None,mapping=None,timeline_offset_pts=0,notification_utc=None,receipt_uncertainty_ms=0.0):
        self.registry.require('storage')
        if closed is not True: raise ValueError('A completion notification is required')
        self.register_source(source)
        chunk_id=operation_key(source.model_dump(),sequence)
        with self.lock:
            prior=self.db.execute("SELECT body FROM records WHERE kind='chunk' AND id=?",(chunk_id,)).fetchone()
            if prior:
                prior=ChunkManifest.model_validate_json(prior['body'])
                with open(path,'rb') as raw:incoming_hash=hashlib.file_digest(raw,'sha256').hexdigest()
                if (incoming_hash!=prior.media.sha256 or prior.provenance!=provenance or prior.timeline_offset_pts!=timeline_offset_pts or
                    (geometry is not None and geometry!=prior.geometry) or (mapping is not None and mapping!=prior.event_mapping)):
                    raise ValueError('Chunk identity reused for changed bytes or provenance')
                self.storage.inspect(prior.media);self.storage.inspect_manifest(prior)
                return prior
        started=time.time()
        with self.lock:
            if self.db.execute("SELECT count(*) FROM records").fetchone()[0]>=self.settings.limits.ledger_records-16:
                raise ValueError("Ledger capacity reached; archive admission is degraded")
        limits=self.settings.limits
        size=Path(path).stat().st_size
        pending=sum(p.stat().st_size for p in (self.runtime/'recordings').rglob('*.mp4'))
        if size>limits.pending_bytes or pending+size>limits.archive_bytes or self.storage.free_bytes()-size<limits.disk_reserve_bytes:
            self.gap(source,None,'Storage pressure; new archive work rejected')
            raise ValueError('Archive storage or disk reserve limit reached')
        self.cleanup()
        with self.lock:
            retained=sum(json.loads(r['body'])['media']['size'] for r in self._records('chunk') if r['available'])
            if retained+pending+size>limits.archive_bytes:
                raise ValueError('Pinned archive prevents new media admission')
        info=inspect_video(path)
        if info['time_base']!=source.time_base: raise ValueError('Native time base differs from registered source')
        # Publication and retention share one lock, including shared content keys.
        with self.lock:
            retained=sum(json.loads(r['body'])['media']['size'] for r in self._records('chunk') if r['available'])
            if retained+pending+size>limits.archive_bytes or self.storage.free_bytes()-size<limits.disk_reserve_bytes:
                raise ValueError('Archive capacity changed before publication')
            artifact=self.storage.put(path,info['format'],info['frames'])
            geometry=geometry or Geometry(native_width=info['width'],native_height=info['height'],output_width=info['width'],
                                          output_height=info['height'],scaled_width=info['width'],scaled_height=info['height'])
            manifest=ChunkManifest(chunk_id=chunk_id,source=source,sequence=sequence,
                native=Interval(start=info['start']+timeline_offset_pts,end=info['end']+timeline_offset_pts),
                file_native=Interval(start=info['start'],end=info['end']),timeline_offset_pts=timeline_offset_pts,media=artifact,geometry=geometry,event_mapping=mapping,
                last_receipt_utc=last_receipt_utc,notification_utc=notification_utc,
                receipt_basis='frame-receipt' if last_receipt_utc is not None else 'recording-notification',
                receipt_uncertainty_ms=receipt_uncertainty_ms,
                finalized_utc=time.time(),configuration_revision=self.settings.configuration_revision,
                provenance=provenance)
            manifest_reference=self.storage.publish_manifest(manifest)
            with self.transaction():
                row=self.db.execute("SELECT body FROM records WHERE kind='chunk' AND id=?",(chunk_id,)).fetchone()
                if row:
                    existing=ChunkManifest.model_validate_json(row['body'])
                    if existing.media!=artifact or existing.native!=manifest.native or existing.geometry!=manifest.geometry:
                        raise ValueError('Chunk identity reused for changed bytes or provenance')
                    return existing
                self._put('chunk',chunk_id,1,manifest,run=source.run_id)
                self._notify('chunk.ready',chunk_id,{'manifest_ref':manifest_reference,'manifest':manifest.model_dump(mode='json')})
            if mapping:self.current_mappings={**self.current_mappings,source.source_id:mapping}
        self.stage_times.append({'trace_id':chunk_id,'stage':'finalization','seconds':time.time()-started})
        self.cleanup()
        return manifest

    def chunks(self, source, interval=None):
        with self.lock:
            result=[]
            for row in self._records('chunk',run=source.run_id):
                manifest=ChunkManifest.model_validate_json(row['body'])
                if manifest.source!=source: continue
                if interval and (manifest.native.end<=interval.start or manifest.native.start>=interval.end): continue
                if not row['available']:
                    if interval is not None:raise ValueError('Retained media was deleted')
                    continue
                self.storage.inspect(manifest.media)
                self.storage.inspect_manifest(manifest)
                result.append(manifest)
            return sorted(result,key=lambda m:m.native.start)

    def resolve(self, source, interval, *, mapping_revision=None, owner=None, deadline_utc=None):
        interval=Interval.model_validate(interval) if isinstance(interval,dict) else interval
        with self.transaction():
            chunks=self.chunks(source,interval)
            position=interval.start
            for chunk in chunks:
                if chunk.native.start>position: raise ValueError('Retained interval has a gap')
                if mapping_revision is not None and (not chunk.event_mapping or chunk.event_mapping.revision!=mapping_revision):
                    mapping=self.mapping(source,mapping_revision)
                    if not mapping.valid.start<=interval.start<interval.end<=mapping.valid.end:
                        raise ValueError('mapping_invalid')
                position=max(position,chunk.native.end)
            if position<interval.end: raise ValueError('Retained interval is unavailable or unfinalized')
            if len(chunks)>16:raise ValueError('Retained interval exceeds resolver chunk limit')
            if owner:
                if not isinstance(owner,str) or not 0<len(owner)<=192: raise ValueError('A bounded pin owner is required')
                now=time.time()
                if deadline_utc is None or not now<deadline_utc<=now+self.settings.limits.pin_seconds:
                    raise ValueError('Pin deadline exceeds configured limit')
                for chunk in chunks:
                    self.db.execute('INSERT OR REPLACE INTO pins VALUES (?,?,?)',(owner,chunk.chunk_id,deadline_utc))
                self.pin_owners.add(owner)
            return ArchiveResolution(source=source,native=interval,mapping_revision=mapping_revision,
                chunks=[{'manifest':m,'path':str(self.storage.inspect(m.media))} for m in chunks]).model_dump(mode='json')

    def release(self, owner):
        with self.transaction(): self.db.execute('DELETE FROM pins WHERE owner=?',(owner,))
        self.pin_owners.discard(owner)

    def cleanup(self, now=None):
        now=time.time() if now is None else now
        with self.transaction():
            self.db.execute('DELETE FROM pins WHERE deadline<=?',(now,))
            rows=self.db.execute("SELECT * FROM records WHERE kind='chunk' AND available=1 ORDER BY created").fetchall()
            total=sum(json.loads(r['body'])['media']['size'] for r in rows)
            limits=self.settings.limits
            for row in rows:
                if now-row['created']<=limits.archive_seconds and total<=limits.archive_bytes and self.storage.free_bytes()>=limits.disk_reserve_bytes:
                    continue
                if self.db.execute('SELECT 1 FROM pins WHERE chunk=?',(row['id'],)).fetchone(): continue
                # Work retains its own pin until its original deadline.
                m=ChunkManifest.model_validate_json(row['body'])
                self.db.execute("UPDATE records SET available=0 WHERE kind='chunk' AND id=?",(row['id'],))
                for index in self._records('index',current=True):
                    if row['id'] in json.loads(index['body'])['chunk_ids']:
                        self.db.execute("UPDATE records SET active=0 WHERE kind='index' AND id=?",(index['id'],))
                self._notify('media.unavailable','delete-'+row['id'],{'chunk_id':row['id']})
                self.unavailable_chunks=self.unavailable_chunks | {row['id']}
                total-=m.media.size
            # Availability commits before deleting. Shared content is deleted only after its last reference.
        with self.lock:
            dead=self.db.execute("SELECT body FROM records WHERE kind='chunk' AND available=0").fetchall()
            alive={json.loads(r['body'])['media']['key'] for r in self.db.execute("SELECT body FROM records WHERE kind='chunk' AND available=1")}
            for row in dead:
                m=ChunkManifest.model_validate_json(row['body'])
                if m.media.key not in alive:self.storage.delete(m.media)
            self.db.execute('PRAGMA wal_checkpoint(PASSIVE)')

    def gap(self,source,interval,reason):
        body={'source':source.model_dump(),'native':interval.model_dump() if interval else None,'reason':reason}
        key=operation_key(body)
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM records WHERE kind='gap' AND id=?",(key,)).fetchone():
                self._put('gap',key,1,body,run=source.run_id)
        self.last_failure=reason

    def window(self, manifest, snapshot=None):
        """Build a supported overlapping window. Coverage never crosses an epoch or gap."""
        source=manifest.source
        chunks=self.chunks(source)
        if not chunks:raise ValueError('No readable finalized coverage')
        end=manifest.native.end
        width=round(self.settings.limits.window_s/float(Fraction(source.time_base)))
        start=max(chunks[0].native.start,end-width)
        # Walk back only through continuous finalized coverage.
        selected=[c for c in chunks if c.native.end>start and c.native.start<end]
        for a,b in zip(selected,selected[1:]):
            if b.native.start>a.native.end:
                self.gap(source,Interval(start=a.native.end,end=b.native.start),'Missing finalized chunk')
                start=b.native.start
        selected=[c for c in selected if c.native.end>start]
        interval=Interval(start=start,end=end)
        snapshot=snapshot or self.reviewed_snapshot()
        key=operation_key(source.event_id,source.run_id,source.source_id,source.epoch,interval.model_dump(),'analysis',self.settings.configuration_revision)
        # Closure, storage, and queue time all consume this first receipt-based deadline.
        return AnalysisWindow(job_key=key,source=source,native=interval,chunk_ids=[c.chunk_id for c in selected],
            snapshot=snapshot,deadline_utc=(manifest.last_receipt_utc if manifest.last_receipt_utc is not None else manifest.notification_utc)+self.settings.limits.live_deadline_s-manifest.receipt_uncertainty_ms/1000,
            deadline_basis=manifest.receipt_basis,
            created_utc=time.time(),trace_id=key)

    def enqueue(self, window):
        self.registry.require('jobs')
        window=AnalysisWindow.model_validate(window)
        if window.source.run_id!=self.run_id: raise ValueError('Prior-run job cannot enter current analysis')
        with self.transaction():
            row=self.db.execute('SELECT * FROM jobs WHERE key=?',(window.job_key,)).fetchone()
            if row:
                old=AnalysisWindow.model_validate_json(row['body'])
                if old.source!=window.source or old.native!=window.native or old.chunk_ids!=window.chunk_ids:
                    raise ValueError('Logical job key changed contents')
                return row['state']
            if self.db.execute('SELECT count(*) FROM jobs').fetchone()[0]>=self.settings.limits.ledger_records:raise ValueError('Job capacity reached')
            src=operation_key(window.source.model_dump())
            latest=self.db.execute("SELECT body FROM jobs WHERE source=? ORDER BY json_extract(body,'$.native.end') DESC LIMIT 1",(src,)).fetchone()
            if latest:
                previous=AnalysisWindow.model_validate_json(latest['body'])
                elapsed=(window.native.end-previous.native.end)*float(Fraction(window.source.time_base))
                if 0<elapsed<self.settings.limits.step_s:
                    return 'waiting-step'
                if window.native.start>previous.native.end:
                    # This is an analysis gap, even when the original media still exists.
                    body={'source':window.source.model_dump(),'native':{'start':previous.native.end,'end':window.native.start},
                          'reason':'Analysis coverage omitted before newer finalized window'}
                    key=operation_key(body)
                    if not self.db.execute("SELECT 1 FROM records WHERE kind='gap' AND id=?",(key,)).fetchone():
                        self._put('gap',key,1,body,run=window.source.run_id)
            pending=self.db.execute("SELECT key,body FROM jobs WHERE source=? AND state='queued'",(src,)).fetchall()
            for row in pending:
                old=AnalysisWindow.model_validate_json(row['body'])
                if old.native.end>=window.native.end:
                    self._notify('analysis.skipped',window.job_key,{'window':window.model_dump(),'reason':'Older pending window'})
                    return 'skipped'
                self.db.execute("UPDATE jobs SET state='skipped',error='Superseded pending interval' WHERE key=?",(row['key'],))
                self._notify('analysis.skipped',row['key'],{'window':old.model_dump(),'reason':'Superseded pending interval'})
            state='expired' if window.deadline_utc<=time.time() else 'queued'
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,0,?,?)',
                (window.job_key,src,self.run_id,window.model_dump_json(),state,window.deadline_utc,
                 'Original deadline expired' if state=='expired' else None,time.time()))
        self.wake.set()
        return state

    def _validate_observation(self, observation,window):
        if (observation.job_key!=window.job_key or observation.source!=window.source or observation.snapshot!=window.snapshot or
            observation.configuration_revision!=window.snapshot.configuration_revision or observation.chunk_ids!=window.chunk_ids or
            observation.native.start<window.native.start or observation.native.end>window.native.end):
            raise ValueError('Provider output exceeds issued window or changes reviewed snapshot')
        if observation.origin=='fixture':
            expected_model='labeled-cosmos-fixture'
            expected_version=self.registry.labels['version']
        else:
            provider=self.registry.require('cosmos')
            expected_model,expected_version=provider.model_id,provider.version
        if observation.model_id!=expected_model or observation.model_version!=expected_version:
            raise ValueError('Provider model or version differs from issued configuration')
        self.resolve(window.source,observation.native)
        for detection in observation.detections:
            if not observation.native.start<=detection.pts<observation.native.end:
                raise ValueError('Detection is outside supporting interval')
        if observation.view and not observation.native.start<=observation.view.native.start<observation.view.native.end<=observation.native.end:
            raise ValueError('View assessment exceeds inspected native interval')

    def ingest(self, window, results, *, trusted_origin):
        began=time.time()
        if trusted_origin not in ('fixture','provider'): raise ValueError('Unknown trusted adapter origin')
        if window.source.event_id!=self.settings.event.event_id: raise ValueError('Wrong event')
        observations=[Observation.model_validate(r) for r in results]
        for observation in observations:
            if observation.origin!=trusted_origin: raise ValueError('Adapter origin cannot be set by returned evidence')
            self._validate_observation(observation,window)
        if len(observations)>256:raise ValueError('Provider result count exceeds window limit')
        with self.lock:
            issued=self.db.execute('SELECT body FROM jobs WHERE key=?',(window.job_key,)).fetchone()
            if not issued or AnalysisWindow.model_validate_json(issued['body'])!=window:
                raise ValueError('Result has no matching issued request')
        signature=operation_key([o.model_dump() for o in observations])
        self.stage_times.append({'trace_id':window.trace_id,'stage':'validation','seconds':time.time()-began})
        persisted=time.time()
        with self.transaction():
            row=self.db.execute('SELECT * FROM operations WHERE key=?',(window.job_key,)).fetchone()
            if row:
                if row['signature']!=signature: raise ValueError('Duplicate result changed contents')
                return json.loads(row['result'])
            for observation in observations:
                self._put('observation',observation.evidence_id,1,observation,run=window.source.run_id)
                self.db.execute('INSERT INTO evidence_versions SELECT ?,COALESCE(max(ordinal),0)+1 FROM evidence_versions',(observation.evidence_id,))
                scene_key=operation_key(observation.source.model_dump(),observation.association_key or observation.evidence_id)
                previous=self.db.execute("SELECT * FROM records WHERE kind='scene' AND id=? AND active=1",(scene_key,)).fetchone()
                old=SceneEvent.model_validate_json(previous['body']) if previous else None
                evidence=old.evidence_ids+[observation.evidence_id] if old else [observation.evidence_id]
                native=Interval(start=min(old.native.start,observation.native.start),end=max(old.native.end,observation.native.end)) if old else observation.native
                # Older evidence extends history without replacing a newer description.
                newest=not old or observation.native.end>=old.native.end
                scene=SceneEvent(scene_id=scene_key,revision=old.revision+1 if old else 1,source=observation.source,
                    native=native,evidence_ids=evidence,description=observation.description if newest else old.description,
                    uncertainty=max(old.uncertainty,observation.uncertainty) if old else observation.uncertainty,origin=trusted_origin)
                self.db.execute("UPDATE records SET active=0 WHERE kind IN ('scene','index') AND id=?",(scene_key,))
                self._put('scene',scene_key,scene.revision,scene,run=window.source.run_id)
                self._put('index',scene_key,scene.revision,{'event_id':scene.source.event_id,'scene':scene.model_dump(),
                    'chunk_ids':sorted(set(c for eid in evidence for c in self._observation(eid).chunk_ids)),
                    'index_version':'fixture-index-1' if trusted_origin=='fixture' else self.settings.providers['search'].version,
                    'embedding_version':'fixture-labels-1' if trusted_origin=='fixture' else self.settings.providers['search'].model_id,
                    'watermark':scene.native.end,'ranking':'simulated' if trusted_origin=='fixture' else 'provider'},run=window.source.run_id)
                self._notify('scene.updated',scene_key+'-'+str(scene.revision),{'scene_id':scene_key,'revision':scene.revision})
            current=self.reviewed_snapshot()
            result={'job_key':window.job_key,'evidence_ids':[o.evidence_id for o in observations],
                    'live_eligible':window.deadline_basis=='frame-receipt' and time.time()<window.deadline_utc and window.source.run_id==self.run_id
                        and window.source in current.sources
                        and window.snapshot.context_revision==current.context_revision
                        and window.snapshot.configuration_revision==current.configuration_revision
                        and window.snapshot.program_revision==current.program_revision
                        and window.snapshot.control_revision==current.control_revision}
            self.db.execute('INSERT INTO operations VALUES (?,?,?)',(window.job_key,signature,canonical(result)))
            self.db.execute("UPDATE jobs SET state='completed',updated=? WHERE key=?",(time.time(),window.job_key))
            self._notify('analysis.completed',window.job_key,result)
        self.stage_times.append({'trace_id':window.trace_id,'stage':'persistence-and-index','seconds':time.time()-persisted})
        self.scene_versions={r['id']:r['revision'] for r in self._records('scene',run=self.run_id)}
        return result

    def _observation(self,eid):
        row=self.db.execute("SELECT body FROM records WHERE kind='observation' AND id=?",(eid,)).fetchone()
        if not row: raise ValueError('Evidence is unavailable')
        return Observation.model_validate_json(row['body'])

    def retract(self,evidence_id,operation,reason):
        validate_operation(operation)
        if not isinstance(reason,str) or not 0<len(reason)<=2048:raise ValueError('A bounded correction reason is required')
        with self.transaction():
            old=self.db.execute("SELECT body FROM records WHERE kind='correction' AND id=?",(operation,)).fetchone()
            body={'evidence_id':evidence_id,'reason':reason}
            if old:
                if json.loads(old['body'])!=body: raise ValueError('Correction key changed contents')
                return
            self._observation(evidence_id)
            self.db.execute("UPDATE records SET active=0 WHERE kind='observation' AND id=?",(evidence_id,))
            self._put('correction',operation,1,body)
            self.db.execute('INSERT INTO evidence_versions SELECT ?,COALESCE(max(ordinal),0)+1 FROM evidence_versions',(operation,))
            for row in self._records('scene'):
                old_scene=SceneEvent.model_validate_json(row['body'])
                if evidence_id not in old_scene.evidence_ids: continue
                scene=old_scene.model_copy(update={'revision':old_scene.revision+1,'status':'retracted'})
                self.db.execute("UPDATE records SET active=0 WHERE kind IN ('scene','index') AND id=?",(scene.scene_id,))
                self._put('scene',scene.scene_id,scene.revision,scene,run=scene.source.run_id)
                self._notify('evidence.invalidated',operation+'-'+scene.scene_id,{'evidence_id':evidence_id,'scene_id':scene.scene_id,'reason':reason})
        # Published after commit. Media checks this immutable set without a database read.
        self.invalidated_evidence=self.invalidated_evidence | {evidence_id}
        self.scene_versions={r['id']:r['revision'] for r in self._records('scene',run=self.run_id)}

    def notifications(self,after=0,limit=100):
        if type(limit) is not int or not 1<=limit<=256: raise ValueError('Notification limit must be bounded')
        with self.lock:
            return [dict(sequence=r['sequence'],key=r['key'],kind=r['kind'],body=json.loads(r['body']))
                for r in self.db.execute('SELECT * FROM outbox WHERE sequence>? ORDER BY sequence LIMIT ?',(after,limit))]

    def acknowledge(self,sequence):
        with self.transaction(): self.db.execute('UPDATE outbox SET acknowledged=1 WHERE sequence=?',(sequence,))

    def search(self,query,*,deadline_utc=None):
        query=SearchQuery.model_validate(query) if isinstance(query,dict) else query
        self.registry.require('search')
        if query.event_id!=self.settings.event.event_id: return []
        deadline_utc=deadline_utc or time.time()+self.settings.replay.query_s
        with self.lock:entries=[json.loads(row['body']) for row in self._records('index',run=query.run_id)]
        ranked=asyncio.run(self.registry.query(query,entries,deadline_utc))
        scores=dict(ranked)
        hits=[]
        with self.lock:
            for entry in entries:
                scene=SceneEvent.model_validate(entry['scene'])
                if scene.scene_id not in scores:continue
                if entry['index_version']!=query.index_version or entry['embedding_version']!=query.embedding_version or scene.status=='retracted':continue
                if query.eligible and (scene.native.start<query.eligible.start or scene.native.end>query.eligible.end):continue
                if any(not self.db.execute("SELECT 1 FROM records WHERE kind='observation' AND id=? AND active=1",(eid,)).fetchone() for eid in scene.evidence_ids):continue
                try: media=self.resolve(scene.source,scene.native)
                except ValueError: continue
                if time.time()>=deadline_utc:raise TimeoutError('Search deadline expired')
                score=scores[scene.scene_id]
                hits.append(SearchHit(scene=scene,score=float(score),ranking=entry['ranking'],index_version=entry['index_version'],
                    embedding_version=entry['embedding_version'],watermark=entry['watermark'],media=media).model_dump(mode='json'))
            return sorted(hits,key=lambda h:(-h['score'],h['scene']['scene_id']))[:query.limit]

    def program_text(self,text,*,owner,reserve=False):
        text=ProgramText.model_validate(text) if isinstance(text,dict) else text
        if owner not in ('controller','fixture') or text.origin!=owner: raise PermissionError('Program history belongs to controller')
        if text.event_id!=self.settings.event.event_id or text.run_id!=self.run_id: raise ValueError('Wrong program history owner')
        with self.transaction():
            if reserve:
                if text.channel!='intent' or text.state!='pending':raise ValueError('Commentary reservation requires pending intent')
                for row in self._records('program_text',run=self.run_id):
                    other=ProgramText.model_validate_json(row['body'])
                    if other.cue_id!=text.cue_id and other.text==text.text and other.state in ('prepared','pending','started','aired','completed','interrupted'):
                        raise ValueError('Commentary repeats pending or delivered text')
            rows=self.db.execute("SELECT * FROM records WHERE kind='program_text' AND id=? ORDER BY revision",(text.cue_id,)).fetchall()
            if rows:
                previous=ProgramText.model_validate_json(rows[-1]['body'])
                if previous==text:return
                transitions = {'prepared': {'pending','canceled','expired'},
                    'pending': {'started','canceled','expired','aired'},
                    'started': {'completed','interrupted'}}
                if text.state not in transitions.get(previous.state,set()):
                    raise ValueError('Aired/canceled history is immutable')
                stable=('cue_id','event_id','run_id','text','event_ms','program_revision','evidence_ids','origin','channel','session_id')
                if any(getattr(previous,key)!=getattr(text,key) for key in stable):
                    raise ValueError('Cue identity and text cannot change during delivery')
            self.db.execute("UPDATE records SET active=0 WHERE kind='program_text' AND id=?",(text.cue_id,))
            self._put('program_text',text.cue_id,len(rows)+1,text)

    def context(self,role,source,eligible,snapshot,*,event_ms=None,archive_hits=None,archive=False):
        if role not in ('director','commentator','segmentor'):raise ValueError('Unknown role')
        if snapshot.event_id!=self.settings.event.event_id or snapshot.run_id!=self.run_id or source.run_id!=self.run_id:
            raise ValueError('Context belongs to a prior run or different event')
        if source not in snapshot.sources:
            if not archive:raise ValueError('Source is absent from reviewed snapshot')
            with self.lock:
                if not any(SourceEpoch.model_validate_json(r['body'])==source for r in self._records('source',run=self.run_id)):
                    raise ValueError('Original archive source is unavailable')
        limits=self.settings.limits
        result={'role':role,'snapshot':snapshot.model_dump(),'target':{'source':source.model_dump(),'native':eligible.model_dump() if isinstance(eligible,Interval) else eligible,'event_ms':event_ms},'event':{},'facts':{},'observations':[], 'scenes':[],
                'aired':[],'pending':[],'archive_hits':[],'omitted':[],'status':'ready','timing':'source-local'}
        if eligible is None:
            result.update(status='unavailable',timing='unknown');return ContextSlice.model_validate(result).model_dump(mode='json')
        if not isinstance(eligible,Interval):eligible=Interval.model_validate(eligible)
        if role=='director' and snapshot.runtime is None:
            result.update(status='unavailable',omitted=['Reviewed runtime state is unavailable'])
        with self.lock:
            row=self.db.execute("SELECT body FROM records WHERE kind='context' AND revision=?",(snapshot.context_revision,)).fetchone()
            if not row:raise ValueError('Reviewed context revision is unavailable')
            result['event']=json.loads(row['body'])
            cutoff=eligible.start-round(limits.context_seconds/float(Fraction(source.time_base)))
            observations=[]
            for row in self._records('observation',run=self.run_id):
                obs=Observation.model_validate_json(row['body'])
                if obs.source!=source or obs.native.start<cutoff:continue
                if role=='commentator' and obs.native.end>eligible.end:continue
                # Expired jobs stay inspectable, outside live director context.
                job=self.db.execute('SELECT deadline,body FROM jobs WHERE key=?',(obs.job_key,)).fetchone()
                if role=='director' and (not job or job['deadline']<=time.time() or json.loads(job['body']).get('deadline_basis')!='frame-receipt'):continue
                ordinal=self.db.execute("SELECT ordinal FROM evidence_versions WHERE id=?",(obs.evidence_id,)).fetchone()[0]
                if ordinal>snapshot.evidence_revision:continue
                observations.append(obs)
            observations=sorted(observations,key=lambda o:o.native.end,reverse=True)
            result['omitted'] += [o.evidence_id for o in observations[limits.context_records:]]
            kept=observations[:limits.context_records]
            result['observations']=[o.model_dump() for o in kept]
            if archive:
                from foundation_records import TimeMapping
                key=operation_key(source.model_dump())
                mappings=[TimeMapping.model_validate_json(r['body']) for r in self.db.execute(
                    "SELECT body FROM records WHERE kind='mapping' AND id=? AND active=1 ORDER BY revision DESC",(key,))]
                mapping=next((m for m in mappings if m.valid.start<=eligible.start<eligible.end<=m.valid.end and
                    (key,m.revision) not in self.invalidated_mappings),None)
                if mapping is None:
                    mapping=next((chunk.event_mapping for chunk in self.chunks(source,eligible) if chunk.event_mapping and
                        chunk.event_mapping.valid.start<=eligible.start<eligible.end<=chunk.event_mapping.valid.end and
                        (key,chunk.event_mapping.revision) not in self.invalidated_mappings),None)
                if mapping:result['target']['archive_mapping']=mapping.model_dump(mode='json')
            # Reconstruct claims from eligible evidence rather than a scene's later description.
            for row in self._records('scene',run=self.run_id):
                scene=SceneEvent.model_validate_json(row['body'])
                if scene.status=='retracted' or scene.source!=source:continue
                supported=[o for o in kept if o.evidence_id in scene.evidence_ids]
                if not supported:continue
                latest=max(supported,key=lambda o:o.native.end)
                historical=self.db.execute("SELECT body FROM records WHERE kind='scene' AND id=? ORDER BY revision DESC",(scene.scene_id,)).fetchall()
                revision=scene.revision
                for candidate in historical:
                    earlier=SceneEvent.model_validate_json(candidate['body'])
                    if set(earlier.evidence_ids).issubset({o.evidence_id for o in supported}):
                        revision=earlier.revision;break
                result['scenes'].append({**scene.model_dump(),'revision':revision,'description':latest.description,
                    'uncertainty':max(o.uncertainty for o in supported),
                    'native':{'start':min(o.native.start for o in supported),'end':max(o.native.end for o in supported)},
                    'evidence_ids':[o.evidence_id for o in supported]})
            remaining=max(0,limits.context_records-len(result['observations']))
            result['omitted'] += [s['scene_id'] for s in result['scenes'][remaining:]]
            result['scenes']=result['scenes'][:remaining]
            valid_event=False
            if event_ms is not None:
                for chunk in self.chunks(source,eligible):
                    m=chunk.event_mapping
                    if m and snapshot.mapping_revisions.get(source.source_id)==m.revision and m.valid.start<=eligible.start and eligible.end<=m.valid.end:
                        mapped_end=m.event_ms(eligible.end-1)
                        if m.event_ms(eligible.start)-m.uncertainty_ms<=event_ms<=mapped_end+m.uncertainty_ms:valid_event=True
                if valid_event:
                    result['timing']='calibrated-event'
                    facts={}
                    for row in self._records('fact',current=False):
                        fact=OfficialFact.model_validate_json(row['body'])
                        if fact.revision<=snapshot.context_revision:
                            if fact.effective_event_ms is None:facts.pop(fact.name,None)
                            elif fact.effective_event_ms<=event_ms:facts[fact.name]=fact.model_dump()
                    result['facts']=facts
                else:result['omitted'].append('event-time facts: mapping unknown or invalid')
            for row in self._records('program_text',run=self.run_id):
                text=ProgramText.model_validate_json(row['body'])
                if text.program_revision>snapshot.program_revision:continue
                if text.state in ('aired','completed','interrupted') and (text.event_ms is None or (valid_event and text.event_ms<=event_ms)):
                    result['aired'].append(text.model_dump())
                elif text.state in ('prepared','pending','started'):result['pending'].append(text.model_dump())
            result['aired']=result['aired'][-limits.context_utterances:]
            result['pending']=result['pending'][-limits.context_utterances:]
            if archive_hits:
                if len(archive_hits)>limits.context_hits:result['omitted'].append('archive hit limit')
                # Requery eligibility; a supplied retrieval description is never trusted.
                for hit in archive_hits[:limits.context_hits]:
                    scene=SceneEvent.model_validate(hit['scene'])
                    if scene.source.event_id!=source.event_id or scene.source.run_id!=self.run_id:continue
                    if role=='commentator' and (scene.source!=source or scene.native.end>eligible.end):continue
                    active=self.db.execute("SELECT body FROM records WHERE kind='scene' AND id=? AND active=1",(scene.scene_id,)).fetchone()
                    if not active or SceneEvent.model_validate_json(active['body'])!=scene or scene.status=='retracted':continue
                    # Retrieval cannot introduce evidence produced after review, or revive a correction.
                    supported=True
                    for evidence_id in scene.evidence_ids:
                        evidence=self.db.execute("SELECT r.body,v.ordinal FROM records r JOIN evidence_versions v ON v.id=r.id "
                            "WHERE r.kind='observation' AND r.id=? AND r.active=1",(evidence_id,)).fetchone()
                        if (not evidence or evidence['ordinal']>snapshot.evidence_revision or
                            Observation.model_validate_json(evidence['body']).source!=scene.source):
                            supported=False;break
                    if not supported:
                        result['omitted'].append(scene.scene_id);continue
                    try:self.resolve(scene.source,scene.native)
                    except ValueError:continue
                    result['archive_hits'].append({'scene':scene.model_dump(),'ranking':hit['ranking']})
        # Preserve required facts and references. Do not silently replace truth with a summary.
        while len(canonical(result).encode())>limits.context_bytes:
            for key in ('archive_hits','scenes','observations','aired','pending'):
                if result[key]:
                    removed=result[key].pop();result['omitted'].append(removed.get('evidence_id',removed.get('scene_id',removed.get('cue_id','archive'))));break
            else:
                result.update(status='unavailable',event={},facts={},omitted=['Required context exceeds byte limit']);break
        if len(result['omitted'])>32:result['omitted']=result['omitted'][:32]+['More references omitted']
        if result['omitted'] and result['status']=='ready':result['status']='truncated'
        return ContextSlice.model_validate(result).model_dump(mode='json')

    def start(self):
        if self.threads:raise RuntimeError('Foundation already started')
        # Provider mode dispatch belongs to DataEngine. No second local submitter.
        config=self.settings.providers.get('jobs')
        if ((not config or config.adapter!='fixture') and not self.settings.direction.enabled and not self.settings.replay.enabled
                and not self.registry.capabilities()['search']['ready']):return
        for index in range(self.settings.limits.concurrency):
            thread=threading.Thread(target=self._worker,name=f'analysis-{index}',daemon=True)
            self.threads.append(thread);thread.start()

    def _next(self):
        with self.transaction():
            rows=self.db.execute("SELECT * FROM jobs WHERE state='queued' AND run=? ORDER BY updated",(self.run_id,)).fetchall()
            for row in rows:
                if row['source'] in self.active_sources:continue
                if row['deadline']<=time.time():
                    self.db.execute("UPDATE jobs SET state='expired',error='Original deadline expired' WHERE key=?",(row['key'],))
                    self._notify('analysis.expired','expiry-'+row['key'],{'job_key':row['key']});continue
                self.active_sources.add(row['source'])
                self.db.execute("UPDATE jobs SET state='running',updated=? WHERE key=?",(time.time(),row['key']))
                return row
        return None

    def submit_role(self, role, work):
        """One newest pending trigger per role; workers share the analysis budget."""
        if role not in ('director','commentator','speech','segmentor','search'):raise ValueError('Unknown role work')
        with self.lock:
            if role=='search' and role in self.role_pending:
                raise ValueError('capacity_reached: search queue')
            prior=self.role_pending.get(role)
            self.role_pending[role]=work
        if prior and hasattr(prior,'cancel'):prior.cancel('Replaced by the newest eligible commentary')
        self.wake.set()

    def _worker(self):
        prefer_role=True
        while not self.stop.is_set():
            work=None
            with self.lock:
                if prefer_role or not self.db.execute("SELECT 1 FROM jobs WHERE state='queued' AND run=?",(self.run_id,)).fetchone():
                    # Archive work never consumes both shared slots. Fresh live work
                    # precedes new archive work; alternating analysis retains progress.
                    archive_busy=bool({'segmentor','search'} & self.role_active)
                    role=next((key for key in ('director','commentator','speech','segmentor','search')
                        if key in self.role_pending and key not in self.role_active and
                        (key not in ('segmentor','search') or not archive_busy)),None)
                    if role:
                        work=self.role_pending.pop(role);self.role_active.add(role)
            if work:
                try:work()
                except Exception as error:self.last_failure='Role work failed: '+type(error).__name__
                finally:
                    with self.lock:self.role_active.discard(role)
                prefer_role=False
                continue
            row=self._next()
            prefer_role=True
            if not row:self.wake.wait(.1);self.wake.clear();continue
            window=AnalysisWindow.model_validate_json(row['body'])
            pin='analysis-'+window.job_key
            began=time.time()
            self.stage_times.append({'trace_id':window.trace_id,'stage':'closure-storage-queue','seconds':max(0,began-(window.deadline_utc-self.settings.limits.live_deadline_s))})
            try:
                if time.time()>=window.deadline_utc:raise TimeoutError('Original live deadline expired')
                self.resolve(window.source,window.native,owner=pin,deadline_utc=min(window.deadline_utc,time.time()+self.settings.limits.pin_seconds))
                manifests=self.chunks(window.source,window.native)
                def attempted(count):
                    with self.transaction():self.db.execute('UPDATE jobs SET attempts=? WHERE key=?',(count,window.job_key))
                results=asyncio.run(bounded_call(lambda:analyze_artifacts(window,manifests,self.settings,self.storage,self.registry),window.deadline_utc,self.settings.limits,attempted))
                self.stage_times.append({'trace_id':window.trace_id,'stage':'inference','seconds':time.time()-began})
                self.ingest(window,results,trusted_origin='fixture')
                self.stage_times.append({'trace_id':window.trace_id,'stage':'context-available','seconds':time.time()-began})
            except Exception as error:
                reason=type(error).__name__ # Provider exception text can contain signed URLs or credentials.
                self.last_failure=reason
                with self.transaction():
                    self.db.execute('UPDATE jobs SET state=?,error=?,updated=? WHERE key=?',
                        ('expired' if isinstance(error,TimeoutError) else 'failed',reason,time.time(),window.job_key))
                    self._notify('analysis.failed','failure-'+window.job_key,{'job_key':window.job_key,'reason':reason})
            finally:
                self.release(pin)
                with self.lock:self.active_sources.discard(row['source'])

    def recover(self):
        """Recover committed readiness notifications, including missed deliveries."""
        with self.lock:
            manifests=[ChunkManifest.model_validate_json(r['body']) for r in self._records('chunk',run=self.run_id) if r['available']]
        for m in manifests:
            try:self.enqueue(self.window(m))
            except ValueError:self.gap(m.source,m.native,'Recovery media unavailable')

    def diagnostics(self):
        with self.lock:
            counts={r[0]:r[1] for r in self.db.execute('SELECT state,count(*) FROM jobs WHERE run=? GROUP BY state',(self.run_id,))}
            rows=self._records('chunk',run=None)
            progress={}
            for job in self.db.execute('SELECT * FROM jobs WHERE run=? ORDER BY updated',(self.run_id,)):
                window=AnalysisWindow.model_validate_json(job['body'])
                item=progress.setdefault(job['source'],{'source':window.source.model_dump(),'completed_intervals':[], 'gaps':[]})
                if job['state']=='completed':item['completed_intervals'].append(window.native.model_dump())
                elif job['state'] in ('skipped','failed','expired'):
                    item['gaps'].append({'native':window.native.model_dump(),'state':job['state'],'reason':job['error']})
            for item in progress.values():
                intervals=sorted(item.pop('completed_intervals'),key=lambda value:value['start'])
                first=min((json.loads(r['body'])['native']['start'] for r in rows
                    if json.loads(r['body'])['source']==item['source']),default=0)
                watermark=first
                for interval in intervals:
                    if interval['start']>watermark:break
                    watermark=max(watermark,interval['end'])
                item['watermark_pts']=watermark
                latest=max((json.loads(r['body'])['native']['end'] for r in rows
                    if r['available'] and json.loads(r['body'])['source']==item['source']),default=watermark)
                item['latest_ready_pts']=latest
                item['analysis_lag_source_s']=max(0,latest-watermark)*float(Fraction(item['source']['time_base']))
                item['gaps']=item['gaps'][-32:]
            current_sources={s.slot:s.model_dump() for s in self.reviewed_snapshot().sources}
            return {'event_id':self.settings.event.event_id,'run_id':self.run_id,'context_revision':self.context_revision,
                'capabilities':self.registry.capabilities(),'jobs':counts,'active_jobs':len(self.active_sources),
                'storage_bytes':sum(json.loads(r['body'])['media']['size'] for r in rows if r['available']),
                'unavailable_chunks':sum(not r['available'] for r in rows),'pins':self.db.execute('SELECT count(*) FROM pins').fetchone()[0],
                'gaps':[json.loads(r['body']) for r in self._records('gap')[-32:]],'last_failure':self.last_failure,
                'sources':[item for item in progress.values() if current_sources.get(item['source']['slot'])==item['source']],
                'stage_times':list(self.stage_times),'limits':self.settings.limits.model_dump()}

    def close(self):
        self.stop.set();self.wake.set()
        if self.readonly:
            self.db.close();return
        end=time.monotonic()+max(self.settings.limits.call_timeout_s+1,self.settings.replay.recall_s+1)
        for thread in self.threads:thread.join(max(0,end-time.monotonic()))
        with self.lock:
            pending=list(self.role_pending.values());self.role_pending.clear()
        for work in pending:
            if hasattr(work,'cancel'):work.cancel('Studio stopped before preparation')
        with self.transaction():
            for owner in self.pin_owners:
                self.db.execute('DELETE FROM pins WHERE owner=?',(owner,))
        self.db.close()
