"""FastAPI routes preserving the existing Studio HTTP contract."""
from contextlib import asynccontextmanager
import functools
import hmac
import io
import json
import mimetypes
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, StrictStr
from starlette.concurrency import run_in_threadpool
import qrcode

ROOT = Path(__file__).resolve().parent
MEDIA_ROUTE = re.compile(r'^/(program|camera/[a-f0-9]{32})/(whip|whep)(?:/([a-f0-9-]+))?$')


class ChatRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    id: StrictStr
    text: StrictStr
    run_id: StrictStr


class AuthRequest(BaseModel):
    # MediaMTX owns the other authentication fields. Only these fields grant access.
    model_config = ConfigDict(strict=True, extra='ignore')
    action: str | None = None
    path: str | None = None
    token: str | None = None
    password: str | None = None
    ip: str | None = None


def send(status, data=b'', content_type='application/json', headers=None):
    if isinstance(data,(dict,list)):data=json.dumps(data,allow_nan=False).encode()
    if isinstance(data,str):data=data.encode()
    return Response(data,status_code=status,media_type=content_type,
        headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer',**(headers or {})})


async def body(request):
    size=0; chunks=[]
    async for part in request.stream():
        size+=len(part)
        if size>65536:raise ValueError('Request body exceeds the experiment limit')
        chunks.append(part)
    return b''.join(chunks)


async def object_body(request):
    data=json.loads(await body(request))
    if not isinstance(data,dict):raise ValueError('Expected a JSON object')
    return data


def errors(api):
    from replay_work import ReplayError
    async def replay_error(request,error):return send(error.status,{'error':str(error),'reason':error.code})
    api.add_exception_handler(ReplayError,replay_error)
    async def conflict(request,error):
        # Pydantic errors include rejected input. Keep credentials out of error bodies.
        from pydantic import ValidationError
        message='Invalid request fields or values' if isinstance(error,ValidationError) else str(error)
        return send(409,{'error':message})
    for cls in (ValueError,TypeError,KeyError):api.add_exception_handler(cls,conflict)
    async def denied(request,error):return send(403,{'error':str(error)})
    api.add_exception_handler(PermissionError,denied)
    async def gateway(request,error):return send(503,{'error':'Media gateway is unavailable; read the local logs'})
    api.add_exception_handler(urllib.error.URLError,gateway)


def auth_api(app):
    api=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
    @api.post('/auth')
    async def auth(request:Request):
        try:
            data=AuthRequest.model_validate(await object_body(request))
            token=data.token or data.password or ''
            allowed=False
            if data.action=='publish':
                allowed=not app.stop.is_set() and ((data.path=='program' and hmac.compare_digest(token,app.cfg.program_token)) or app.leases.allow_publish(data.path,token))
            elif data.action=='read':
                allowed=data.ip in ('127.0.0.1','::1') and (data.path=='program' or bool(re.fullmatch(r'camera/[a-f0-9]{32}',data.path or '')))
            return send(200 if allowed else 401,{})
        except (ValueError,TypeError):return send(401,{})
    @api.post('/recording')
    async def recording(request:Request):
        if request.client.host not in ('127.0.0.1','::1'):return send(403,{})
        data=await object_body(request)
        return send(200,await run_in_threadpool(app.recording_event,data))
    errors(api)
    return api


def web_api(app, *, manage_lifecycle=True):
    @asynccontextmanager
    async def lifespan(api):
        try:
            if manage_lifecycle:await run_in_threadpool(app.start,start_web=False)
            yield
        finally:
            if manage_lifecycle:await run_in_threadpool(app.close)
    api=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    errors(api)

    @api.get('/healthz')
    def health():
        alive=app.gateway_process and app.gateway_process.poll() is None
        return send(200 if alive and not app.program.error else 503,{'gateway':bool(alive),'program':app.program.status()})

    @api.get('/api/actions/{action_id}')
    def action(action_id:str):
        with app.control.lock:
            record=app.control.actions.get(action_id)
            return send(200 if record else 404,record or {'error':'Action not found in this run'})

    @api.post('/api/search')
    async def search(request:Request):
        from foundation_records import PublicSearchRequest
        from pydantic import ValidationError
        try:value=PublicSearchRequest.model_validate(await object_body(request))
        except (ValidationError,ValueError):return send(422,{'reason':'invalid_request','error':'Invalid search fields or values'})
        return send(200,await run_in_threadpool(app.replay_work.search,value))

    @api.get('/api/search/{query_id}')
    def search_result(query_id:str):
        with app.foundation.lock:
            row=app.foundation.db.execute('SELECT result FROM operations WHERE key=?',
                ('search-'+app.control.run_id+'-'+query_id,)).fetchone()
        if not row:return send(404,{'reason':'missing_reference','error':'Search not found in this run'})
        stored=json.loads(row[0])
        return send(200,stored['result']) if stored['state']=='complete' else send(503,{'reason':'provider_unavailable','error':'Search did not complete'})

    for path,fn in {
        '/api/viewer':lambda:{'join_url':app.join_url()},'/api/status':app.status,
        '/api/graphics/catalog':app.program.graphics.manifest,'/api/replay-context':app.replay_context.summary,
    }.items():
        def endpoint(fn=fn):return send(200,fn())
        # Do not expose the callable as a FastAPI query parameter.
        endpoint.__signature__=__import__('inspect').Signature()
        api.add_api_route(path,endpoint,methods=['GET'])

    @api.get('/api/qr')
    def qr():
        output=io.BytesIO(); qr=qrcode.QRCode(box_size=6,border=4)
        qr.add_data(app.join_url());qr.make(fit=True);qr.make_image().save(output,format='PNG')
        return send(200,output.getvalue(),'image/png')

    @api.get('/api/preview/{target}')
    def preview(target:str):
        source=app.get_source(int(target)) if target.isdigit() else None
        if source:
            with source.lock:frame=source.frames[-1].data if source.frames else None
        else:frame=app.program.frame if target=='program' else None
        return send(200 if frame else 404,frame or b'','image/jpeg')

    @api.get('/api/graphics/thumbnail/{key}')
    def thumbnail(key:str):
        image=app.program.graphics.thumbnails.get(key)
        return send(200 if image else 404,image or b'','image/png')

    @api.get('/api/replay-media/{key}')
    def replay(key:str):
        r=app.replays.get(key);exists=r and r.path.is_file()
        return send(200 if exists else 404,app.replay_work.preview(r) if exists else b'','video/mp4')

    @api.get('/api/lease/{lease_id}')
    def lease(lease_id:str,request:Request):
        row=app.leases.authenticated(lease_id,request.headers.get('Authorization','').removeprefix('Bearer '))
        program=app.program.status()
        return send(200,{'state':row['state'],'slot':row['slot'],'on_air':program['actual']=='LIVE' and
            not program['graphics']['applied']['covers_camera'] and program['actual_target'].get('source_path')==row['path']})

    def post(path,data,token=''):
        if app.stop.is_set():raise PermissionError('This event has ended')
        if path=='/api/leases':
            if not isinstance(data.get('code'),str) or not hmac.compare_digest(data['code'],app.join_code) or time.monotonic()>=app.join_expires:
                raise PermissionError('Event join code is invalid or expired')
            return send(200,app.leases.reserve(data.get('client')))
        if path.startswith('/api/lease/') and path.endswith('/release'):
            lease_id=path.split('/')[3]
            if token:app.leases.authenticated(lease_id,token)
            app.release(lease_id);return send(200,{})
        if path=='/api/event/end':
            if data!={'confirm':'End broadcast','run_id':app.control.run_id}:raise ValueError('Confirm End broadcast for the current run: all cameras and viewers will disconnect')
            app.control.submit({'id':uuid.uuid4().hex,'op':'takeover','args':{}});app.stop.set();return send(200,{})
        if path=='/api/join/rotate':
            app.join_code=secrets.token_urlsafe(24);app.join_expires=time.monotonic()+4*3600;app.write_access()
            return send(200,{'join_url':app.join_url()})
        if path=='/api/actions':
            if data.get('op')=='prepare' and 'search_id' in data.get('args',{}):
                args=data['args']
                try:app.control._validate_args('prepare',args)
                except (ValueError,TypeError):return send(422,{'reason':'invalid_request','error':'Use one complete search hit reference'})
                expected=data.get('expected',{})
                if expected.get('run_id')!=app.control.run_id:
                    return send(409,{'reason':'wrong_run','error':'Preparation belongs to another run'})
                app.replay_work.selected_hit(args['search_id'],args['scene_id'],args['scene_revision'])
            data.setdefault('expected',{});return send(200,app.control.submit(data))
        if path=='/api/chat':
            value=ChatRequest.model_validate(data);return send(200,app.control.chat(value.id,value.text,value.run_id))
        if path=='/api/replays':
            data.setdefault('expected',{});return send(202,app.human_action(data,'prepare'))
        if path=='/api/replay-cancel':
            data.setdefault('expected',{});return send(200,app.human_action(data,'cancel'))
        if path=='/api/replay-validate':
            if data.get('schema_version')=='1.2':
                from replay_inputs import resolve_plan
                resolved=resolve_plan(app,data,'review-'+uuid.uuid4().hex,min(time.time()+5,data.get('expires_at',0)))
            else:resolved=app.replay_context.resolve(data)
            try:return send(200,{'plan':resolved.plan,'shots':resolved.shots,'plan_hash':resolved.plan_hash})
            finally:resolved.release()
        if path=='/api/graphics/preview':
            graphics=app.program.graphics;return send(200,graphics.preview(graphics.prepare(data,preview=True)),'image/png')
        if path=='/api/program':
            if data.get('action') not in ('live','audio','replay','holding','graphics'):raise ValueError('Legacy program route accepts only media operations')
            if 'revision' not in data:raise ValueError('Program revision is required')
            data.setdefault('expected',{});return send(200,app.human_action(data))
        if path=='/internal/context':
            return send(200,app.update_event_context(data))
        if path=='/api/setup':return send(200,app.setup_event(data))
        functions={'/api/replay-calibrations':app.replay_context.calibrate,'/api/replay-evidence':app.replay_context.register_evidence,
                   '/api/replay-window':app.replay_context.window,'/api/replay-select':app.replay_context.select_fixture}
        if path in functions:return send(200,functions[path](data))
        return send(404,{'error':'Not found'})

    paths=('/api/leases','/api/lease/{lease_id}/release','/api/event/end','/api/join/rotate','/api/actions',
        '/api/chat','/api/replays','/api/replay-cancel','/api/replay-validate','/api/graphics/preview','/api/program',
        '/api/replay-calibrations','/api/replay-evidence','/api/replay-window','/api/replay-select','/internal/context','/api/setup')
    async def post_endpoint(request:Request):
        if request.url.path.startswith('/internal/') and request.client.host not in ('127.0.0.1','::1'):raise PermissionError('Internal interface requires local access')
        data=await object_body(request)
        return await run_in_threadpool(post,request.url.path,data,request.headers.get('Authorization','').removeprefix('Bearer '))
    for path in paths:api.add_api_route(path,post_endpoint,methods=['POST'])

    @api.api_route('/media/{media_path:path}',methods=['POST','PATCH','DELETE','OPTIONS','GET'])
    async def media(media_path:str,request:Request):
        match=MEDIA_ROUTE.fullmatch('/'+media_path)
        if not match or request.method not in ('POST','PATCH','DELETE','OPTIONS'):return send(404,{'error':'Unknown media route'})
        source,protocol,session=match.groups()
        if protocol=='whep':
            if source!='program':raise PermissionError('Only the broadcast program is available to viewers')
            if session and request.method not in ('PATCH','DELETE'):return send(404,{'error':'Unknown media session route'})
        elif source=='program':raise PermissionError('Program publishing is internal to the controller')
        raw=await body(request)
        def proxy():
            upstream=f'http://127.0.0.1:{8889+app.cfg.offset}/{media_path}'
            headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','authorization','if-match')}
            req=urllib.request.Request(upstream,raw,headers,method=request.method)
            try:response=urllib.request.urlopen(req,timeout=10)
            except urllib.error.HTTPError as error:response=error
            with response:
                forwarded={k:v for k,v in response.headers.items() if k.lower() in ('location','etag','link','accept-patch','access-control-expose-headers')}
                for key in list(forwarded):
                    if key.lower()=='location':forwarded[key]='/media'+urllib.parse.urlsplit(urllib.parse.urljoin(upstream,forwarded[key])).path
                return send(response.status,response.read(),response.headers.get('Content-Type','application/sdp'),forwarded)
        return await run_in_threadpool(proxy)

    @api.get('/{path:path}')
    def static(path:str):
        name={'':'watch.html','broadcast':'watch.html','operator':'operator.html','join':'join.html','watch':'watch.html'}.get(path,path)
        file=(ROOT/'web'/name).resolve()
        if not file.is_relative_to((ROOT/'web').resolve()) or not file.is_file():return send(404,{'error':'Not found'})
        return send(200,file.read_bytes(),mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
    return api
