"""Configured OpenAI-compatible speech output, decoded before mixer admission."""
import asyncio
import os
from pathlib import Path
import tempfile
import time

import av
import httpx

from foundation_records import SpeechResult
from live_gpu import discover, endpoint, request


class LiveSpeech:
    def __init__(self,registry):
        self.registry=registry
        self.config=registry.live.config
        self.model=None
        self.verified=False
        self.protocol=self.config.get('BREADCAST_TTS_PROTOCOL','nvidia-nim')
        self.voices=None
        self.version=None

    def configured(self):return bool(self.config.get('BREADCAST_TTS_URL'))

    async def synthesize(self,text,storage,deadline,event):
        if not event.voice_id:raise ValueError('Select the configured speech voice in Event details')
        url=endpoint(self.config.get('BREADCAST_TTS_URL'))
        token=self.config.get('BREADCAST_TTS_API_KEY')
        remaining=deadline-time.time()
        if remaining<=0:raise TimeoutError('Speech deadline expired')
        path=None
        try:
            async with asyncio.timeout(remaining),httpx.AsyncClient(follow_redirects=False) as client:
                if self.protocol=='nvidia-nim':
                    if self.voices is None:
                        await request(client,'GET',url+'/v1/health/ready',token=token,deadline=deadline)
                        metadata=await request(client,'GET',url+'/v1/metadata',token=token,deadline=deadline)
                        models=[item['shortName'] for item in metadata.get('modelInfo',[])
                            if isinstance(item,dict) and isinstance(item.get('shortName'),str)]
                        selected=self.config.get('BREADCAST_TTS_MODEL')
                        if not selected and len(models)==1:selected=models[0]
                        if selected not in models:raise ValueError('Select the returned NVIDIA speech model identity')
                        self.model=selected;self.version=metadata.get('version')
                        self.voices=await request(client,'GET',url+'/v1/audio/list_voices',token=token,deadline=deadline)
                    locales=set();voices=set()
                    for languages,group in self.voices.items():
                        if isinstance(group,dict) and isinstance(group.get('voices'),list):
                            locales.update(languages.split(','));voices.update(group['voices'])
                    language=self.config.get('BREADCAST_TTS_LANGUAGE') or event.language
                    if language not in locales:
                        matches=[locale for locale in locales if locale.split('-')[0]==language]
                        if len(matches)!=1:raise ValueError('Select a speech language returned by NVIDIA NIM')
                        language=matches[0]
                    if event.voice_id not in voices:raise ValueError('Event voice is not available on NVIDIA NIM')
                    route='/v1/audio/synthesize'
                    fields={'text':text,'language':language,'voice':event.voice_id,
                        'sample_rate_hz':'48000','encoding':'LINEAR_PCM'}
                    payload={'files':{name:(None,value) for name,value in fields.items()}}
                elif self.protocol=='openai':
                    if self.model is None:
                        self.model,_=await discover(client,url,self.config.get('BREADCAST_TTS_MODEL'),token,deadline)
                    route='/v1/audio/speech'
                    payload={'json':{'model':self.model,'input':text,'voice':event.voice_id,'response_format':'wav'}}
                else:raise ValueError('Speech protocol must be nvidia-nim or openai')
                headers={'Authorization':'Bearer '+token} if token else {}
                async with client.stream('POST',url+route,headers=headers,**payload,
                    timeout=max(.01,deadline-time.time())) as response:
                    if not 200<=response.status_code<300:raise ValueError('Speech HTTP '+str(response.status_code))
                    fd,temporary=tempfile.mkstemp(prefix='.speech-',suffix='.wav',dir=storage.root)
                    path=Path(temporary);received=0
                    with os.fdopen(fd,'wb') as output:
                        async for part in response.aiter_bytes():
                            received+=len(part)
                            if received>self.registry.settings.direction.speech_asset_bytes:
                                raise ValueError('Speech output exceeds the mixer asset limit')
                            output.write(part)
            frames=samples=0
            with av.open(str(path)) as media:
                if media.format.name!='wav' or len(media.streams.audio)!=1 or media.streams.video:
                    raise ValueError('Speech provider must return one WAV audio stream')
                stream=media.streams.audio[0]
                rate=stream.codec_context.sample_rate;channels=stream.codec_context.channels
                for frame in media.decode(audio=0):
                    frames+=1;samples+=frame.samples
                    if samples/rate>self.registry.settings.direction.speech_max_s:
                        raise ValueError('Generated speech exceeds eight seconds')
            if not frames or not samples:raise ValueError('Speech audio is empty')
            if time.time()>=deadline:raise TimeoutError('Speech arrived after its program interval')
            asset=storage.put(path,'wav',frames)
            self.verified=True
            return SpeechResult(media=asset,duration_s=samples/rate,sample_rate=rate,channels=channels,
                origin='provider',model_id=self.model,voice_id=event.voice_id,transcript=text,
                configuration_revision=self.registry.settings.configuration_revision)
        except httpx.HTTPError:raise ValueError('Speech transport failed') from None
        finally:
            if path:path.unlink(missing_ok=True)
