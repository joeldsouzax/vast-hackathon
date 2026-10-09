"""Generic bounded artifact worker. This module has no controller or SQLite writer."""
from foundation_storage import inspect_video


async def analyze_artifacts(window, manifests, settings, storage, registry, *, work_deadline=None):
    if window.snapshot.configuration_revision!=settings.configuration_revision:
        raise ValueError('Worker configuration differs from issued snapshot')
    if set(window.chunk_ids)!={m.chunk_id for m in manifests}:
        raise ValueError('Worker manifests differ from issued window')
    position=window.native.start
    for manifest in sorted(manifests,key=lambda m:m.native.start):
        if manifest.source!=window.source:raise ValueError('Worker source ownership differs')
        info=inspect_video(storage.inspect(manifest.media))
        if (info['frames']!=manifest.media.decoded_frames or
            info['start']+manifest.timeline_offset_pts!=manifest.native.start or
            info['end']+manifest.timeline_offset_pts!=manifest.native.end or info['time_base']!=window.source.time_base):
            raise ValueError('Worker bytes differ from ready manifest')
        if manifest.native.start>position:raise ValueError('Worker coverage gap')
        position=max(position,manifest.native.end)
    if position<window.native.end:raise ValueError('Worker interval is not finalized')
    return await registry.analyze(window,manifests,work_deadline=work_deadline)
