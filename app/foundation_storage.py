"""Immutable filesystem artifacts. The ledger alone owns retention and pins."""
from __future__ import annotations
import hashlib
import io
import os
from pathlib import Path
import shutil
import tempfile
import av
from foundation_records import MediaObject


def inspect_video(path):
    """Decode every frame. A probe or an existing file cannot establish readiness."""
    frames, times, sizes, last_duration = 0, [], set(), 0
    with av.open(str(path)) as media:
        video = media.streams.video[0]
        time_base = str(video.time_base)
        codec = video.codec_context.name
        for frame in media.decode(video):
            if frame.pts is None:
                raise ValueError('Recorded frame has no PTS')
            frames += 1
            times.append(frame.pts)
            last_duration=frame.duration
            sizes.add((frame.width, frame.height))
        format_name = media.format.name
    if not frames or len(sizes) != 1 or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError('Empty, discontinuous, or changing-geometry chunk')
    step = last_duration
    if step <= 0:
        raise ValueError('Chunk end cannot be established')
    width, height = next(iter(sizes))
    return dict(frames=frames, start=times[0], end=times[-1]+step, time_base=time_base,
                width=width, height=height, codec=codec, format=format_name)


class FileStorage:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, key):
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('Invalid content key')
        return self.root / key

    def put(self, source, format_name, decoded_frames):
        source = Path(source)
        digest = hashlib.sha256()
        fd, temporary = tempfile.mkstemp(prefix='.partial-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as out, source.open('rb') as incoming:
                for data in iter(lambda: incoming.read(1048576), b''):
                    digest.update(data); out.write(data)
                out.flush(); os.fsync(out.fileno())
            key = digest.hexdigest()
            artifact = MediaObject(key=key, sha256=key, size=Path(temporary).stat().st_size,
                                   format=format_name, decoded_frames=decoded_frames)
            try:
                os.link(temporary, self.path(key))
            except FileExistsError:
                self.inspect(artifact)
            # Persist the directory entry before a manifest can become ready.
            directory = os.open(self.root, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
            self.inspect(artifact)
            return artifact
        finally:
            Path(temporary).unlink(missing_ok=True)

    def inspect(self, artifact):
        path = self.path(artifact.key)
        try:
            if path.stat().st_size != artifact.size:
                raise ValueError('Artifact size changed')
            with path.open('rb') as data:
                if hashlib.file_digest(data, 'sha256').hexdigest() != artifact.sha256:
                    raise ValueError('Artifact hash changed')
        except FileNotFoundError as error:
            raise ValueError('Retained media is unavailable') from error
        return path

    def read(self, artifact):
        return self.inspect(artifact).read_bytes()

    def delete(self, artifact):
        self.path(artifact.key).unlink(missing_ok=True)

    def free_bytes(self):
        return shutil.disk_usage(self.root).free

    def publish_manifest(self, manifest):
        import json
        raw=json.dumps(manifest.model_dump(mode='json'),sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        key=hashlib.sha256(raw).hexdigest()
        directory=self.root/'manifests';directory.mkdir(exist_ok=True)
        target=directory/key
        fd,temp=tempfile.mkstemp(prefix='.partial-',dir=directory)
        try:
            with os.fdopen(fd,'wb') as output:output.write(raw);output.flush();os.fsync(output.fileno())
            try:os.link(temp,target)
            except FileExistsError:
                if target.read_bytes()!=raw:raise ValueError('Immutable manifest changed')
            directory_fd=os.open(directory,os.O_RDONLY)
            try:os.fsync(directory_fd)
            finally:os.close(directory_fd)
        finally:Path(temp).unlink(missing_ok=True)
        return {'key':key,'sha256':key,'size':len(raw),'path':str(target)}

    def inspect_manifest(self, manifest):
        import json
        raw=json.dumps(manifest.model_dump(mode='json'),sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        key=hashlib.sha256(raw).hexdigest();path=self.root/'manifests'/key
        try:actual=path.read_bytes()
        except FileNotFoundError as error:raise ValueError('Ready manifest is missing') from error
        if actual!=raw:raise ValueError('Ready manifest changed')
        return path
