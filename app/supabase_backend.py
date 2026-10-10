"""Private Supabase clip storage and pgvector search through documented REST APIs."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote

import httpx
from gemini_api import https_origin
from provider_errors import ProviderFailure, http_failure


class SupabaseBackend:
    def __init__(self, config):
        self.config = config
        self.uploaded = set()
        self.verified = set()
        self.bucket_limit = 512*1024*1024

    def configured(self):
        return bool(self.config.get('SUPABASE_URL') and
            (self.config.get('SUPABASE_SECRET_KEY') or self.config.get('SUPABASE_SERVICE_ROLE_KEY')))

    def connection(self):
        origin = https_origin(self.config.get('SUPABASE_URL'), 'storage')
        key = self.config.get('SUPABASE_SECRET_KEY') or self.config.get('SUPABASE_SERVICE_ROLE_KEY')
        if not key:
            raise ProviderFailure('configuration_missing', 'storage', hint='Set the server Supabase secret key')
        headers = {'apikey': key}
        # New sb_secret keys are API keys, not JWTs.
        if not key.startswith('sb_secret_'):
            headers['Authorization'] = 'Bearer '+key
        return origin, headers

    async def request(self, client, method, path, deadline, *, boundary='storage', payload=None,
            params=None, headers=None):
        origin, auth = self.connection()
        remaining = deadline-time.time()
        if remaining <= 0:
            raise ProviderFailure('deadline_missed', boundary)
        try:
            async with asyncio.timeout(remaining):
                response = await client.request(method, origin+path, json=payload, params=params,
                    headers={**auth, **(headers or {})}, timeout=remaining)
                if response.status_code not in (200, 201, 204):
                    raise http_failure(response.status_code, boundary, request_id=response.headers.get('x-request-id'))
                if len(response.content) > 2*1024*1024:
                    raise ProviderFailure('capacity_reached', boundary)
                return response.json() if response.content else None
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise ProviderFailure('deadline_missed', boundary) from None
        except httpx.HTTPError:
            raise ProviderFailure('transport_failed', boundary) from None
        except (ValueError, UnicodeError) as error:
            if isinstance(error, ProviderFailure):
                raise
            raise ProviderFailure('invalid_response', boundary) from None

    async def prepare(self, client, deadline):
        bucket = self.config.get('BREADCAST_SUPABASE_BUCKET', 'breadcast-clips')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', bucket):
            raise ProviderFailure('configuration_missing', 'storage', hint='Supabase bucket name is invalid')
        result = await self.request(client, 'GET', '/storage/v1/bucket/'+bucket, deadline)
        if not isinstance(result, dict) or result.get('id') != bucket or result.get('public') is not False:
            raise ProviderFailure('capability_unverified', 'storage', hint='Create the private clip bucket with the supplied migration')
        limit = result.get('file_size_limit')
        if type(limit) is int and limit > 0:
            self.bucket_limit = min(self.bucket_limit, limit)
        await self.request(client, 'GET', '/rest/v1/breadcast_clips', deadline,
            params={'select': 'id', 'limit': '1'})

    async def put_clip(self, client, path, metadata, deadline):
        path = Path(path)
        size = path.stat().st_size
        if not 0 < size <= self.bucket_limit:
            raise ProviderFailure('capacity_reached', 'storage')
        digest = metadata['sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ProviderFailure('invalid_response', 'storage')
        with path.open('rb') as incoming:
            if hashlib.file_digest(incoming, 'sha256').hexdigest() != digest:
                raise ProviderFailure('media_unavailable', 'storage', hint='Local clip bytes changed')
        bucket = self.config.get('BREADCAST_SUPABASE_BUCKET', 'breadcast-clips')
        scope = hashlib.sha256((metadata['event_id']+':'+metadata['run_id']).encode()).hexdigest()
        object_path = scope+'/'+digest+'.mp4'
        url_path = quote(bucket+'/'+object_path, safe='/')
        origin, auth = self.connection()
        cache_key = (origin, bucket, object_path)
        if cache_key not in self.uploaded:
            async def contents():
                with path.open('rb') as incoming:
                    while chunk := incoming.read(1048576):
                        if time.time() >= deadline:
                            raise ProviderFailure('deadline_missed', 'storage')
                        yield chunk
            remaining = deadline-time.time()
            if remaining <= 0:
                raise ProviderFailure('deadline_missed', 'storage')
            try:
                async with asyncio.timeout(remaining):
                    response = await client.post(origin+'/storage/v1/object/'+url_path, content=contents(),
                        headers={**auth, 'Content-Type': 'video/mp4', 'Content-Length': str(size), 'x-upsert': 'false'},
                        timeout=remaining)
                    # Existing immutable objects are reconciled by reading their bytes.
                    if response.status_code not in (200, 201, 400, 409):
                        raise http_failure(response.status_code, 'storage')
                    actual = hashlib.sha256()
                    received = 0
                    async with client.stream('GET', origin+'/storage/v1/object/authenticated/'+url_path,
                            headers=auth, timeout=max(.01, deadline-time.time())) as stored:
                        if stored.status_code != 200:
                            raise http_failure(stored.status_code, 'storage')
                        async for chunk in stored.aiter_bytes(1048576):
                            received += len(chunk)
                            if received > size or time.time() >= deadline:
                                raise ProviderFailure('media_unavailable', 'storage')
                            actual.update(chunk)
                    if received != size or actual.hexdigest() != digest:
                        raise ProviderFailure('media_unavailable', 'storage', hint='Supabase clip SHA-256 does not match')
            except (httpx.TimeoutException, asyncio.TimeoutError):
                raise ProviderFailure('deadline_missed', 'storage') from None
            except httpx.HTTPError:
                raise ProviderFailure('transport_failed', 'storage') from None
            self.uploaded.add(cache_key)
            if len(self.uploaded) > 256:
                self.uploaded.pop()
        body = {**metadata, 'bucket': bucket, 'object_path': object_path, 'bytes': size}
        body['id'] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        await self.request(client, 'POST', '/rest/v1/breadcast_clips', deadline, payload=body,
            headers={'Prefer': 'resolution=ignore-duplicates,return=minimal'})
        saved = await self.request(client, 'GET', '/rest/v1/breadcast_clips', deadline,
            params={'id': 'eq.'+body['id'], 'select': ','.join(body)})
        if saved != [body]:
            raise ProviderFailure('identity_mismatch', 'storage', hint='Stored clip metadata changed')
        self.verified.add('storage')
        return body

    async def put_scene(self, client, entry, vector, model_version, deadline):
        scene = entry['scene']
        source = scene['source']
        row = {'event_id': source['event_id'], 'run_id': source['run_id'],
            'scene_id': scene['scene_id'], 'revision': scene['revision'],
            'index_version': entry['index_version'], 'embedding_version': entry['embedding_version'],
            'model_version': model_version, 'embedding': list(vector), 'body': entry}
        await self.request(client, 'POST', '/rest/v1/breadcast_scenes', deadline, boundary='search', payload=row,
            headers={'Prefer': 'resolution=ignore-duplicates,return=minimal'})

    async def search(self, client, query, vector, model_version, scene_ids, deadline):
        found = await self.request(client, 'POST', '/rest/v1/rpc/breadcast_search', deadline, boundary='search',
            payload={'p_event_id': query.event_id, 'p_run_id': query.run_id,
                'p_index_version': query.index_version, 'p_embedding_version': query.embedding_version,
                'p_model_version': model_version, 'p_embedding': list(vector),
                'p_scene_ids': scene_ids, 'p_limit': query.limit})
        if not isinstance(found, list):
            raise ProviderFailure('invalid_response', 'search')
        return found
