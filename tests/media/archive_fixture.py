"""Labeled closed recordings for replay checks, admitted through production APIs."""
from fractions import Fraction
import asyncio
import hashlib
import json
from pathlib import Path
import time
import av
from foundation_records import SourceEpoch, Geometry, Interval, ReplayPlan12, ShotIntent
from foundation_storage import inspect_video
from replay_fixture import staged_image

ROOT=Path(__file__).resolve().parents[2]


def configuration(folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    labels=json.loads((ROOT/'tests/fixtures/replay/labels.json').read_text())
    labels['cosmos']=[{'start_s':0.,'end_s':4.,'action':'retained-action',
        'description':'Labeled yellow ball action with lead-in and aftermath', 'kind':'observed','uncertainty':.2,
        'view':{'subject_visible':True,'quality':'usable','adds':'Inspected staged ball path'}}]
    labels['segmentor']={'retained-action':{'required_s':{'start':0.,'end':4.},'action_s':{'start':1.,'end':3.},'speed':1.}}
    labels['media_hashes']=[]
    for speech in [labels['speech']]+labels['speech'].get('variants',[]):
        speech['file']=str(ROOT/'tests/fixtures'/Path(speech['file']).relative_to('/opt/breadcast/tests/fixtures'))
    (folder/'labels.json').write_text(json.dumps(labels))
    config=json.loads((ROOT/'config/replay.fixture.json').read_text())
    config['fixture_file']=str(folder/'labels.json')
    config['event'].update(event_id='archive-check',revision=7)
    config['limits']={'disk_reserve_bytes':0}
    (folder/'config.json').write_text(json.dumps(config))
    return folder/'config.json'


def retained_action(app, folder, camera=1, *, variable=False, rotation=0, start_pts=0):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    base=Fraction(1,15360)
    source=SourceEpoch(event_id=app.foundation.settings.event.event_id,run_id=app.control.run_id,
        source_id=f'camera/retained-{camera}',slot=camera,epoch=1,time_base=str(base))
    paths=[];offset=0;manifests=[]
    for chunk in range(2):
        path=folder/f'view-{camera}-{chunk}.mp4'
        with av.open(str(path),'w') as media:
            stream=media.add_stream('libx264',rate=15);stream.width=640;stream.height=360;stream.pix_fmt='yuv420p'
            stream.time_base=stream.codec_context.time_base=base;stream.options={'preset':'ultrafast','crf':'16','bf':'0'}
            for index in range(30):
                if variable and index==16:continue
                frame=av.VideoFrame.from_image(staged_image(app.cfg,camera,chunk*30+index))
                frame.pts=start_pts+index*1024;frame.time_base=base
                for packet in stream.encode(frame):media.mux(packet)
            for packet in stream.encode():media.mux(packet)
        info=inspect_video(path)
        source=source.model_copy(update={'time_base':info['time_base']})
        if rotation:
            geometry=Geometry(native_width=640,native_height=360,rotation=rotation,output_width=640,output_height=360,
                scaled_width=202,scaled_height=360,pad_x=219,pad_y=0)
        else:geometry=Geometry(native_width=640,native_height=360,output_width=640,output_height=360,scaled_width=640,scaled_height=360)
        # Epoch timeline is continuous despite file PTS reset and nonzero file origin.
        manifest=app.foundation.finalize(path,source,chunk,time.time(),closed=True,
            geometry=geometry,timeline_offset_pts=offset-info['start'],provenance='sample')
        offset=manifest.native.end;manifests.append(manifest);paths.append(path)
        app.foundation.registry.labels.setdefault('media_hashes',[]).append(manifest.media.sha256)
        Path(app.foundation.settings.fixture_file).write_text(json.dumps(app.foundation.registry.labels,indent=2)+'\n')
        window=app.foundation.window(manifest)
        app.foundation.enqueue(window)
        results=asyncio.run(app.foundation.registry.analyze(window,app.foundation.chunks(source,window.native)))
        app.foundation.ingest(window,results,trusted_origin='fixture')
    with app.foundation.lock:
        scenes=[json.loads(r['body']) for r in app.foundation._records('scene',run=app.control.run_id)]
    scene=next(s for s in scenes if s['source']==source.model_dump())
    plan=ReplayPlan12(plan_id=f'archive-{camera}',event_id=source.event_id,run_id=source.run_id,
        context_revision=app.foundation.context_revision,configuration_revision=app.foundation.settings.configuration_revision,
        snapshot=app.foundation.reviewed_snapshot(),input_kind='archive',source=source,
        action=Interval(start=15360,end=46080),required=Interval(start=0,end=61440),expires_at=time.time()+60,
        shots=[ShotIntent(source=source,native=Interval(start=0,end=61440),
            scene_revisions={scene['scene_id']:scene['revision']},evidence_ids=scene['evidence_ids'],reason='Labeled full action')],
        selection_reason='Inspected fixture retained after disconnect')
    record={'origin':'generated labeled fixture','source':source.model_dump(),'scene_id':scene['scene_id'],
        'scene_revision':scene['revision'],'files':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths],
        'manifests':[m.model_dump(mode='json') for m in manifests],'plan':plan.model_dump(mode='json')}
    (folder/'fixture.json').write_text(json.dumps(record,indent=2)+'\n')
    return record
