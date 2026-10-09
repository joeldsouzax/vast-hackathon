"""ElevenLabs and configured speech output, decoded before mixer admission."""
import asyncio
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import quote

import av
import httpx

from foundation_records import SpeechResult
from live_gpu import discover, endpoint, request
from provider_errors import ProviderFailure, http_failure


class LiveSpeech:
    def __init__(self,registry):
        self.registry=registry
        self.config=registry.live.config
        self.model=None
        self.verified=False
        self.protocol=self.config.get('BREADCAST_TTS_PROTOCOL','auto')
        if self.protocol=='auto':self.protocol='elevenlabs' if self.config.get('ELEVENLABS_API_KEY') else 'nvidia-nim'
        self.voices=None
        self.version=None
        self.voice_id=None
        self.voice_name=None

    def configured(self):return bool(self.config.get('ELEVENLABS_API_KEY')) if self.protocol=='elevenlabs' else bool(self.config.get('BREADCAST_TTS_URL'))

    async def elevenlabs(self,client,event,deadline):
        url=endpoint(self.config.get('BREADCAST_ELEVENLABS_BASE_URL','https://api.elevenlabs.io'),boundary='speech')
        if self.model is None:
            models=await request(client,'GET',url+'/v1/models',deadline=deadline,boundary='speech')
            rows=[row for row in models if isinstance(row,dict) and row.get('can_do_text_to_speech') is True]
            ids=[row['model_id'] for row in rows if isinstance(row.get('model_id'),str)]
            selected=self.config.get('ELEVENLABS_MODEL_ID')
            if not selected:selected=next((name for name in ('eleven_flash_v2_5','eleven_multilingual_v2') if name in ids),ids[0] if ids else None)
            if selected not in ids:raise ProviderFailure('configuration_missing','speech',hint='Select an available ELEVENLABS_MODEL_ID')
            self.model=selected
        selected=event.voice_id or self.config.get('ELEVENLABS_VOICE_ID')
        if self.voices is None or selected and not any(v['voice_id']==selected for v in self.voices):
            query='/v2/voices?page_size=100'+('&voice_ids='+quote(selected,safe='') if selected else '')
            result=await request(client,'GET',url+query,deadline=deadline,boundary='speech')
            self.voices=[voice for voice in result.get('voices',[]) if isinstance(voice,dict) and isinstance(voice.get('voice_id'),str)]
        if not selected:
            voices=sorted(self.voices,key=lambda voice:(voice.get('category')!='premade',voice['voice_id']))
            selected=voices[0]['voice_id'] if voices else None
        voice=next((v for v in self.voices if v['voice_id']==selected),None)
        if not voice:raise ProviderFailure('configuration_missing','speech',hint='Select an available ELEVENLABS_VOICE_ID')
        self.voice_id=selected;self.voice_name=voice.get('name')
        return url+'/v1/text-to-speech/'+quote(selected,safe='')+'?output_format=mp3_44100_128'

    def wav(self,path):
        """Use the baseline MP3 response; do not require a paid WAV format."""
        output=path.with_suffix('.wav');samples=0
        try:
            with av.open(str(path)) as source,av.open(str(output),'w',format='wav') as target:
                stream=target.add_stream('pcm_s16le',rate=48000);stream.layout='mono'
                resampler=av.AudioResampler(format='s16',layout='mono',rate=48000)
                for frame in source.decode(audio=0):
                    for normalized in resampler.resample(frame):
                        samples+=normalized.samples
                        if samples>48000*self.registry.settings.direction.speech_max_s:
                            raise ValueError('Generated speech exceeds eight seconds')
                        for packet in stream.encode(normalized):target.mux(packet)
                for frame in resampler.resample(None):
                    samples+=frame.samples
                    if samples>48000*self.registry.settings.direction.speech_max_s:raise ValueError('Generated speech exceeds eight seconds')
                    for packet in stream.encode(frame):target.mux(packet)
                for packet in stream.encode(None):target.mux(packet)
            path.unlink();return output
        except BaseException:
            output.unlink(missing_ok=True);raise

    async def synthesize(self,text,storage,deadline,event):
        if not event.voice_id and self.protocol!='elevenlabs':raise ProviderFailure('configuration_missing','speech',hint='Select the configured voice in Event details')
        url=endpoint(self.config.get('BREADCAST_TTS_URL'),boundary='speech') if self.protocol!='elevenlabs' else None
        token=self.config.get('BREADCAST_TTS_API_KEY')
        remaining=deadline-time.time()
        if remaining<=0:raise TimeoutError('Speech deadline expired')
        path=None
        try:
            key=self.config.get('ELEVENLABS_API_KEY')
            default_headers={'xi-api-key':key} if self.protocol=='elevenlabs' and key else {}
            async with asyncio.timeout(remaining),httpx.AsyncClient(follow_redirects=False,headers=default_headers) as client:
                if self.protocol=='elevenlabs':
                    if not key:raise ProviderFailure('configuration_missing','speech',hint='Set ELEVENLABS_API_KEY on the VM')
                    speech_url=await self.elevenlabs(client,event,deadline)
                    payload={'json':{'text':text,'model_id':self.model}}
                    token=None
                elif self.protocol=='nvidia-nim':
                    if self.voices is None:
                        await request(client,'GET',url+'/v1/health/ready',token=token,deadline=deadline,boundary='speech')
                        metadata=await request(client,'GET',url+'/v1/metadata',token=token,deadline=deadline,boundary='speech')
                        models=[item['shortName'] for item in metadata.get('modelInfo',[])
                            if isinstance(item,dict) and isinstance(item.get('shortName'),str)]
                        selected=self.config.get('BREADCAST_TTS_MODEL')
                        if not selected and len(models)==1:selected=models[0]
                        if selected not in models:raise ValueError('Select the returned NVIDIA speech model identity')
                        self.model=selected;self.version=metadata.get('version')
                        self.voices=await request(client,'GET',url+'/v1/audio/list_voices',token=token,deadline=deadline,boundary='speech')
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
                        self.model,_=await discover(client,url,self.config.get('BREADCAST_TTS_MODEL'),token,deadline,boundary='speech')
                    route='/v1/audio/speech'
                    payload={'json':{'model':self.model,'input':text,'voice':event.voice_id,'response_format':'wav'}}
                else:raise ValueError('Speech protocol must be elevenlabs, nvidia-nim or openai')
                headers={'Authorization':'Bearer '+token} if token else {}
                async with client.stream('POST',speech_url if self.protocol=='elevenlabs' else url+route,headers=headers,**payload,
                    timeout=max(.01,deadline-time.time())) as response:
                    if not 200<=response.status_code<300:
                        raise http_failure(response.status_code,'speech',request_id=response.headers.get('request-id') or response.headers.get('x-request-id'))
                    fd,temporary=tempfile.mkstemp(prefix='.speech-',suffix='.mp3' if self.protocol=='elevenlabs' else '.wav',dir=storage.root)
                    path=Path(temporary);received=0
                    with os.fdopen(fd,'wb') as output:
                        async for part in response.aiter_bytes():
                            received+=len(part)
                            if received>self.registry.settings.direction.speech_asset_bytes:
                                raise ValueError('Speech output exceeds the mixer asset limit')
                            output.write(part)
            if self.protocol=='elevenlabs':path=self.wav(path)
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
                origin='provider',model_id=self.model,voice_id=self.voice_id if self.protocol=='elevenlabs' else event.voice_id,transcript=text,
                configuration_revision=self.registry.settings.configuration_revision)
        except httpx.TimeoutException:raise ProviderFailure('deadline_missed','speech') from None
        except httpx.HTTPError:raise ProviderFailure('transport_failed','speech') from None
        finally:
            if path:path.unlink(missing_ok=True)
