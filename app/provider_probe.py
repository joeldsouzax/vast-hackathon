"""Explicit, read-only verification of assigned VSS tenant access.

This command does not enable adapters, upload media, or start a broadcast.
Credentials are read only from the assigned /config/*.config file.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import shlex
import signal
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_JSON_BYTES = 2 * 1024 * 1024
CALL_SECONDS = 5.0
TOTAL_SECONDS = 30.0
KNOWN_FIELDS = frozenset((
    'status', 'healthy', 'ready', 'version', 'backend_version', 'username',
    'access_token', 'token_type', 's3_endpoint', 'vdb_endpoint', 'vdb_collection',
    'vdb_schema', 'embedding_host', 'embedding_dims', 'cosmos_host',
))


class ProbeFailure(Exception):
    """Only fixed reason codes can enter the public report."""
    def __init__(self, code: str, *, blocked=False):
        self.code, self.blocked = code, blocked
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProbeFailure('redirect_rejected')


@contextmanager
def bounded_time(seconds):
    # Explicit CLI execution has a main thread on the supplied Linux runtime.
    # SIGALRM also bounds DNS and slow response bodies, which socket timeouts alone
    # cannot bound. Unsupported execution contexts fail before any request.
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, 'setitimer'):
        raise ProbeFailure('deadline_unavailable', blocked=True)
    previous_handler = signal.getsignal(signal.SIGALRM)
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    began = time.monotonic()
    def expired(signum, frame):
        raise ProbeFailure('timeout')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, remaining) if remaining else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if remaining:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, remaining - (time.monotonic() - began)), interval)


def assigned_values(directory):
    try:
        files = sorted(Path(directory).glob('*.config'))
    except OSError:
        raise ProbeFailure('assigned_config_unreadable_or_invalid', blocked=True) from None
    if len(files) != 1 or not files[0].is_file():
        raise ProbeFailure('assigned_config_missing_or_ambiguous', blocked=True)
    try:
        with files[0].open('rb') as source:
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ProbeFailure('assigned_config_too_large', blocked=True)
        text = raw.decode('utf-8')
        values = {}
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            match = re.fullmatch(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
            if not match:
                raise ProbeFailure('assigned_config_invalid', blocked=True)
            # Parse quoting, never execute source, commands, variables, or expansion.
            parts = shlex.split(match[2], comments=False, posix=True)
            if len(parts) > 1 and not parts[1].startswith('#'):
                raise ProbeFailure('assigned_config_invalid', blocked=True)
            key, value = match[1], parts[0] if parts else ''
            if key in values:
                raise ProbeFailure('assigned_config_duplicate_key', blocked=True)
            values[key] = value
    except (OSError, UnicodeError, ValueError):
        raise ProbeFailure('assigned_config_unreadable_or_invalid', blocked=True) from None
    return values


def assigned_config(directory):
    values = assigned_values(directory)
    if any(not values.get(key) for key in ('INGRESS_URL', 'USERNAME', 'PASSWORD')):
        raise ProbeFailure('assigned_config_missing_required_fields', blocked=True)
    endpoint = values['INGRESS_URL'].rstrip('/')
    try:
        url = urllib.parse.urlsplit(endpoint)
        valid = (url.scheme in ('http', 'https') and url.hostname and
            url.username is None and url.password is None and not url.query and not url.fragment and
            not any(c.isspace() or ord(c) < 32 or c in '<>\\' for c in endpoint))
        _ = url.port
    except ValueError:
        valid = False
    if not valid:
        raise ProbeFailure('assigned_endpoint_invalid', blocked=True)
    return endpoint, values['USERNAME'], values['PASSWORD']


def shape(value):
    if isinstance(value, dict):
        # Arbitrary keys and values may contain credentials. Report only known
        # contract field names and types; preserve the count of other fields.
        known = {key: type(item).__name__ for key, item in value.items() if key in KNOWN_FIELDS}
        return {'type': 'object', 'fields': known, 'other_field_count': len(value) - len(known)}
    return {'type': type(value).__name__}


def returned_version(values, sensitive):
    for data in reversed(values):
        for key in ('backend_version', 'version'):
            version = data.get(key)
            if (isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+\-]{0,79}', version)
                    and not any(secret and secret in version for secret in sensitive)):
                return version
    return None


def run_probe(config_dir=Path('/config'), *, open_url=None):
    began = time.monotonic()
    report = {'schema_version': '1', 'run_id': secrets.token_hex(12),
        'scope': 'read_only_tenant_access', 'fixture': False, 'endpoint': None,
        'transport': None, 'configuration_source': 'assigned_team_file', 'steps': [],
        'provider_version': None, 'configuration_sha256': None, 'failure_reason': None,
        'gates': {f'G{i:02}': 'unverified' for i in range(1, 10)}}
    try:
        with bounded_time(TOTAL_SECONDS):
            endpoint, username, password = assigned_config(config_dir)
            report['endpoint'] = endpoint
            report['transport'] = urllib.parse.urlsplit(endpoint).scheme
            report['requested_identity_sha256'] = hashlib.sha256(username.encode()).hexdigest()
            report['input_binding_sha256'] = hashlib.sha256(json.dumps(
                {'endpoint': endpoint, 'username_sha256': report['requested_identity_sha256']}, sort_keys=True).encode()).hexdigest()
            opener = open_url or urllib.request.build_opener(NoRedirect(),
                urllib.request.HTTPSHandler(context=ssl.create_default_context())).open

            def request(name, path, *, payload=None, token=None):
                step = {'step': name, 'method': 'POST' if payload is not None else 'GET',
                    'path': path, 'http_status': None, 'elapsed_seconds': None}
                report['steps'].append(step)
                headers = {'Accept': 'application/json'}
                if payload is not None:
                    headers['Content-Type'] = 'application/json'
                if token:
                    headers['Authorization'] = 'Bearer ' + token
                url = endpoint + path
                req = urllib.request.Request(url, json.dumps(payload).encode() if payload is not None else None, headers)
                started = time.monotonic()
                try:
                    budget = min(CALL_SECONDS, TOTAL_SECONDS - (started - began))
                    if budget <= 0:
                        raise ProbeFailure('timeout')
                    with bounded_time(budget), opener(req, timeout=budget) as response:
                        if response.geturl() != url:
                            raise ProbeFailure('redirect_rejected')
                        step['http_status'] = response.status
                        if response.status != 200:
                            raise ProbeFailure('http_status_failure')
                        raw = response.read(MAX_JSON_BYTES + 1)
                        if len(raw) > MAX_JSON_BYTES:
                            raise ProbeFailure('response_too_large')
                        def invalid_constant(value):
                            raise ProbeFailure('invalid_json_response')
                        def unique_fields(pairs):
                            result = {}
                            for key, item in pairs:
                                if key in result:
                                    raise ProbeFailure('invalid_json_response')
                                result[key] = item
                            return result
                        value = json.loads(raw, parse_constant=invalid_constant, object_pairs_hook=unique_fields)
                        if not isinstance(value, dict):
                            raise ProbeFailure('invalid_json_object')
                        step['response_bytes'] = len(raw)
                        step['response_shape'] = shape(value)
                        if name == 'configuration':
                            report['configuration_sha256'] = hashlib.sha256(raw).hexdigest()
                        return value
                except urllib.error.HTTPError as error:
                    step['http_status'] = error.code
                    error.close()
                    raise ProbeFailure('redirect_rejected' if 300 <= error.code < 400 else 'http_status_failure') from None
                except (urllib.error.URLError, OSError):
                    raise ProbeFailure('transport_failure') from None
                except (ValueError, UnicodeError):
                    raise ProbeFailure('invalid_json_response') from None
                finally:
                    step['elapsed_seconds'] = round(time.monotonic() - started, 6)

            health = request('health', '/health')
            if health.get('healthy') is False or health.get('ready') is False or health.get('status') in ('error', 'unhealthy', 'failed'):
                raise ProbeFailure('backend_unhealthy')
            login = request('login', '/api/v1/auth/login', payload={'username': username, 'password': password})
            token = login.get('access_token')
            if (not isinstance(token, str) or not 1 <= len(token) <= 8192 or
                    not re.fullmatch(r'[\x21-\x7e]+', token) or
                    not isinstance(login.get('token_type'), str) or login['token_type'].lower() != 'bearer'):
                raise ProbeFailure('invalid_login_response')
            if 'username' in login and login['username'] != username:
                raise ProbeFailure('identity_mismatch')
            identity = request('identity', '/api/v1/auth/me', token=token)
            report['identity_matches'] = identity.get('username') == username
            if not report['identity_matches']:
                raise ProbeFailure('identity_mismatch')
            configuration = request('configuration', '/api/v1/config', token=token)
            report['provider_version'] = returned_version((health, configuration), (username, password, token))
            report['gates']['G01'] = 'verified'
            report['outcome'] = 'verified'
    except ProbeFailure as error:
        report['failure_reason'] = error.code
        report['outcome'] = 'blocked' if error.blocked else 'failed'
        report['gates']['G01'] = report['outcome']
    except Exception:
        # Never serialize provider exception text, response bodies, or config input.
        report['failure_reason'] = 'unexpected_probe_failure'
        report['outcome'] = 'failed'
        report['gates']['G01'] = 'failed'
    finally:
        report['elapsed_run_seconds'] = round(time.monotonic() - began, 6)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Verify assigned VSS tenant access without media or deployment changes.')
    parser.add_argument('--evidence', type=Path, help='Optional sanitized JSON report path; no credentials are saved')
    args = parser.parse_args(argv)
    report = run_probe()
    serialized = json.dumps(report, indent=2, allow_nan=False) + '\n'
    if args.evidence:
        try:
            args.evidence.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with args.evidence.open('x') as output:
                output.write(serialized)
        except OSError:
            report['evidence_write_failed'] = True
            report['failure_reason'] = 'evidence_write_failed'
            report['outcome'] = 'failed'
            serialized = json.dumps(report, indent=2, allow_nan=False) + '\n'
    print(serialized, end='')
    return 0 if report['outcome'] == 'verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
