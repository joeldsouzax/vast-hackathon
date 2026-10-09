"""Public provider failures contain codes and fixed application text, not bodies."""
import re

CODES={
    'configuration_missing':'Configuration is missing',
    'capability_unverified':'Provider capability is not verified',
    'auth_failed':'Authentication failed',
    'forbidden':'Access was refused',
    'rate_limited':'Provider rate limit was reached',
    'deadline_missed':'The work deadline expired',
    'canceled':'Work was canceled',
    'transport_failed':'Provider connection failed',
    'submission_unknown':'Upload outcome is unknown',
    'invalid_response':'Provider output has an unsupported format',
    'identity_mismatch':'Provider identity does not match the assigned team',
    'timing_unknown':'Media timing is unknown',
    'media_unavailable':'Required media is unavailable',
    'version_mismatch':'Provider model or configuration changed',
    'capacity_reached':'The work queue is full',
}
BOUNDARIES={'storage','jobs','yolo','cosmos','search','llm','speech'}


class ProviderFailure(ValueError):
    def __init__(self,code,boundary,*,hint=None,http_status=None,request_id=None,retryable=False):
        if code not in CODES or boundary not in BOUNDARIES:raise ValueError('Unknown provider failure code or boundary')
        self.code=code;self.boundary=boundary;self.hint=hint
        self.http_status=http_status;self.retryable=retryable
        self.request_id=request_id if isinstance(request_id,str) and re.fullmatch(r'[A-Za-z0-9_.:\-]{1,128}',request_id) else None
        super().__init__(f'{boundary}: {CODES[code]}'+(f'. {hint}' if hint else ''))

    def public(self):
        return {'code':self.code,'boundary':self.boundary,'reason':str(self),
            'http_status':self.http_status,'provider_request_id':self.request_id,'retryable':self.retryable}


def http_failure(status,boundary,*,request_id=None,read=False):
    code={401:'auth_failed',403:'forbidden',429:'rate_limited'}.get(status,
        'transport_failed' if status>=500 else 'invalid_response')
    return ProviderFailure(code,boundary,http_status=status,request_id=request_id,
        retryable=read and code in ('rate_limited','transport_failed'))


def public_failure(error,boundary):
    if isinstance(error,ProviderFailure):return error.public()
    from pydantic_ai.exceptions import ModelHTTPError
    from openai import APITimeoutError
    import httpx
    if isinstance(error,ModelHTTPError):
        headers=error.headers or {}
        return http_failure(error.status_code,boundary,request_id=headers.get('x-request-id')).public()
    if isinstance(error,(APITimeoutError,httpx.TimeoutException)):
        return ProviderFailure('deadline_missed',boundary).public()
    code='deadline_missed' if isinstance(error,TimeoutError) else 'invalid_response'
    return ProviderFailure(code,boundary).public()
