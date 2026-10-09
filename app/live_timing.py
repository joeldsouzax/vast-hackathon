"""Measured recording-to-decoder mapping. Ambiguous pictures remain unknown."""
from fractions import Fraction
import io
import statistics
import av
from PIL import Image, ImageChops, ImageStat


def fingerprint(image, size=(64,36)):
    return image.convert('L').resize(size,Image.Resampling.BILINEAR)


def match_recording(path, source, fps):
    """Three image matches establish an offset; the interior match checks it.

    Declare tolerances before matching: MAE <= 8 luminance units, timestamp
    residual <= one normalized frame, distinct competing matches >= 0.5s apart.
    Static/ambiguous images, clock resets, missing trace, and expired buffer fail.
    This connects application clocks. It does not calibrate phone capture time.
    """
    if not source:raise ValueError('No matching live decoder')
    with source.lock:
        frames=tuple(f for f in source.frames if f.native_provenance and f.source_epoch in (None,source.epoch))
        epoch=source.epoch
    if len(frames)<3:raise ValueError('Live receipt trace is unavailable')
    clock={f.native_provenance['native_time_base'] for f in frames}
    revision={f.native_provenance['timeline_revision'] for f in frames}
    if len(clock)!=1 or len(revision)!=1:raise ValueError('Decoder clock discontinuity remains unresolved')
    geometry=frames[-1].native_provenance.get('geometry')
    if not geometry:raise ValueError('Decoder geometry is unavailable')
    decoded=[]
    with av.open(str(path)) as media:
        stream=media.streams.video[0];file_clock=str(stream.time_base)
        for frame in media.decode(stream):
            if frame.pts is None:raise ValueError('Recorded frame has no clock')
            if (frame.width,frame.height)!=(geometry['native_width'],geometry['native_height']):
                raise ValueError('Recorded orientation differs from the decoder geometry')
            normalized=Image.new('RGB',(geometry['output_width'],geometry['output_height']))
            normalized.paste(frame.to_image().resize((geometry['scaled_width'],geometry['scaled_height']),Image.Resampling.BILINEAR),
                (geometry['pad_x'],geometry['pad_y']))
            decoded.append((frame.pts,fingerprint(normalized)))
            if len(decoded)>360:raise ValueError('Recording exceeds bounded mapping window')
    if len(decoded)<3:raise ValueError('Recording cannot establish three matches')
    lo=float(decoded[0][0]*Fraction(file_clock));hi=float(decoded[-1][0]*Fraction(file_clock))
    # Candidate range is bounded by the recording notification cadence and retained buffer.
    candidates=[]
    for frame in frames[-fps*16:]:
        native=frame.native_provenance
        candidates.append((frame,fingerprint(Image.open(io.BytesIO(frame.data)))))
    matches=[]
    for position in (0,len(decoded)//2,len(decoded)-1):
        pts,signature=decoded[position]
        ranked=sorted((ImageStat.Stat(ImageChops.difference(signature,fp)).mean[0],index) for index,(_,fp) in enumerate(candidates))
        score,index=ranked[0];matched=candidates[index][0]
        native=matched.native_provenance
        seconds=float(native['native_pts']*Fraction(native['native_time_base']))
        competing=next((error for error,i in ranked[1:] if abs(float(candidates[i][0].native_provenance['native_pts']*
            Fraction(candidates[i][0].native_provenance['native_time_base']))-seconds)>=.5),None)
        if score>8 or competing is not None and competing<=score+.15:
            raise ValueError('Recording image match is weak or ambiguous')
        if matched.received_utc is None:raise ValueError('Frame receipt UTC was not measured')
        matches.append({'file_pts':pts,'decoder_pts':native['native_pts'],'decoder_time_base':native['native_time_base'],
            'offset_s':seconds-float(pts*Fraction(file_clock)),'received_utc':matched.received_utc,'mae':score,
            'sequence':matched.sequence,'uncertainty_ms':native['uncertainty_ms']})
    offset=statistics.median(m['offset_s'] for m in matches)
    residual=max(abs(m['offset_s']-offset) for m in matches)
    if residual>1/fps+1e-6:raise ValueError('Recorder/decoder mapping exceeds one-frame tolerance')
    if source.epoch!=epoch:raise ValueError('Source epoch changed during mapping')
    ticks=round(offset/float(Fraction(file_clock)))
    return {'timeline_offset_pts':ticks,'time_base':file_clock,'last_receipt_utc':matches[-1]['received_utc'],
        'geometry':geometry,'revision':next(iter(revision)),'matches':matches,'residual_ms':residual*1000,
        'uncertainty_ms':max(m['uncertainty_ms'] for m in matches)+residual*1000,
        'algorithm':'three decoded image matches and interior timestamp residual',
        'tolerance_ms':1000/fps,'capture_clock':'unknown'}
