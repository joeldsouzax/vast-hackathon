"""Retained-file input for the existing replay compiler and renderer."""
from __future__ import annotations
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import math
import time
import av
from PIL import Image
from foundation import operation_key
from foundation_records import ArchiveResolution, ReplayPlan12, SceneEvent
from replay import Resolved, digest
from direction_media import validate_crop


def output_geometry(recorded, cfg):
    """Original recording geometry and the deterministic replay proxy are distinct."""
    from foundation_records import Geometry
    if (recorded.output_width,recorded.output_height)==(cfg.width,cfg.height):return recorded
    width,height=recorded.native_width,recorded.native_height
    if recorded.rotation in (90,270):width,height=height,width
    rate=min(cfg.width/width,cfg.height/height)
    scaled_width=max(1,round(width*rate));scaled_height=max(1,round(height*rate))
    return Geometry(native_width=recorded.native_width,native_height=recorded.native_height,rotation=recorded.rotation,
        output_width=cfg.width,output_height=cfg.height,scaled_width=scaled_width,scaled_height=scaled_height,
        pad_x=(cfg.width-scaled_width)//2,pad_y=(cfg.height-scaled_height)//2)


@dataclass(frozen=True)
class ArchiveFrame:
    data: bytes
    media_s: float
    pts: int
    time_base: str
    duration_s: float
    native_provenance: dict
    sequence: int | None = None
    received: float | None = None


def check_budget(deadline, canceled=None):
    if canceled and (canceled() if callable(canceled) else canceled.is_set()):raise ValueError('Preparation canceled')
    if time.time() >= deadline:raise TimeoutError('deadline_missed')


def verify_dependencies(foundation, plan):
    if (plan.event_id,plan.run_id)!=(foundation.settings.event.event_id,foundation.run_id):raise ValueError('wrong_run')
    if plan.context_revision!=foundation.context_revision or plan.configuration_revision!=foundation.settings.configuration_revision:
        raise ValueError('Reviewed context changed')
    if time.time()>=plan.expires_at:raise ValueError('deadline_missed')
    if plan.expires_at>time.time()+60:raise ValueError('Plan expiry exceeds application limit')
    with foundation.lock:
        for shot in plan.shots:
            intervals=[]
            scene_evidence=set()
            for scene_id,revision in shot.scene_revisions.items():
                row=foundation.db.execute("SELECT body FROM records WHERE kind='scene' AND id=? AND active=1",(scene_id,)).fetchone()
                if not row:raise ValueError('scene_changed')
                scene=SceneEvent.model_validate_json(row[0])
                if scene.status=='retracted':raise ValueError('evidence_retracted')
                if scene.revision!=revision or scene.source!=shot.source:raise ValueError('scene_changed')
                if not set(scene.evidence_ids).intersection(shot.evidence_ids):raise ValueError('Unused scene dependency')
                scene_evidence.update(scene.evidence_ids)
            if any(eid not in scene_evidence for eid in shot.evidence_ids):raise ValueError('Unsupported scene evidence')
            for eid in shot.evidence_ids:
                row=foundation.db.execute("SELECT r.body,v.ordinal FROM records r JOIN evidence_versions v ON r.id=v.id "
                    "WHERE r.kind='observation' AND r.id=? AND r.active=1",(eid,)).fetchone()
                if not row:raise ValueError('evidence_retracted')
                obs=foundation._observation(eid)
                if row['ordinal']>plan.snapshot.evidence_revision:raise ValueError('Evidence was not reviewed')
                if obs.source!=shot.source:raise ValueError('Evidence source differs')
                view=obs.view
                if not view or not view.subject_visible or view.quality!='usable':raise ValueError('Unsupported view')
                if not obs.native.start<=view.native.start<view.native.end<=obs.native.end:
                    raise ValueError('View exceeds inspected interval')
                intervals.append((view.native.start,view.native.end))
            position=shot.native.start
            for start,end in sorted(intervals):
                if start<=position:position=max(position,end)
            if position<shot.native.end:raise ValueError('coverage_gap')
        # All angles must be linked to the same supported action; overlap is insufficient.
        associations={foundation._observation(eid).association_key for s in plan.shots for eid in s.evidence_ids}
        if len({operation_key(s.source.model_dump()) for s in plan.shots})>1 and (None in associations or len(associations)!=1):
            raise ValueError('Unsupported cross-camera association')
    return True


def published_dependencies(foundation, plan, chunk_ids):
    """No SQL or file IO. Used only after full validation and bounded pinning."""
    return (not set(chunk_ids).intersection(foundation.unavailable_chunks) and
        not {eid for s in plan.shots for eid in s.evidence_ids}.intersection(foundation.invalidated_evidence) and
        all(foundation.scene_versions.get(sid)==revision for s in plan.shots for sid,revision in s.scene_revisions.items()) and
        all((operation_key(s.source.model_dump()),s.mapping_revision) not in foundation.invalidated_mappings
            for s in plan.shots if s.mapping_revision is not None))


def _frames(resolution, cfg, deadline, canceled, memory_left):
    """Stream closed chunks; select by native PTS and recorded frame duration."""
    frames=[];used=0;position=resolution.native.start
    source_base=Fraction(resolution.source.time_base)
    for chunk in resolution.chunks:
        manifest=chunk.manifest;geometry=output_geometry(manifest.geometry,cfg)
        previous=None
        with av.open(chunk.path) as media:
            stream=media.streams.video[0]
            if Fraction(stream.time_base)!=source_base:raise ValueError('Native time base changed')
            for frame in media.decode(stream):
                check_budget(deadline,canceled)
                if frame.pts is None or frame.duration<=0:raise ValueError('Frame timing is unknown')
                pts=frame.pts+manifest.timeline_offset_pts
                end=pts+frame.duration
                if previous is not None and pts!=previous:raise ValueError('coverage_gap')
                previous=end
                if end<=resolution.native.start:continue
                if pts>=resolution.native.end:break
                if pts>position:raise ValueError('coverage_gap')
                if frames and pts<=frames[-1].pts:raise ValueError('Duplicate or discontinuous native samples')
                position=max(position,end)
                image=frame.to_image().convert('RGB')
                if image.size!=(geometry.native_width,geometry.native_height):raise ValueError('Recorded geometry changed')
                if geometry.rotation:image=image.rotate(-geometry.rotation,expand=True)
                scaled=image.resize((geometry.scaled_width,geometry.scaled_height),Image.Resampling.LANCZOS)
                normalized=Image.new('RGB',(geometry.output_width,geometry.output_height),'black')
                normalized.paste(scaled,(geometry.pad_x,geometry.pad_y))
                if normalized.size!=(cfg.width,cfg.height):raise ValueError('Output geometry differs')
                from media import jpeg
                data=jpeg(normalized);used+=len(data)
                # Include image/decoder working space, not just compressed JPEGs.
                if used+frame.width*frame.height*12+cfg.width*cfg.height*12>memory_left:
                    raise ValueError('capacity_reached: preparation memory')
                native={'native_pts':pts,'native_time_base':str(source_base),'chunk_id':manifest.chunk_id,
                    'file_pts':frame.pts,'file_time_base':str(frame.time_base),'timeline_offset_pts':manifest.timeline_offset_pts,
                    'geometry':geometry.model_dump(),'recording_geometry':manifest.geometry.model_dump(),'timeline_revision':None,'received_utc':None,
                    'uncertainty_ms':0.0}
                frames.append(ArchiveFrame(data,pts*float(source_base),pts,str(source_base),
                    frame.duration*float(source_base),native))
    if not frames or position<resolution.native.end:raise ValueError('coverage_gap')
    return tuple(frames),used


def resolve_plan(app, raw, owner, deadline, canceled=None):
    plan=ReplayPlan12.model_validate(raw)
    foundation=app.foundation;settings=foundation.settings.replay;cfg=app.cfg
    deadline=min(deadline,plan.expires_at)
    check_budget(deadline,canceled);verify_dependencies(foundation,plan)
    if len(plan.shots)>settings.max_shots:raise ValueError('capacity_reached: shot count')
    if plan.output.model_dump()!={'width':cfg.width,'height':cfg.height,'fps_num':cfg.fps,'fps_den':1}:
        raise ValueError('Unsupported replay output')
    resolutions=[];pins=[];shots=[];total=0.;used=0;unique={};mappings=[]
    try:
        for shot in plan.shots:
            check_budget(deadline,canceled)
            if plan.input_kind=='archive':
                resolution=ArchiveResolution.model_validate(foundation.resolve(shot.source,shot.native,
                    mapping_revision=shot.mapping_revision,owner=owner,
                    deadline_utc=min(deadline,time.time()+min(60,foundation.settings.limits.pin_seconds))))
                for chunk in resolution.chunks:unique[chunk.manifest.chunk_id]=chunk.manifest
                resolutions.append(resolution)
            else:
                # Native timing is mandatory for canonical live-buffer plans.
                source=app.get_source(shot.source.slot)
                if not source or (source.path,source.epoch)!=(shot.source.source_id,shot.source.epoch):raise ValueError('Live source lease changed')
                with source.lock:selected=tuple(source.frames)
                frames=[]
                for frame in selected:
                    native=frame.native_provenance
                    if not native:continue
                    pts=round(native['native_pts']*float(Fraction(native['native_time_base'])/Fraction(shot.source.time_base)))
                    instant=pts*float(Fraction(shot.source.time_base))
                    if instant+1/cfg.fps>shot.native.start*float(Fraction(shot.source.time_base)) and pts<shot.native.end:
                        frames.append(ArchiveFrame(frame.data,instant,pts,shot.source.time_base,1/cfg.fps,native,frame.sequence,frame.received))
                if not frames:raise ValueError('coverage_gap')
                pins.append(tuple(frames));used+=sum(len(f.data) for f in frames)
        if len(unique)>settings.input_chunks or sum(m.media.size for m in unique.values())>settings.input_bytes:
            raise ValueError('capacity_reached: archive input')
        if any(m.geometry.native_width*m.geometry.native_height*12+cfg.width*cfg.height*12>settings.memory_bytes
            for m in unique.values()):raise ValueError('capacity_reached: decoder geometry')
        if foundation.storage.free_bytes()<foundation.settings.limits.disk_reserve_bytes+settings.scratch_bytes:
            raise ValueError('capacity_reached: disk reserve')
        for index,shot in enumerate(plan.shots):
            if plan.input_kind=='archive':
                frames,size=_frames(resolutions[index],cfg,deadline,canceled,settings.memory_bytes-used)
                pins.append(frames);used+=size
            else:frames=pins[index]
            geometry=frames[0].native_provenance.get('geometry')
            if any(f.native_provenance.get('geometry')!=geometry for f in frames):raise ValueError('Geometry changed within shot')
            from foundation_records import Geometry
            g=Geometry.model_validate(geometry)
            crop=shot.crop
            if crop.model_dump()=={'x':0.,'y':0.,'width':1.,'height':1.}:
                normalized=[0.,0.,1.,1.]
            else:
                validate_crop(crop,g,cfg.width,cfg.height,2.)
                normalized=[(g.pad_x+crop.x*g.scaled_width)/cfg.width,(g.pad_y+crop.y*g.scaled_height)/cfg.height,
                    crop.width*g.scaled_width/cfg.width,crop.height*g.scaled_height/cfg.height]
            start,end=shot.native.start*float(Fraction(shot.source.time_base))*1000,shot.native.end*float(Fraction(shot.source.time_base))*1000
            mapping=None
            if shot.mapping_revision is not None:
                mapping=foundation.mapping(shot.source,shot.mapping_revision)
                if not mapping.valid.start<=shot.native.start<shot.native.end<=mapping.valid.end:raise ValueError('mapping_invalid')
                mapped_start=mapping.offset_event_ms+(shot.native.start-mapping.origin_pts)*float(Fraction(shot.source.time_base))*1000*mapping.rate_correction
                mapped_end=mapping.offset_event_ms+(shot.native.end-mapping.origin_pts)*float(Fraction(shot.source.time_base))*1000*mapping.rate_correction
                if max(abs(mapped_start-shot.event.start),abs(mapped_end-shot.event.end))>1.:raise ValueError('mapping_invalid')
            mappings.append(mapping)
            duration=(end-start)/shot.speed
            retained={'source_path':shot.source.source_id,'sequence':None,'pts':[frames[0].pts,frames[-1].pts],
                'time_base':shot.source.time_base,'chunk_ids':[c.manifest.chunk_id for c in resolutions[index].chunks] if resolutions else [],
                'sha256':hashlib.sha256(b''.join(f.data for f in frames)).hexdigest()}
            with foundation.lock:origins=sorted({foundation._observation(eid).origin for eid in shot.evidence_ids})
            shots.append({**shot.model_dump(mode='json'),'source_id':shot.source.source_id,'source_slot':shot.source.slot,
                'source_epoch':shot.source.epoch,'event_start_ms':shot.event.start if shot.event else None,
                'event_end_ms':shot.event.end if shot.event else None,'source_start_ms':start,'source_end_ms':end,
                'output_start_ms':total,'output_end_ms':total+duration,'crop_normalized':normalized,
                'retained_media':retained,'orientation_transform':g.model_dump(),
                'evidence_origins':origins})
            total+=duration
        # Ensure lead-in, decisive action and required aftermath all remain in the edit.
        intervals=[]
        required=plan.required
        if plan.shots[0].event:
            primary=next((s for s in plan.shots if s.source==plan.source),None)
            if not primary:raise ValueError('Action source was not reviewed')
            mapping=foundation.mapping(plan.source,primary.mapping_revision)
            required_start=mapping.offset_event_ms+(required.start-mapping.origin_pts)*float(Fraction(plan.source.time_base))*1000*mapping.rate_correction
            required_end=mapping.offset_event_ms+(required.end-mapping.origin_pts)*float(Fraction(plan.source.time_base))*1000*mapping.rate_correction
            intervals=[(s.event.start,s.event.end) for s in plan.shots if s.edit=='continuous']
        else:
            required_start,required_end=required.start,required.end
            intervals=[(s.native.start,s.native.end) for s in plan.shots if s.edit=='continuous']
        position=required_start
        for start,end in sorted(intervals):
            if start<=position+1e-6:position=max(position,end)
        if position<required_end-1e-6:raise ValueError('Edit omits required action coverage')
        bounds=[]
        for i,a in enumerate(plan.shots):
            for j,b in enumerate(plan.shots[:i]):
                if a.source!=b.source:
                    bound=mappings[i].uncertainty_ms+mappings[j].uncertainty_ms+2000/cfg.fps
                    if bound>settings.alignment_ms:raise ValueError('mapping_invalid: cross-camera uncertainty')
                    bounds.append(bound)
        for shot in shots:shot['combined_alignment_bound_ms']=max(bounds,default=None)
        if total>settings.max_duration_s*1000+1e-6:raise ValueError('Replay exceeds duration limit')
        if plan.expected_duration_ms is not None and abs(total-plan.expected_duration_ms)>1e-3:raise ValueError('Duration differs')
        if used+math.ceil(total*cfg.fps/1000)*cfg.width*cfg.height*3>settings.memory_bytes:
            raise ValueError('capacity_reached: validation memory')
        verify_dependencies(foundation,plan)
        raw=plan.model_dump(mode='json');raw['expected_duration_ms']=total
        resolved=Resolved(raw,shots,pins,total,digest(raw),deadline=deadline,
            release_callback=lambda:foundation.release(owner),scratch_bytes=settings.scratch_bytes,
            monotonic_deadline=time.monotonic()+max(0,deadline-time.time()))
        return resolved
    except BaseException:
        foundation.release(owner)
        raise
