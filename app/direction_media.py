"""Prepared media only. No provider, storage, or database work in playback."""
from __future__ import annotations
from array import array
from dataclasses import dataclass, field
import math
import sys
import time
import av
from PIL import Image, ImageDraw
from foundation_records import CropRect, Geometry


def validate_crop(rect, geometry, width=640, height=360, maximum=2.0):
    rect=CropRect.model_validate(rect)
    geometry=Geometry.model_validate(geometry)
    aspect=(rect.width*geometry.scaled_width)/(rect.height*geometry.scaled_height)
    if not math.isclose(aspect,width/height,rel_tol=1e-6):
        raise ValueError('Crop must match the program aspect ratio')
    if max(1/rect.width,1/rect.height)>maximum+1e-9:
        raise ValueError('Crop exceeds the magnification limit')
    return rect


def crop_pixels(image, rect, geometry):
    # Coordinates refer to oriented source pixels, excluding the fit padding.
    x=geometry.pad_x+rect.x*geometry.scaled_width
    y=geometry.pad_y+rect.y*geometry.scaled_height
    box=(round(x),round(y),round(x+rect.width*geometry.scaled_width),round(y+rect.height*geometry.scaled_height))
    return image.crop(box).resize(image.size,Image.Resampling.LANCZOS)


def caption_layer(graphics, text):
    if any(ord(c)<32 for c in text):raise ValueError('Caption must be plain text')
    font=graphics.font(20,'dmsans');draw=ImageDraw.Draw(Image.new('RGB',(1,1)))
    lines=['']
    for word in text.split():
        trial=(lines[-1]+' '+word).strip()
        if draw.textlength(trial,font=font)>graphics.w*.86:
            if not lines[-1] or len(lines)==2:raise ValueError('Caption does not fit two lines')
            lines.append(word)
            if draw.textlength(word,font=font)>graphics.w*.86:raise ValueError('Caption word does not fit')
        else:lines[-1]=trial
    if not lines[0]:raise ValueError('Empty caption')
    layer=Image.new('RGBA',(graphics.w,graphics.h));draw=ImageDraw.Draw(layer)
    bottom=round(graphics.h*.95);top=bottom-12-28*len(lines)
    draw.rounded_rectangle((round(graphics.w*.05),top,round(graphics.w*.95),bottom),8,fill=(20,24,20,235))
    for index,line in enumerate(lines):draw.text((graphics.w//2,top+6+index*28),line,font=font,fill='white',anchor='mt')
    return layer


def decode_speech(result, storage, text, event, settings):
    if result.transcript!=text:raise ValueError('Speech transcript does not match requested text')
    if result.configuration_revision!=settings.configuration_revision:raise ValueError('Speech configuration changed')
    if result.origin=='provider' and result.voice_id!=event.voice_id:raise ValueError('Speech voice differs from reviewed voice')
    limits=settings.direction
    if result.media.size>limits.speech_asset_bytes:raise ValueError('Speech file exceeds asset limit')
    path=storage.inspect(result.media)
    pcm=bytearray();resampler=av.AudioResampler(format='s16',layout='mono',rate=48000)
    frames=0
    with av.open(str(path)) as media:
        if media.format.name!='wav' or result.media.format!='wav':raise ValueError('Speech format is not supported by this mixer')
        if len(media.streams.audio)!=1 or media.streams.video:raise ValueError('Speech requires one audio stream')
        stream=media.streams.audio[0]
        if stream.codec_context.sample_rate!=result.sample_rate or stream.codec_context.channels!=result.channels:
            raise ValueError('Speech format differs from its record')
        for frame in media.decode(stream):
            frames+=1
            for normalized in resampler.resample(frame):
                pcm.extend(bytes(normalized.planes[0])[:normalized.samples*2])
                if len(pcm)+result.media.size>limits.speech_asset_bytes or len(pcm)>limits.speech_max_s*96000:
                    raise ValueError('Decoded speech exceeds duration or memory limit')
        for normalized in resampler.resample(None):pcm.extend(bytes(normalized.planes[0])[:normalized.samples*2])
    duration=len(pcm)/96000
    if frames!=result.media.decoded_frames or not duration or duration>limits.speech_max_s or abs(duration-result.duration_s)>max(.02,1/result.sample_rate):
        raise ValueError('Decoded speech duration differs from its record or exceeds limit')
    return bytes(pcm)


def samples(data):
    values=array('h');values.frombytes(data)
    if sys.byteorder!='little':values.byteswap()
    return values


class Mixer:
    def __init__(self, settings):
        self.settings=settings;self.gain=settings.ambient_gain
    def mix(self, ambient, voice=b'', offset=0, total=0):
        a=samples(ambient);v=samples(voice)
        config=self.settings
        target=config.ambient_gain*(config.duck_gain if voice else 1)
        step=config.ambient_gain/max(1,config.ramp_ms*48)
        ramp=max(1,config.ramp_ms*48)
        for index,value in enumerate(a):
            self.gain+=max(-step,min(step,target-self.gain))
            narration=0
            if index<len(v):
                envelope=min(1,(offset+index+1)/ramp,max(0,(total-offset-index)/ramp))
                narration=v[index]*min(config.narrator_gain,max(0,.95-self.gain))*envelope
            # Saturation is a final safeguard; configured headroom avoids clipping.
            a[index]=max(-32767,min(32767,round(value*self.gain+narration)))
        if sys.byteorder!='little':a.byteswap()
        return a.tobytes()


@dataclass
class PreparedCue:
    id: str
    session_id: str
    text: str
    pcm: bytes
    caption: Image.Image | None
    start_frame: int
    end_frame: int
    expires_at: float
    program_revision: int
    target: dict
    guard: object
    evidence_ids: list[str]=field(default_factory=list)
    event_ms: int | None=None
    asset: object=None
    offset: int=0
    delivered: dict=field(default_factory=dict)
    canceled: bool=False
    audio_inflight: bool=False
    caption_inflight: bool=False
    cancel_reason: str | None=None

    def valid(self):
        return not self.canceled and time.time()<self.expires_at and self.guard()

    @property
    def memory_bytes(self):
        return len(self.pcm)+(self.asset.size if self.asset else 0)+(self.caption.width*self.caption.height*4 if self.caption else 0)
