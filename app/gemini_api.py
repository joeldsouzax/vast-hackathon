"""Gemini REST/SSE transport. Partial output never has controller authority."""
from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import urlsplit

import httpx
from provider_errors import ProviderFailure, http_failure

MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,191}')
SCHEMA_KEYS = {'$defs', '$ref', 'type', 'title', 'description', 'enum', 'items',
    'minItems', 'maxItems', 'minimum', 'maximum', 'anyOf', 'properties',
    'additionalProperties', 'required'}


def json_schema(value):
    """Convert Pydantic schema metadata; local validation keeps every constraint."""
    if isinstance(value, list):
        return [json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == 'const':
            result['enum'] = [item]
        elif key == 'oneOf':
            result['anyOf'] = json_schema(item)
        elif key in ('$defs', 'properties'):
            result[key] = {name: json_schema(schema) for name, schema in item.items()}
        elif key in SCHEMA_KEYS:
            result[key] = json_schema(item)
    return result


def https_origin(value, boundary):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError
        return value.rstrip('/')
    except (ValueError, TypeError):
        raise ProviderFailure('configuration_missing', boundary, hint='Set a valid HTTPS origin') from None


class GeminiAPI:
    def __init__(self, config):
        self.config = config
        self.last_stream = {}

    def configured(self):
        return bool(self.config.get('GEMINI_API_KEY') or
            self.config.get('SUPABASE_URL') and self.config.get('BREADCAST_RUNTIME_KEY'))

    def route(self, operation, model=None, params=None):
        if model and not MODEL.fullmatch(model):
            raise ProviderFailure('configuration_missing', 'llm', hint='Gemini model ID is invalid')
        # Hosted inference keeps the Gemini key only in Supabase function secrets.
        if self.config.get('SUPABASE_URL') and self.config.get('BREADCAST_RUNTIME_KEY'):
            origin = https_origin(self.config['SUPABASE_URL'], 'llm')
            return origin+'/functions/v1/gemini', {
                'x-breadcast-runtime-key': self.config['BREADCAST_RUNTIME_KEY']}, {
                'operation': operation, 'model': model, 'params': params or {}}
        key = self.config.get('GEMINI_API_KEY')
        if not key:
            raise ProviderFailure('configuration_missing', 'llm', hint='Configure the Supabase Gemini function or GEMINI_API_KEY')
        origin = https_origin(self.config.get('BREADCAST_GEMINI_BASE_URL',
            'https://generativelanguage.googleapis.com'), 'llm')
        path = '/v1beta/models' if operation == 'models' else '/v1beta/models/'+model+':'+operation
        return origin+path, {'x-goog-api-key': key}, None

    async def request(self, client, operation, deadline, *, boundary, model=None, payload=None, params=None):
        url, headers, envelope = self.route(operation, model, params)
        remaining = deadline-time.time()
        if remaining <= 0:
            raise ProviderFailure('deadline_missed', boundary)
        if envelope is not None:
            method, body, query = 'POST', {**envelope, 'payload': payload}, None
        else:
            method, body, query = ('GET' if operation == 'models' else 'POST'), payload, params
        try:
            async with asyncio.timeout(remaining):
                response = await client.request(method, url, headers=headers, json=body, params=query,
                    timeout=httpx.Timeout(remaining, connect=min(5, remaining)))
                if response.status_code != 200:
                    raise http_failure(response.status_code, boundary,
                        request_id=response.headers.get('x-request-id'))
                if len(response.content) > 2*1024*1024:
                    raise ProviderFailure('invalid_response', boundary)
                result = response.json()
                if not isinstance(result, dict) or 'error' in result:
                    raise ProviderFailure('invalid_response', boundary)
                return result
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise ProviderFailure('deadline_missed', boundary) from None
        except httpx.HTTPError:
            raise ProviderFailure('transport_failed', boundary) from None
        except (json.JSONDecodeError, UnicodeError):
            raise ProviderFailure('invalid_response', boundary) from None

    async def generate(self, client, model, parts, deadline, *, boundary, schema=None, instructions=None,
            generation=None, output_limit=131072):
        config = {'maxOutputTokens': 4096, **(generation or {})}
        if schema is not None:
            config['responseFormat'] = {'text': {'mimeType': 'application/json', 'schema': json_schema(schema)}}
        payload = {'contents': [{'role': 'user', 'parts': parts}], 'generationConfig': config}
        if instructions:
            payload['systemInstruction'] = {'parts': [{'text': instructions}]}
        # Leave room for JSON overhead below Gemini's 20 MB inline-request limit.
        if len(json.dumps(payload).encode()) > 19*1024*1024:
            raise ProviderFailure('capacity_reached', boundary, hint='Gemini inline request exceeds its byte limit')
        url, headers, envelope = self.route('streamGenerateContent', model, {'alt': 'sse'})
        body = {**envelope, 'payload': payload} if envelope else payload
        params = None if envelope else {'alt': 'sse'}
        remaining = deadline-time.time()
        if remaining <= 0:
            raise ProviderFailure('deadline_missed', boundary)
        started = time.monotonic()
        metrics = {'state': 'streaming', 'model_id': model, 'chunks': 0, 'first_chunk_s': None}
        self.last_stream[boundary] = metrics
        text, audio, event_lines = [], [], []
        text_bytes = audio_bytes = wire_bytes = event_bytes = 0
        finish = version = None

        def accept(lines):
            nonlocal text_bytes, audio_bytes, finish, version
            import base64
            try:
                chunk = json.loads('\n'.join(lines))
                if not isinstance(chunk, dict) or 'error' in chunk or chunk.get('promptFeedback', {}).get('blockReason'):
                    raise ValueError
                returned_version = chunk.get('modelVersion')
                if returned_version:
                    if version and version != returned_version:
                        raise ValueError
                    version = returned_version
                candidates = chunk.get('candidates', [])
                if len(candidates) > 1:
                    raise ValueError
                for candidate in candidates:
                    if candidate.get('index', 0) != 0:
                        raise ValueError
                    if candidate.get('finishReason'):
                        finish = candidate['finishReason']
                    for part in candidate.get('content', {}).get('parts', []):
                        if part.get('thought'):
                            continue
                        if 'text' in part:
                            value = part['text']
                            text_bytes += len(value.encode())
                            if text_bytes > output_limit:
                                raise ValueError
                            text.append(value)
                        if 'inlineData' in part:
                            inline = part['inlineData']
                            if inline.get('mimeType') not in ('audio/L16;codec=pcm;rate=24000', 'audio/pcm;rate=24000',
                                    'audio/l16', 'audio/L16'):
                                raise ValueError
                            value = base64.b64decode(inline['data'], validate=True)
                            audio_bytes += len(value)
                            if audio_bytes > output_limit:
                                raise ValueError
                            audio.append(value)
                metrics['chunks'] += 1
                if metrics['first_chunk_s'] is None:
                    metrics['first_chunk_s'] = round(time.monotonic()-started, 6)
            except (ValueError, KeyError, TypeError, AttributeError):
                raise ProviderFailure('invalid_response', boundary) from None

        try:
            async with asyncio.timeout(remaining), client.stream('POST', url, json=body, headers=headers,
                    params=params, timeout=httpx.Timeout(remaining, connect=min(5, remaining))) as response:
                if response.status_code != 200:
                    raise http_failure(response.status_code, boundary,
                        request_id=response.headers.get('x-request-id'))
                if 'text/event-stream' not in response.headers.get('content-type', ''):
                    raise ProviderFailure('invalid_response', boundary)
                async for line in response.aiter_lines():
                    size = len(line.encode())
                    wire_bytes += size
                    event_bytes += size
                    if wire_bytes > 8*1024*1024 or event_bytes > 2*1024*1024:
                        raise ProviderFailure('capacity_reached', boundary)
                    if not line:
                        if event_lines:
                            accept(event_lines)
                        event_lines = []
                        event_bytes = 0
                    elif line.startswith('data:'):
                        event_lines.append(line[5:].lstrip())
                if event_lines:
                    accept(event_lines)
                if finish != 'STOP' or not text and not audio:
                    raise ProviderFailure('invalid_response', boundary, hint='Gemini stream did not finish successfully')
                if time.time() >= deadline:
                    raise ProviderFailure('deadline_missed', boundary)
                metrics.update(state='complete', elapsed_s=round(time.monotonic()-started, 6),
                    returned_model_version=version, finish_reason=finish)
                return {'text': ''.join(text), 'pcm': b''.join(audio), 'model_version': version}
        except (asyncio.TimeoutError, httpx.TimeoutException):
            metrics['state'] = 'failed'
            raise ProviderFailure('deadline_missed', boundary) from None
        except httpx.HTTPError:
            metrics['state'] = 'failed'
            raise ProviderFailure('transport_failed', boundary) from None
        except BaseException:
            metrics['state'] = 'failed'
            raise
