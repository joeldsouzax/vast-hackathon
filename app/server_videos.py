"""Explicit operator staging of configured server videos; no model or airtime work."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlsplit
import uuid

DEFAULT_MAX_FILE_BYTES=512*1024*1024
DEFAULT_DISK_RESERVE_BYTES=2*1024*1024*1024


class StageError(ValueError):
    """Safe error text. Never include an SDK exception or secret configuration."""


@dataclass(frozen=True)
class Video:
    id: str
    name: str
    uri: str
    bucket: str | None
    key: str | None
    version_id: str | None
    expected_sha256: str | None
    local_path: Path | None=None


@dataclass(frozen=True)
class Settings:
    videos: tuple[Video,...]
    max_file_bytes: int=DEFAULT_MAX_FILE_BYTES
    disk_reserve_bytes: int=DEFAULT_DISK_RESERVE_BYTES


def load_config(path):
    if path is None:raise StageError('Set BREADCAST_SERVER_VIDEOS_CONFIG or pass --config with the configured server video list')
    try:
        with Path(path).open('rb') as incoming:raw=incoming.read(65537)
        if len(raw)>65536:raise StageError('Server video configuration exceeds 64 KiB')
        data=json.loads(raw)
    except StageError:raise
    except (OSError,ValueError):raise StageError('Server video configuration is unreadable or invalid JSON') from None
    if not isinstance(data,dict) or set(data)-{'schema_version','videos','max_file_bytes','disk_reserve_bytes'}:
        raise StageError('Server video configuration contains unsupported fields')
    if type(data.get('schema_version')) is not int or data['schema_version']!=1:
        raise StageError('Server video configuration requires schema_version 1')
    rows=data.get('videos')
    if not isinstance(rows,list) or not 1<=len(rows)<=2:raise StageError('Configure one or two server videos')
    videos=[];ids=set()
    for row in rows:
        if not isinstance(row,dict) or set(row)-{'id','name','uri','version_id','expected_sha256'}:
            raise StageError('Server video entry contains unsupported fields')
        identity=row.get('id');name=row.get('name');uri=row.get('uri')
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',identity) or identity in ids:
            raise StageError('Each server video requires a distinct safe id')
        if not isinstance(name,str) or not name.strip() or len(name)>200 or any(ord(c)<32 for c in name):
            raise StageError('Each server video requires a name of at most 200 characters')
        if not isinstance(uri,str) or len(uri)>4096 or any(ord(c)<32 for c in uri):raise StageError('Server video requires a valid s3:// or file: URI')
        local_path=None;bucket=key=None
        try:
            parsed=urlsplit(uri)
            if parsed.scheme not in ('s3','file') or '?' in uri or '#' in uri or parsed.username or parsed.password:
                raise ValueError
            decoded=unquote(parsed.path,errors='strict')
            if not decoded or any(ord(c)<32 for c in decoded):raise ValueError
            if parsed.scheme=='s3':
                if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',parsed.netloc) or not decoded.startswith('/') or len(decoded)<2:raise ValueError
                bucket=parsed.netloc;key=decoded[1:]
            else:
                if parsed.netloc:raise ValueError
                candidate=Path(decoded)
                local_path=(candidate if candidate.is_absolute() else Path(path).resolve().parent/candidate).resolve()
        except (ValueError,UnicodeError):raise StageError('Server video URI must be s3://bucket/key or file:path without credentials, query, or fragment') from None
        version=row.get('version_id')
        if version is not None and (not isinstance(version,str) or not version or len(version)>1024 or any(ord(c)<32 for c in version)):
            raise StageError('Server video version_id is invalid')
        if local_path is not None and version is not None:raise StageError('version_id applies only to S3 server videos')
        expected=row.get('expected_sha256')
        if expected is not None and (not isinstance(expected,str) or not re.fullmatch(r'[0-9a-fA-F]{64}',expected)):
            raise StageError('Server video expected_sha256 must contain 64 hexadecimal characters')
        videos.append(Video(identity,name,uri,bucket,key,version,expected.lower() if expected else None,local_path));ids.add(identity)
    maximum=data.get('max_file_bytes',DEFAULT_MAX_FILE_BYTES)
    reserve=data.get('disk_reserve_bytes',DEFAULT_DISK_RESERVE_BYTES)
    if type(maximum) is not int or not 1<=maximum<=2*1024*1024*1024:raise StageError('max_file_bytes must be 1 byte through 2 GiB')
    if type(reserve) is not int or not 0<=reserve<=1024**4:raise StageError('disk_reserve_bytes must be 0 bytes through 1 TiB')
    return Settings(tuple(videos),maximum,reserve)


def s3_client(values=None):
    values=values or {}
    def setting(key):return os.environ.get(key) or values.get(key)
    endpoint=setting('BREADCAST_S3_ENDPOINT_URL') or setting('S3_ENDPOINT') or None
    if endpoint is not None:
        try:
            parsed=urlsplit(endpoint)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:raise ValueError
        except ValueError:raise StageError('BREADCAST_S3_ENDPOINT_URL must be an HTTP URL without credentials, query, or fragment') from None
    try:
        import boto3
        from botocore.config import Config
        credentials={}
        if setting('ACCESS_KEY') and setting('SECRET_KEY'):
            credentials={'aws_access_key_id':setting('ACCESS_KEY'),'aws_secret_access_key':setting('SECRET_KEY')}
        return boto3.client('s3',endpoint_url=endpoint,config=Config(connect_timeout=5,read_timeout=5,
            retries={'total_max_attempts':1}),**credentials)
    except Exception:raise StageError('S3 client could not start; check the VM credential chain and endpoint configuration') from None


def check_cancelled(cancelled):
    if cancelled is not None and cancelled():raise StageError('Server video staging canceled')


def sha256_file(path,cancelled=None):
    digest=hashlib.sha256()
    with path.open('rb') as incoming:
        while True:
            check_cancelled(cancelled);chunk=incoming.read(1048576)
            if not chunk:break
            digest.update(chunk)
    check_cancelled(cancelled)
    return digest.hexdigest()


def probe_video(path,cancelled=None):
    process=None
    try:
        check_cancelled(cancelled)
        process=subprocess.Popen(['ffprobe','-v','error','-show_entries',
            'stream=codec_type,codec_name,width,height,time_base,duration,start_time,sample_aspect_ratio:stream_tags=rotate:stream_side_data=rotation:format=format_name,duration',
            '-of','json',str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        deadline=time.monotonic()+30
        while True:
            check_cancelled(cancelled)
            if time.monotonic()>=deadline:raise ValueError
            try:output,_=process.communicate(timeout=.2);break
            except subprocess.TimeoutExpired:pass
        if process.returncode or len(output)>2*1024*1024:raise ValueError
        data=json.loads(output);streams=data['streams']
        stream=next(item for item in streams if item.get('codec_type')=='video')
        width=stream['width'];height=stream['height'];time_base=stream['time_base']
        if type(width) is not int or type(height) is not int or not 1<=width<=16384 or not 1<=height<=16384 or Fraction(time_base)<=0:raise ValueError
        if not isinstance(stream.get('codec_name'),str) or not stream['codec_name']:raise ValueError
        def seconds(value):
            if value is None or value=='N/A':return None
            number=float(value)
            if not math.isfinite(number) or number<0:raise ValueError
            return number
        duration=seconds(stream.get('duration'));basis='video_stream' if duration is not None else None
        if duration is None:
            duration=seconds(data.get('format',{}).get('duration'))
            if duration is not None:basis='container'
        rotation=next((item['rotation'] for item in stream.get('side_data_list',[]) if 'rotation' in item),None)
        if rotation is None:rotation=stream.get('tags',{}).get('rotate')
        if rotation is not None:
            rotation=float(rotation)
            if not math.isfinite(rotation):raise ValueError
        return {'codec':stream['codec_name'],'format':data.get('format',{}).get('format_name'),
            'duration_s':duration,'duration_basis':basis,'time_base':time_base,
            'video_duration_s':seconds(stream.get('duration')),
            'audio_present':any(item.get('codec_type')=='audio' for item in streams),
            'width':width,'height':height,'rotation_deg':rotation,'sample_aspect_ratio':stream.get('sample_aspect_ratio')}
    except StageError:raise
    except (OSError,ValueError,ArithmeticError,KeyError,IndexError,StopIteration,TypeError,subprocess.SubprocessError):
        raise StageError('Staged object is not a readable video with valid ffprobe metadata') from None
    finally:
        if process is not None and process.poll() is None:
            process.kill();process.communicate()


def atomic_json(path, value,cancelled=None):
    fd,temporary=tempfile.mkstemp(prefix='.partial-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as output:
            json.dump(value,output,sort_keys=True,indent=2,allow_nan=False);output.write('\n');output.flush();os.fsync(output.fileno())
        check_cancelled(cancelled);os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:Path(temporary).unlink(missing_ok=True)


def local_identity(path):
    try:
        info=path.stat()
        if not path.is_file():raise OSError
        return {'kind':'local_file','path':str(path),'device':info.st_dev,'inode':info.st_ino,
            'size':info.st_size,'mtime_ns':info.st_mtime_ns}
    except OSError:raise StageError('Configured local server video is not a readable regular file') from None


def stage_video(video, settings, root, client,cancelled=None):
    check_cancelled(cancelled)
    if video.local_path is not None:
        remote=local_identity(video.local_path);size=remote['size'];etag=version=None;parameters={}
    else:
        parameters={'Bucket':video.bucket,'Key':video.key}
        if video.version_id is not None:parameters['VersionId']=video.version_id
        try:head=client.head_object(**parameters)
        except Exception:raise StageError('S3 HEAD failed; check object access, configured path, and version') from None
        size=head.get('ContentLength');etag=head.get('ETag');version=head.get('VersionId')
        if not isinstance(etag,str) or not etag:raise StageError('S3 HEAD did not return an object ETag for a conditional read')
        if video.version_id is not None and version!=video.version_id:raise StageError('S3 HEAD returned a different object version')
        if version is not None and not isinstance(version,str):raise StageError('S3 HEAD returned an invalid object version')
        remote={'bucket':video.bucket,'key':video.key,'version_id':version,'etag':etag}
    if type(size) is not int or not 0<size<=settings.max_file_bytes:raise StageError('Server video size is invalid or exceeds max_file_bytes')
    check_cancelled(cancelled)
    identity=hashlib.sha256(json.dumps({'id':video.id,'remote':remote},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    manifest_path=root/'manifests'/f'{identity}.json'
    if manifest_path.is_file():
        try:
            previous=json.loads(manifest_path.read_text())
            digest=previous['sha256']
            if not re.fullmatch(r'[0-9a-f]{64}',digest):raise ValueError
            target=root/digest
            if previous['remote']!=remote or previous['bytes']!=size or target.stat().st_size!=size:raise ValueError
            if previous.get('schema_version')!=1 or previous.get('source_kind')!='server_video' or previous.get('id')!=video.id or previous.get('uri')!=video.uri or previous.get('path')!=str(target):raise ValueError
            if previous.get('fixture') is not False or previous.get('live_sensor') is not False:raise ValueError
            actual=sha256_file(target,cancelled)
            if actual!=digest or (video.expected_sha256 is not None and actual!=video.expected_sha256):raise ValueError
            check_cancelled(cancelled);metadata=probe_video(target,cancelled)
            refreshed={**previous,'name':video.name,'metadata':metadata}
            if refreshed!=previous:atomic_json(manifest_path,refreshed,cancelled)
            check_cancelled(cancelled)
            return {**refreshed,'cached':True,'manifest_path':str(manifest_path)}
        except StageError:raise
        except (OSError,ValueError,KeyError,TypeError):raise StageError('Previously staged immutable video or manifest changed') from None
    if shutil.disk_usage(root).free<size+settings.disk_reserve_bytes:raise StageError('Insufficient disk space to preserve disk_reserve_bytes')
    get_parameters={**parameters,'IfMatch':etag}
    if version is not None:get_parameters['VersionId']=version
    body=None;temporary=None
    try:
        if video.local_path is not None:
            try:body=video.local_path.open('rb')
            except OSError:raise StageError('Configured local server video is unreadable') from None
        else:
            try:response=client.get_object(**get_parameters)
            except Exception:raise StageError('S3 conditional GET failed; object access or identity changed') from None
            body=response.get('Body')
            if body is None or not callable(getattr(body,'read',None)):raise StageError('S3 GET returned no readable body')
            if type(response.get('ContentLength')) is not int or response['ContentLength']!=size or response.get('ETag')!=etag or response.get('VersionId')!=version:
                raise StageError('S3 object changed between HEAD and GET')
        fd,temporary=tempfile.mkstemp(prefix='.partial-',dir=root)
        digest=hashlib.sha256();received=0
        with os.fdopen(fd,'wb') as output:
            while True:
                check_cancelled(cancelled)
                try:chunk=body.read(min(1048576,settings.max_file_bytes-received+1))
                except Exception:raise StageError('Server video stream failed before the video was complete') from None
                check_cancelled(cancelled)
                if not chunk:break
                if not isinstance(chunk,bytes):raise StageError('Server video stream returned invalid bytes')
                received+=len(chunk)
                if received>size or received>settings.max_file_bytes:raise StageError('Server video stream exceeded the declared object size or max_file_bytes')
                if shutil.disk_usage(root).free<len(chunk)+settings.disk_reserve_bytes:raise StageError('Server video stream exhausted the disk reserve')
                digest.update(chunk);output.write(chunk)
            output.flush();os.fsync(output.fileno())
        if received!=size:raise StageError('Server video stream ended before the declared object size')
        if video.local_path is not None and local_identity(video.local_path)!=remote:raise StageError('Local server video changed during staging')
        checksum=digest.hexdigest()
        if video.expected_sha256 is not None and checksum!=video.expected_sha256:raise StageError('Server video SHA-256 does not match expected_sha256')
        metadata=probe_video(Path(temporary),cancelled);target=root/checksum
        check_cancelled(cancelled)
        try:os.link(temporary,target)
        except FileExistsError:
            existing=sha256_file(target,cancelled)
            if existing!=checksum:raise StageError('Existing immutable video bytes changed') from None
        directory=os.open(root,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        manifest={'schema_version':1,'source_kind':'server_video','id':video.id,'name':video.name,
            'uri':video.uri,'remote':remote,'sha256':checksum,'bytes':received,'path':str(target),
            'metadata':metadata,'fixture':False,'live_sensor':False}
        atomic_json(manifest_path,manifest,cancelled)
        return {**manifest,'cached':False,'manifest_path':str(manifest_path)}
    finally:
        if body is not None:
            try:body.close()
            except Exception:pass
        if temporary is not None:Path(temporary).unlink(missing_ok=True)


def stage(config_path, runtime, client=None,cancelled=None):
    settings=load_config(config_path)
    check_cancelled(cancelled)
    root=Path(runtime).resolve()/'server-videos';root.mkdir(parents=True,exist_ok=True,mode=0o700)
    (root/'manifests').mkdir(exist_ok=True,mode=0o700)
    if client is None and any(video.local_path is None for video in settings.videos):client=s3_client()
    results=[]
    for video in settings.videos:
        check_cancelled(cancelled);results.append(stage_video(video,settings,root,client,cancelled))
    check_cancelled(cancelled)
    return results


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=os.environ.get('BREADCAST_SERVER_VIDEOS_CONFIG') or None)
    parser.add_argument('--runtime',type=Path,default=os.environ.get('BREADCAST_RUNTIME','.runtime'))
    parser.add_argument('--report',type=Path)
    args=parser.parse_args(argv);started=time.monotonic();run_id=uuid.uuid4().hex
    report_path=args.report or args.runtime/'server-video-checks'/run_id/'report.json'
    report={'run_id':run_id,'source_kind':'server_video','passed':False,'videos':[],
        'live_provider_verified':False,'physical_camera_verified':False}
    try:report['videos']=stage(args.config,args.runtime);report['passed']=True
    except StageError as error:report['failure']=str(error)
    except Exception:report['failure']='Server video staging failed; check local storage, binaries, and S3 access'
    report['elapsed_s']=time.monotonic()-started
    report_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_json(report_path,report)
    print(('Server videos staged' if report['passed'] else 'Server video staging blocked')+': '+str(report_path),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
