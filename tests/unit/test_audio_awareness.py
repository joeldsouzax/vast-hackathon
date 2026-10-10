"""Audio proxy media and transcript contract checks; no model calls."""
import asyncio
from array import array
import io
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

import av
from pydantic import ValidationError

from foundation_records import AudioEvidence
from live_gpu import proxy


class AudioAwareness(unittest.TestCase):
    def test_unclear_audio_cannot_supply_words(self):
        for speech in ('none', 'unclear'):
            with self.assertRaises(ValidationError):
                AudioEvidence(speech=speech, transcript='Invented words')
            self.assertEqual(AudioEvidence(speech=speech).transcript, '')

    def test_meaning_requires_words_and_preserves_old_records(self):
        for speech in ('none','unclear','foreground','background'):
            with self.assertRaises(ValidationError):
                AudioEvidence(speech=speech,meaning='A voice demo')
        heard=AudioEvidence(speech='foreground',transcript='We built a voice demo.',meaning='They built a voice demo.')
        self.assertNotEqual(heard.meaning,heard.transcript)
        old={'speech':'background','transcript':'Hello'}
        self.assertEqual(AudioEvidence.model_validate(old).model_dump(),old)

    def test_trimmed_proxy_preserves_audio_and_video_only_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.mp4'
            subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error',
                '-f','lavfi','-i','color=c=green:s=320x180:r=15:d=3',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=3',
                '-c:v','libx264','-threads','1','-c:a','aac','-shortest',str(source)],check=True)
            # Two contiguous chunks with nonzero source timestamps. Trim one
            # second from each; the original files each contain three seconds.
            manifests=[SimpleNamespace(media=source,native=SimpleNamespace(start=1000,end=4000),timeline_offset_pts=1000),
                SimpleNamespace(media=source,native=SimpleNamespace(start=4000,end=7000),timeline_offset_pts=4000)]
            window=SimpleNamespace(source=SimpleNamespace(time_base='1/1000'),native=SimpleNamespace(start=3000,end=5000))
            storage=SimpleNamespace(root=root,inspect=lambda path:path)
            for include_audio in (True,False):
                result=asyncio.run(proxy(window,manifests,storage,include_audio=include_audio))
                with av.open(io.BytesIO(result)) as media:
                    self.assertEqual(bool(media.streams.audio),include_audio)
                    self.assertAlmostEqual(media.duration/av.time_base,2,delta=.1)
                    if include_audio:
                        frames=list(media.decode(audio=0))
                        self.assertGreater(sum(f.samples for f in frames),30000)
                        samples=array('f')
                        for frame in frames:
                            self.assertEqual(frame.format.name,'fltp')
                            samples.frombytes(bytes(frame.planes[0])[:frame.samples*4])
                        self.assertGreater(max(abs(v) for v in samples),.01)
            muted=root/'muted.mp4'
            subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-i',str(source),'-c:v','copy','-an',str(muted)],check=True)
            manifests[1].media=muted
            result=asyncio.run(proxy(window,manifests,storage,include_audio=True))
            with av.open(io.BytesIO(result)) as media:self.assertFalse(media.streams.audio)
