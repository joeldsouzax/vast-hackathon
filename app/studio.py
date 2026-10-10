#!/usr/bin/env python3
"""Headless, single-event media experiment. No model or cloud integration."""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
import fcntl
import hashlib
import hmac
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import ipaddress
import sqlite3
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import qrcode
from media import Program, Source, compile_single_camera, stop_process
from replay import ReplayContext, render_plan
from control import Coordinator
from foundation import Foundation
from foundation_records import FoundationSettings, SourceEpoch
from direction import Direction
from replay_work import ReplayWork
from http_api import web_api, auth_api
from example_playback import ExamplePlayback
from video_analysis import VideoAnalysis
import uvicorn

ROOT = Path(__file__).resolve().parent
ID = re.compile(r"^[a-f0-9]{32}$")
MEDIA_ROUTE = re.compile(r"^/(program|camera/[a-f0-9]{32})/(whip|whep)(/[a-f0-9-]+)?$")


def loopback_host(host):
    host=host.lower().rstrip('.')
    if host=='localhost' or host.endswith('.localhost'):return True
    try:return ipaddress.ip_address(host).is_loopback
    except ValueError:return False


def public_origin(value):
    if not isinstance(value,str) or not value or any(c.isspace() or ord(c)<32 for c in value):
        raise ValueError('Set BREADCAST_PUBLIC_URL to the browser address, such as http://your-server:8080')
    value=value.rstrip('/')
    parsed=urllib.parse.urlsplit(value)
    if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or
            parsed.username is not None or parsed.password is not None):
        raise ValueError('Public URL must be an HTTP(S) origin without credentials or a path')
    if parsed.port is not None and parsed.port<1:raise ValueError('Public URL port must be 1–65535')
    return value


def request_json(url, data=None, method=None, token=None, timeout=5, context=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, json.dumps(data).encode() if data is not None else None,
                                     headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        body = response.read()
        return json.loads(body) if body else {}


@dataclass
class Config:
    runtime: Path
    public_url: str
    bind: str = "127.0.0.1"
    port: int = 8080
    offset: int = 0
    width: int = 640
    height: int = 360
    fps: int = 15
    delay: float = 3.0
    buffer_seconds: float = 120
    buffer_bytes: int = 16 * 1024 * 1024
    reservation_seconds: float = 60
    reconnect_seconds: float = 20
    program_token: str = ""
    certificate: str | None = None
    private_key: str | None = None
    ice_hosts: tuple = ()
    ice_servers: tuple = ()
    print_access: bool = True
    foundation_config: Path | None = None
    program_proof: Path | None = None
    webrtc_port: int | None = None
    public_path_prefix: str = ""
    operator_auth: str = "local"
    operator_token_file: Path | None = None
    operator_token: str = field(default="", init=False, repr=False)
    server_videos_config: Path = ROOT.parent / 'config/server-videos.json'
    crew_mode: str = 'automatic'
    viewer_transport: str = 'webrtc'

    def __post_init__(self):
        if self.viewer_transport not in ('webrtc', 'hls'):
            raise ValueError('BREADCAST_VIEWER_TRANSPORT must be webrtc or hls')
        if self.crew_mode not in ('automatic', 'human'):
            raise ValueError('BREADCAST_CREW_MODE must be automatic or human')
        self.public_url=public_origin(self.public_url)
        if self.public_path_prefix not in ('', '/app'):
            raise ValueError('BREADCAST_PUBLIC_PATH_PREFIX must be empty or /app')
        if self.operator_auth not in ('local', 'proxy', 'token'):
            raise ValueError('BREADCAST_OPERATOR_AUTH must be local, proxy, or token')
        if self.operator_auth == 'token':
            if not self.operator_token_file or not Path(self.operator_token_file).is_absolute():
                raise ValueError('Token access requires an absolute BREADCAST_OPERATOR_TOKEN_FILE path')
            try:
                with Path(self.operator_token_file).open('rb') as secret:
                    raw = secret.read(4097)
                token = raw.decode('ascii').strip()
            except (OSError, UnicodeError):
                raise ValueError('Operator token file is unreadable or invalid') from None
            if len(raw) > 4096 or len(token) < 32 or not re.fullmatch(r'[A-Za-z0-9_-]+', token):
                raise ValueError('Operator token must contain 32–4096 URL-safe characters')
            self.operator_token = token
        host=urllib.parse.urlsplit(self.public_url).hostname
        self.ice_hosts=self.ice_hosts or (host,)
        for candidate in self.ice_hosts:
            if not candidate or any(c.isspace() for c in candidate) or '/' in candidate or '@' in candidate:
                raise ValueError('BREADCAST_ICE_HOSTS must contain host names or IP addresses, not URLs')
        if not loopback_host(host) and all(loopback_host(h) for h in self.ice_hosts):
            raise ValueError('Remote media cannot use loopback ICE hosts; clear BREADCAST_ICE_HOSTS or set the reachable server host')
        if self.webrtc_port is None:self.webrtc_port=8189+self.offset
        if not 1024<=self.webrtc_port<=65535:raise ValueError('WebRTC port must be 1024–65535')

    @property
    def audio_size(self):
        return 48000 // self.fps * 2

    def public_path(self, path):
        return self.public_path_prefix + path

    def rtsp_url(self, path, token=None):
        auth = f"publisher:{token}@" if token else ""
        return f"rtsp://{auth}127.0.0.1:{8554 + self.offset}/{path}"

    def gateway(self, route, data=None, method=None):
        return request_json(f"http://127.0.0.1:{9997 + self.offset}/v3/{route}", data, method)


class Leases:
    """Transactions reserve five slots; removing a gateway path fences its publisher."""
    def __init__(self, cfg: Config, gateway=None):
        self.cfg = cfg
        self.gateway = gateway or cfg.gateway
        self.lock = threading.RLock()
        self.master = secrets.token_bytes(32)
        self.db = sqlite3.connect(cfg.runtime / "leases.sqlite", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS leases (
            id TEXT PRIMARY KEY, slot INTEGER UNIQUE NOT NULL CHECK (slot BETWEEN 1 AND 5),
            client TEXT UNIQUE NOT NULL, path TEXT UNIQUE NOT NULL, state TEXT NOT NULL,
            reserved_until REAL NOT NULL, last_media REAL, disconnected_at REAL,
            publisher_id TEXT, inbound_bytes INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0
        )""")
        # Every start launches a fresh gateway with no camera paths. Old leases cannot publish.
        self.db.execute("DELETE FROM leases")
        os.chmod(cfg.runtime / "leases.sqlite", 0o600)

    def token(self, row):
        return hmac.new(self.master, row["id"].encode(), hashlib.sha256).hexdigest()

    def public(self, row):
        return {"lease_id": row["id"], "slot": row["slot"], "source_path": row["path"],
                "state": row["state"], "epoch": row["epoch"], "token": self.token(row),
                "reservation_remaining_s": max(0, row["reserved_until"] - time.monotonic())}

    def reserve(self, client: str):
        if not isinstance(client, str) or not ID.fullmatch(client):
            raise ValueError("A 32-character camera session ID is required")
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT * FROM leases WHERE client=?", (client,)).fetchone()
                if row:
                    if row["state"] == "REVOKING":
                        raise ValueError("Previous camera session is closing; retry shortly")
                    self.db.execute("COMMIT")
                    return self.public(row)
                used = {row[0] for row in self.db.execute("SELECT slot FROM leases")}
                slot = next((s for s in range(1, 6) if s not in used), None)
                if slot is None:
                    raise ValueError("All five camera slots are occupied")
                lease_id = uuid.uuid4().hex
                path = f"camera/{lease_id}"
                self.db.execute("INSERT INTO leases (id,slot,client,path,state,reserved_until) VALUES (?,?,?,?,?,?)",
                                (lease_id, slot, client, path, "RESERVED",
                                 time.monotonic() + self.cfg.reservation_seconds))
                # Preserve capacity before the external operation. A lost response
                # can leave a real gateway path that must be fenced before reuse.
                self.db.execute("COMMIT")
                # Gateway API authorization is local and does not call back into this lock.
                try:
                    self.gateway(f"config/paths/add/{path}", {}, "POST")
                except BaseException:
                    self.db.execute("UPDATE leases SET state='REVOKING' WHERE id=?", (lease_id,))
                    raise
                return self.public(self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone())
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def rows(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM leases ORDER BY slot")]

    def authenticated(self, lease_id: str, token: str):
        with self.lock:
            row = self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
            if not row or not hmac.compare_digest(self.token(row), token):
                raise PermissionError("Camera session is invalid or expired")
            return dict(row)

    def allow_publish(self, path: str, token: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT * FROM leases WHERE path=?", (path,)).fetchone()
            if not row or row["state"] == "REVOKING" or not hmac.compare_digest(self.token(row), token):
                return False
            now = time.monotonic()
            if row["state"] == "RESERVED":
                return now < row["reserved_until"]
            return row["disconnected_at"] is None or now - row["disconnected_at"] < self.cfg.reconnect_seconds

    def update_media(self, lease_id: str, path_info: dict | None):
        with self.lock:
            row = self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
            if not row or row["state"] == "REVOKING":
                return
            now = time.monotonic()
            if ((row['state'] == 'RESERVED' and now >= row['reserved_until']) or
                (row['state'] == 'RECONNECTING' and row['disconnected_at'] is not None and
                    now - row['disconnected_at'] >= self.cfg.reconnect_seconds)):
                # Polling is not proof that this publisher was admitted in time.
                self.db.execute("UPDATE leases SET state='REVOKING' WHERE id=?", (lease_id,))
                return
            source = (path_info or {}).get("source") or {}
            incoming = (path_info or {}).get("inboundBytes", 0)
            online = (path_info or {}).get("online", False)
            if online and incoming > 0 and (source.get("id") != row["publisher_id"] or incoming > row["inbound_bytes"]):
                new_epoch = row["epoch"] + int(source.get("id") != row["publisher_id"])
                self.db.execute("""UPDATE leases SET state='ACTIVE', last_media=?, disconnected_at=NULL,
                                   publisher_id=?, inbound_bytes=?, epoch=? WHERE id=?""",
                                (now, source.get("id"), incoming, new_epoch, lease_id))
            elif row["state"] in ("ACTIVE", "RECONNECTING") and (
                    not online or row["last_media"] is None or now - row["last_media"] > 3):
                self.db.execute("""UPDATE leases SET state='RECONNECTING',
                                   disconnected_at=COALESCE(disconnected_at, ?) WHERE id=?""", (now, lease_id))

    def expired(self):
        now = time.monotonic()
        return [r["id"] for r in self.rows() if r["state"] == "REVOKING" or
                (r["state"] == "RESERVED" and now >= r["reserved_until"]) or
                (r["state"] == "RECONNECTING" and r["disconnected_at"] is not None and
                 now - r["disconnected_at"] >= self.cfg.reconnect_seconds)]

    def release(self, lease_id: str):
        with self.lock:
            row = self.db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
            if not row:
                return
            self.db.execute("UPDATE leases SET state='REVOKING' WHERE id=?", (lease_id,))
            try:
                self.gateway(f"config/paths/remove/{row['path']}", method="DELETE")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
            # Never free capacity while an old path still accepts publishers.
            self.db.execute("DELETE FROM leases WHERE id=?", (lease_id,))


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop = threading.Event()
        self.join_code = secrets.token_urlsafe(24)
        self.join_expires = time.monotonic() + 4 * 3600
        self.cfg.program_token = secrets.token_urlsafe(32)
        self.leases = Leases(cfg)
        self.sources = {}
        self.sources_lock = threading.RLock()
        self.render_lock = threading.RLock()
        self.jobs = {}
        self.replays = {}
        self.render_thread = None
        self.rendering = False
        self.replay_context = ReplayContext(cfg, self.get_source)
        self.render_cancel = threading.Event()
        self.log_lock = threading.Lock()
        self.program = Program(cfg, self.get_source, self.log)
        self.control = Coordinator(self)
        self.foundation = Foundation(cfg.runtime, self.control.run_id,
            FoundationSettings.load(cfg.foundation_config) if cfg.foundation_config else __import__('workshop_config').settings(),
            snapshot=self.foundation_snapshot)
        self.program.graphics.activate_package(self.program.graphics.prepare_package(self.foundation.event_context()))
        self.direction = Direction(self)
        self.replay_work = ReplayWork(self)
        self.program.replay_guard=self.replay_work.ticket_valid
        self.program.event_mapping_reader=lambda source_id:self.foundation.current_mappings.get(source_id)
        self.gateway_process = None
        self.servers = []
        self.monitor_thread = None
        self.gateway_error = None
        self.recording_thread = None
        self.closed = False
        self.examples = ExamplePlayback(self)
        self.video_analysis = VideoAnalysis(self)

    def foundation_snapshot(self):
        # Never take the controller lock from the ledger: human writes use the reverse order.
        # A changed control revision makes the sampled runtime unavailable rather than mixing states.
        sampled_utc=time.time()
        revision=self.control.revision
        paused=self.control.crew_paused
        policy=copy.deepcopy(self.control.policy)
        health=[]
        for row in self.leases.rows():
            source=self.get_source(row['slot'])
            state=source.status() if source and source.path==row['path'] and source.epoch==row['epoch'] else {}
            if state.get('epoch',row['epoch'])!=row['epoch']:state={}
            health.append({'source_id':row['path'],'slot':row['slot'],'epoch':row['epoch'],'state':row['state'],
                **{key:state.get(key) for key in ('last_frame_age_s','buffer_seconds','buffer_ready','has_audio')}})
        with self.program.lock:
            program=self.program.status()
            runtime={'sampled_utc':sampled_utc,'program':copy.deepcopy({key:program[key] for key in
                ('requested','actual','actual_target','applied_revision','primary_slot','primary_source_path',
                 'audio_slot','audio_source_path','audio_muted','replay_id')}),
                'source_health':health,'crew_paused':paused,'policy':policy}
        with self.sources_lock:current_sources=tuple(self.sources.values())
        return {'program_revision':program['revision'],'control_revision':revision,
                'runtime':runtime if self.control.revision==revision else None,
                'mapping_revisions':{key:value.revision for key,value in self.foundation.current_mappings.items()},
                'decoder_revisions':{source.path:getattr(source,'timeline_revision',1) for source in current_sources}}

    def log(self, kind, **fields):
        record = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "monotonic_s": time.monotonic(), "kind": kind, **fields}
        with self.log_lock:
            path=self.cfg.runtime / "program-history.jsonl"
            if path.exists() and path.stat().st_size>16*1024*1024:
                for index in (2,1):
                    old=path.with_suffix(f".jsonl.{index}")
                    if old.exists(): old.replace(path.with_suffix(f".jsonl.{index+1}"))
                path.replace(path.with_suffix(".jsonl.1"))
            with path.open("a") as output:output.write(json.dumps(record) + "\n")

    def get_source(self, slot):
        with self.sources_lock:
            return self.sources.get(slot)

    def media_config(self):
        cfg = self.cfg
        # JSON is valid YAML. Exact per-lease paths are added through the local API.
        return {"logLevel": "warn", "logStructured": True,
                "authMethod": "http", "authHTTPAddress": f"http://127.0.0.1:{8090 + cfg.offset}/auth",
                "authHTTPExclude": [{"action": "api"}],
                "api": True, "apiAddress": f"127.0.0.1:{9997 + cfg.offset}",
                "rtsp": True, "rtspAddress": f"127.0.0.1:{8554 + cfg.offset}", "rtspTransports": ["tcp"],
                "rtmp": False, "srt": False, "moq": False,
                "hls": cfg.viewer_transport == 'hls', "hlsAddress": f"127.0.0.1:{8888 + cfg.offset}",
                "hlsVariant": "fmp4", "hlsSegmentDuration": "1s", "hlsSegmentMaxSize": "8M",
                # The public proxy exposes only program assets; this credential stays local.
                "hlsCDNSecret": cfg.program_token,
                "webrtc": True, "webrtcAddress": f"127.0.0.1:{8889 + cfg.offset}",
                "webrtcLocalUDPAddress": f"{os.environ.get('BREADCAST_WEBRTC_UDP_BIND','')}:{cfg.webrtc_port}",
                "webrtcLocalTCPAddress": f":{cfg.webrtc_port}",
                "webrtcAdditionalHosts": list(cfg.ice_hosts), "webrtcICEServers2": list(cfg.ice_servers),
                "pathDefaults": {"overridePublisher": False, "record": True, "recordFormat": "fmp4",
                                 "recordPath": str(cfg.runtime / "recordings" / "%path" / "%Y-%m-%d_%H-%M-%S-%f"),
                                 "recordPartDuration": "500ms", "recordSegmentDuration": "2s",
                                 "recordDeleteAfter": "0s",
                                 "runOnRecordSegmentCreate": f"python3 {ROOT / 'recording_hook.py'} create --port {8090 + cfg.offset}",
                                 "runOnRecordSegmentComplete": f"python3 {ROOT / 'recording_hook.py'} complete --port {8090 + cfg.offset}"},
                "paths": {"program": {}}}

    def start(self, *, start_web=True):
        cfg = self.cfg
        if self.servers or self.program.thread.is_alive():
            raise RuntimeError("Studio runtime already started")
        auth = uvicorn.Server(uvicorn.Config(auth_api(self), host="127.0.0.1", port=8090+cfg.offset,
            access_log=False, log_level="warning", workers=1, proxy_headers=False))
        self.servers.append(auth)
        thread = threading.Thread(target=auth.run, daemon=True)
        thread.start()
        deadline = time.monotonic()+10
        while not auth.started:
            if not thread.is_alive() or time.monotonic()>deadline: raise RuntimeError("Internal HTTP server failed")
            time.sleep(.02)
        config_path = cfg.runtime / "mediamtx.json"
        config_path.write_text(json.dumps(self.media_config(), indent=2) + "\n")
        log = open(cfg.runtime / "mediamtx.log", "ab")
        try:
            self.gateway_process = subprocess.Popen(["mediamtx", str(config_path)], stderr=log, stdout=log)
        finally:
            log.close()
        deadline = time.monotonic() + 15
        while True:
            try:
                cfg.gateway("paths/list")
                break
            except (OSError, urllib.error.URLError):
                if self.gateway_process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(f"MediaMTX did not start. Read {cfg.runtime / 'mediamtx.log'}")
                time.sleep(0.1)
        if start_web:
            web=uvicorn.Server(uvicorn.Config(web_api(self,manage_lifecycle=False),host=cfg.bind,port=cfg.port,
                workers=1,reload=False,access_log=False,log_level="warning",proxy_headers=False,
                ssl_certfile=cfg.certificate,ssl_keyfile=cfg.private_key))
            self.servers.append(web)
            thread=threading.Thread(target=web.run,daemon=True);thread.start()
            deadline=time.monotonic()+10
            while not web.started:
                if not thread.is_alive() or time.monotonic()>deadline:raise RuntimeError("Studio HTTP server failed")
                time.sleep(.02)
        self.program.start()
        self.control.start()
        self.foundation.start()
        self.direction.start()
        self.replay_work.start()
        self.recording_thread = threading.Thread(target=self.recordings, daemon=True)
        self.recording_thread.start()
        self.monitor_thread = threading.Thread(target=self.monitor, daemon=True)
        self.monitor_thread.start()
        versions = {name: subprocess.check_output(command, text=True).splitlines()[0]
                    for name, command in {"mediamtx": ["mediamtx", "--version"],
                                          "ffmpeg": ["ffmpeg", "-version"],
                                          "python": ["python3", "--version"]}.items()}
        versions["package_source"] = os.environ.get("BREADCAST_PACKAGE_SOURCE", "local")
        (cfg.runtime / "versions.json").write_text(json.dumps(versions, indent=2) + "\n")
        access = self.write_access()
        access_path = cfg.runtime / "access.json"
        self.log("experiment_start", delay_s=cfg.delay, providers="not connected")
        if cfg.print_access:
            print(f"Operator:  {access['operator_url']}\nBroadcast: {access['broadcast_url']}\n"
                  f"Join QR:   open Broadcast\nRuntime:   {cfg.runtime}", flush=True)
        else:
            print(f"Studio started. Access links are in {access_path}", flush=True)

    def join_url(self):
        return f"{self.cfg.public_url}{self.cfg.public_path('/join')}?code={urllib.parse.quote(self.join_code)}"

    def write_access(self):
        cfg = self.cfg
        access = {"public_url": cfg.public_url, "public_path_prefix": cfg.public_path_prefix,
                  "operator_url": cfg.public_url + cfg.public_path("/operator"),
                  "broadcast_url": cfg.public_url + cfg.public_path("/"), "join_url": self.join_url(), "join_code": self.join_code,
                  "rtsp_port": 8554 + cfg.offset, "width": cfg.width, "height": cfg.height, "fps": cfg.fps}
        temporary = cfg.runtime / "access.new"
        temporary.write_text(json.dumps(access, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(cfg.runtime / "access.json")
        return access

    def monitor(self):
        while not self.stop.wait(0.5):
            try:
                paths = {p["name"]: p for p in self.cfg.gateway("paths/list")["items"]}
                self.gateway_error = None
                for row in self.leases.rows():
                    info = paths.get(row["path"])
                    self.leases.update_media(row["id"], info)
                    if info and info.get("online") and row["state"] != "REVOKING":
                        current = next((r for r in self.leases.rows() if r["id"] == row["id"]), None)
                        if not current or current["state"] != "ACTIVE":
                            continue
                        source = self.get_source(row["slot"])
                        if source is None or source.epoch != current["epoch"]:
                            if source:
                                source.close()
                            tracks = [t.get("codec", "") for t in info.get("tracks2", [])]
                            audio = any(t.lower() in ("opus", "mpeg4audio", "g711", "lpcm", "aac") for t in tracks)
                            source = Source(self.cfg, row["path"], row["slot"], current["epoch"], audio, on_discontinuity=self.source_discontinuity)
                            with self.program.lock, self.sources_lock:
                                self.sources[row["slot"]] = source
                            self.log("source_epoch", slot=row["slot"], epoch=source.epoch)
                        try:self.register_live_source(source)
                        except ValueError as error:self.log('live_source_registration_failed',source_path=source.path,reason=str(error))
                for lease_id in self.leases.expired():
                    self.release(lease_id)
                self.control.start_ready_camera()
            except (OSError, urllib.error.URLError, KeyError) as error:
                self.gateway_error = str(error)
                if self.gateway_process.poll() is not None:
                    self.program.error = "MediaMTX exited; restart the experiment"
                    self.stop.set()

    def register_live_source(self, source):
        # A decoded camera exists before its first recording is finalized. A
        # clock reset must not leave event commentary waiting on archive work.
        from foundation_records import SourceEpoch
        with source.lock:
            frame=source.frames[-1] if source.frames else None
            native=frame.native_provenance if frame else None
            if not native or frame.source_epoch!=source.epoch:return
            identity=SourceEpoch(event_id=self.foundation.settings.event.event_id,run_id=self.control.run_id,
                source_id=source.path,slot=source.slot,epoch=source.epoch,time_base=native['native_time_base'])
        self.foundation.register_source(identity)

    def source_discontinuity(self, path, epoch, *, mapping_only=False):
        if mapping_only:
            self.log("native_mapping_invalidated",source_path=path,epoch=epoch)
            return
        with self.leases.lock, self.leases.db:
            self.leases.db.execute("UPDATE leases SET epoch=epoch+1 WHERE path=? AND epoch=?",(path,epoch))
        from recording_hook import atomic_json
        atomic_json(self.cfg.runtime / ("discontinuity-"+path.rsplit('/',1)[-1]+"-"+str(epoch)),{"utc":time.time()})
        self.log("source_discontinuity",source_path=path,old_epoch=epoch)

    def release(self, lease_id):
        row = next((r for r in self.leases.rows() if r["id"] == lease_id), None)
        self.leases.release(lease_id)
        if row:
            with self.program.lock, self.sources_lock:
                source = self.sources.get(row["slot"])
                if source and source.path == row["path"]:
                    self.sources.pop(row["slot"])
                else:
                    source = None
            if source:
                source.close()
            self.log("lease_released", slot=row["slot"])

    def status(self):
        cameras = []
        for row in self.leases.rows():
            source = self.get_source(row["slot"])
            cameras.append({"lease_id": row["id"], "slot": row["slot"], "state": row["state"],
                            "source_path": row["path"], "media_age_s": round(time.monotonic() - row["last_media"], 2)
                            if row["last_media"] else None, **(source.status() if source else {"capture_sync": "unknown"})})
        with self.render_lock:
            replays = [{"id": r.id, "slot": r.slot, "duration_s": r.duration,
                        "speed": r.speed, "epoch": r.epoch, "validation": r.report,
                        "preview_url": f"/api/replay-media/{r.id}"}
                       for r in self.replays.values()]
            jobs = list(self.jobs.values())
        for replay in replays:
            try:
                with self.replay_context.lock:
                    self.control.replay_reason(self.replays[replay['id']])
                replay.update(eligible=True, reason='')
            except (ValueError, KeyError) as error:
                replay.update(eligible=False, reason=str(error))
        return {"cameras": cameras, "occupied": len(cameras), "capacity": 5, "examples": self.examples.status(),
                "video_analysis":self.video_analysis.status(),
                "program": self.program.status(), "delay_s": self.cfg.delay,
                "join_url": self.join_url(), "join_remaining_s": max(0, self.join_expires - time.monotonic()),
                "replays": replays, "jobs": jobs, "gateway_error": self.gateway_error,
                "providers": self.foundation.registry.capabilities(), "scope": "single-event media experiment",
                "control": self.control.snapshot(), "foundation": self.foundation.diagnostics(),
                "direction": self.direction.status(), "replay_work":self.replay_work.status(), "event_context":self.foundation.event_context().model_dump(mode='json')}

    def update_event_context(self, data):
        if set(data) != {"context", "expected_revision", "operation_key"}:
            raise ValueError("Context edit requires context, expected_revision, and operation_key")
        with self.control.lock:
            prior=self.foundation.context_revision
            result = self.foundation.update_context(data["context"], data["expected_revision"], data["operation_key"])
            if self.foundation.context_revision!=prior:
                self.control._invalidate("Event context changed")
                with self.program.lock:
                    self.program.graphics.active.clear();self.program.graphics.retiring.clear()
            return result

    def setup_event(self, data):
        from foundation_records import EventContext
        if set(data)!={'context','expected_revision','operation_key'}:raise ValueError('Setup requires context, expected_revision, and operation_key')
        event=EventContext.model_validate(data['context'])
        if event.broadcast_delay_s is None:
            event=event.model_copy(update={'broadcast_delay_s':self.cfg.delay})
            data={**data,'context':event.model_dump(mode='json')}
        if event.broadcast_delay_s is not None and event.broadcast_delay_s!=self.cfg.delay:
            raise ValueError('Delay changes require a stopped Studio and a new --delay configuration')
        package=self.program.graphics.prepare_package(event)
        result=self.update_event_context(data)
        with self.control.lock,self.program.lock:
            if self.foundation.context_revision!=event.revision:raise ValueError('Context changed while package activation was pending')
            self.program.graphics.activate_package(package)
        (self.cfg.runtime/'event-graphics-package.json').write_text(json.dumps(package['manifest'],indent=2)+'\n')
        return {'context':result,'package':package['manifest']}

    def recording_event(self, data):
        if set(data) != {"stage", "path", "source_path", "utc"} or data["stage"] != "create":
            raise ValueError("Invalid recording ownership event")
        path = Path(data["path"]).resolve()
        if not path.is_relative_to((self.cfg.runtime / "recordings").resolve()):
            raise PermissionError("Recording path is outside runtime")
        row = next((r for r in self.leases.rows() if r["path"] == data["source_path"]), None)
        if row is None: raise ValueError("Recording has no source lease")
        # Creation can precede the monitor. Query actual gateway state once.
        self.leases.update_media(row["id"], self.cfg.gateway("paths/get/"+urllib.parse.quote(row["path"], safe="")))
        row = next(r for r in self.leases.rows() if r["id"] == row["id"])
        owner={"event_id": self.foundation.settings.event.event_id, "run_id": self.control.run_id,
                "source_id": row["path"], "epoch": max(1,row["epoch"]), "slot": row["slot"],
                "created_utc": data["utc"], "provenance": "sample" if (self.cfg.runtime / ("sample-"+row["id"])).exists() else "camera"}
        video=self.examples.asset_for(row['path'])
        if video:
            owner.update(provenance='server_video',video_id=video['id'],original_sha256=video['sha256'])
        from foundation import operation_key
        with self.foundation.transaction():
            recordings=[json.loads(r['body']) for r in self.foundation._records('recording',run=self.control.run_id)]
            prior=[r for r in recordings if r['source_id']==owner['source_id'] and r['epoch']==owner['epoch']]
            owner['sequence']=max((r['sequence'] for r in prior),default=-1)+1
            owner['recording_id']=operation_key(str(path))
            self.foundation._put('recording',owner['recording_id'],1,owner)
        return owner

    def recording_offset(self, owner, duration_s, time_base, video_duration_pts):
        from fractions import Fraction
        with self.foundation.transaction():
            rows=self.foundation._records('recording',run=owner['run_id'])
            prior=[json.loads(r['body']) for r in rows if json.loads(r['body'])['source_id']==owner['source_id'] and
                   json.loads(r['body'])['epoch']==owner['epoch'] and json.loads(r['body'])['sequence']<owner['sequence']]
            if len(prior)!=owner['sequence'] or any('duration_s' not in r for r in prior):
                raise ValueError("Previous recording closure is missing")
            if any(r.get('time_base')!=time_base for r in prior):raise ValueError('Recording time base changed')
            offset=sum(r['video_duration_pts'] for r in prior)
            current=next((r for r in rows if r['id']==owner['recording_id']),None)
            if current and 'duration_s' not in json.loads(current['body']):
                self.foundation.db.execute("UPDATE records SET active=0 WHERE kind='recording' AND id=?",(owner['recording_id'],))
                self.foundation._put('recording',owner['recording_id'],current['revision']+1,{**owner,'duration_s':duration_s,'video_duration_pts':video_duration_pts,'time_base':time_base,'timeline_offset_pts':offset},run=owner['run_id'])
            return offset

    def recordings(self):
        from foundation_storage import inspect_video
        from foundation import operation_key
        while not self.stop.wait(.25):
            try:
                root = self.cfg.runtime / "recordings"
                enabled = self.foundation.registry.capabilities()['storage']['ready']
                pending = []
                for notification in sorted(root.rglob("*.complete")):
                    if notification.with_suffix(".owner").is_file():pending.append(notification)
                    elif time.time()-notification.stat().st_mtime>60:
                        # Orphans from earlier runs must not occupy the bounded pending window.
                        notification.unlink(missing_ok=True);notification.with_suffix(".mp4").unlink(missing_ok=True)
                for notification in pending[:self.foundation.settings.limits.pending_chunks]:
                    if self.stop.is_set():return
                    try:
                        path = notification.with_suffix(".mp4")
                        owner_path = notification.with_suffix(".owner")
                        if not owner_path.is_file(): continue
                        owner = json.loads(owner_path.read_text())
                        if enabled:
                            info = inspect_video(path)
                            if self.stop.is_set():return
                            completion=json.loads(notification.read_text())
                            offset=self.recording_offset(owner,completion["duration_s"],info["time_base"],info["end"]-info["start"])
                            boundary=self.cfg.runtime / ("discontinuity-"+owner["source_id"].rsplit('/',1)[-1]+"-"+str(owner["epoch"]))
                            if boundary.exists() and json.loads(notification.read_text())["utc"]>=json.loads(boundary.read_text())["utc"]:
                                raise ValueError("Recording crosses an unresolved source discontinuity")
                            source = SourceEpoch(**{k:owner[k] for k in ("event_id","run_id","source_id","epoch","slot")},time_base=info["time_base"])
                            measured=None
                            if self.foundation.settings.direction.enabled:
                                from live_timing import match_recording
                                decoder=self.get_source(source.slot)
                                try:
                                    if not decoder or decoder.path!=source.source_id or decoder.epoch!=source.epoch:
                                        raise ValueError('Recording decoder ownership changed')
                                    measured=match_recording(path,decoder,self.cfg.fps)
                                    offset=measured['timeline_offset_pts']
                                    self.log('live_recording_mapping',source_path=source.source_id,epoch=source.epoch,**measured)
                                except ValueError as error:
                                    self.log('live_recording_mapping_unknown',source_path=source.source_id,reason=str(error))
                            # Unknown/ambiguous matches retain notification-only archive behavior.
                            if self.stop.is_set():return
                            manifest = self.foundation.finalize(path, source, owner["sequence"],
                                measured['last_receipt_utc'] if measured else None, closed=True, provenance=owner["provenance"],
                                timeline_offset_pts=offset,notification_utc=owner["created_utc"],
                                receipt_uncertainty_ms=measured['uncertainty_ms'] if measured else 0.0,
                                geometry=__import__('foundation_records').Geometry.model_validate(measured['geometry']) if measured else None)
                            if self.stop.is_set():return
                            if source.run_id == self.control.run_id and self.foundation.registry.capabilities()['jobs']['ready']:
                                self.foundation.enqueue(self.foundation.window(manifest))
                            path.unlink(missing_ok=True); owner_path.unlink(missing_ok=True); notification.unlink(missing_ok=True)
                        elif time.time()-path.stat().st_mtime > 300:
                            path.unlink(missing_ok=True); owner_path.unlink(missing_ok=True); notification.unlink(missing_ok=True)
                    except (OSError, ValueError, KeyError, StopIteration) as error:
                        if self.stop.is_set():return
                        reason="Recording finalization failed: "+type(error).__name__
                        self.foundation.last_failure=reason
                        if owner_path.is_file():
                            failed_owner=json.loads(owner_path.read_text())
                            with self.foundation.transaction():
                                key='gap-'+failed_owner['recording_id']
                                if not self.foundation.db.execute("SELECT 1 FROM records WHERE kind='gap' AND id=?",(key,)).fetchone():
                                    self.foundation._put('gap',key,1,{'event_id':failed_owner['event_id'],'source_id':failed_owner['source_id'],
                                        'epoch':failed_owner['epoch'],'native':None,'sequence':failed_owner['sequence'],'reason':reason},run=failed_owner['run_id'])
                        # Quarantine one failure so other sources can progress. Never publish it as ready.
                        notification.rename(notification.with_suffix(".failed"))
                if self.stop.is_set():return
                self.foundation.cleanup()
                limits=self.foundation.settings.limits
                backlog=sum(p.stat().st_size for p in root.rglob("*.mp4"))
                retained=self.foundation.diagnostics()['storage_bytes']
                if len(pending)>limits.pending_chunks or backlog>limits.pending_bytes or backlog+retained>limits.archive_bytes or self.foundation.storage.free_bytes()<limits.disk_reserve_bytes:
                    self.foundation.last_failure="Recording storage pressure; archive recording paused"
                    for row in self.leases.rows():
                        self.cfg.gateway("config/paths/patch/"+row["path"], {"record":False}, "PATCH")
                for failed in root.rglob("*.failed"):
                    if time.time()-failed.stat().st_mtime>300:
                        failed.with_suffix(".mp4").unlink(missing_ok=True);failed.with_suffix(".owner").unlink(missing_ok=True);failed.unlink(missing_ok=True)
                # Partial/unowned files have no ready status. Bound abandoned recorder files after shutdown/restart.
                for path in root.rglob("*.mp4"):
                    if time.time()-path.stat().st_mtime > 300 and not path.with_suffix(".complete").exists():
                        path.unlink(missing_ok=True);path.with_suffix(".owner").unlink(missing_ok=True)
            except (OSError, ValueError, KeyError, StopIteration) as error:
                self.foundation.last_failure = f"Recording finalization failed; inspect source coverage ({type(error).__name__}: {str(error)[:160]})"

    def render(self, slot=1, seconds=4, speed=1, zoom=1, plan=None, source_ref=None, resolved=None, replay_id=None):
        if plan is not None and plan.get('schema_version')=='1.2' and resolved is None:
            from replay_inputs import resolve_plan
            replay_id=replay_id or uuid.uuid4().hex
            resolved=resolve_plan(self,plan,'render-'+replay_id,min(time.time()+self.foundation.settings.replay.recall_s,plan['expires_at']))
            plan=None
        canonical=resolved is not None and resolved.plan.get('schema_version')=='1.2'
        source = (source_ref or self.get_source(slot)) if plan is None else None
        if resolved is None and plan is None and source is None:
            raise ValueError("Camera has no decoded media")
        with self.replay_context.lock, self.render_lock:
            if self.rendering:
                raise ValueError("One replay is already rendering")
            if replay_id not in self.jobs and len(self.jobs) >= self.foundation.settings.replay.jobs:
                raise ValueError("This experiment allows 20 replay jobs per run; start a new run")
            # Validate and pin before the worker starts. Pinned immutable frame references
            # survive buffer eviction and reconnect; evidence freshness is checked again.
            replay_id = replay_id or uuid.uuid4().hex
            resolved = resolved or (self.replay_context.resolve(plan) if plan is not None else compile_single_camera(self.cfg, replay_id, source, seconds, speed, zoom))
            self.rendering = True
            self.render_cancel.clear()
            self.jobs.setdefault(replay_id,{"id":replay_id,"slot":slot,"stages":{}})
            self.jobs[replay_id].update(state='rendering',plan=resolved.plan)
            self.jobs[replay_id]['stages']['render_start_utc']=time.time()

        def worker():
            start = time.monotonic()
            try:
                replay = render_plan(self.cfg, replay_id, resolved, self.render_cancel)
                self.jobs[replay_id]['stages']['render_end_utc']=time.time()
                if canonical:self.replay_work.eligible(replay,files=True)
                self.jobs[replay_id]['stages']['output_validated_utc']=time.time()
                if self.foundation.registry.gemini:
                    import asyncio
                    receipt=asyncio.run(self.foundation.registry.gemini.upload_replay(replay,
                        min(time.time()+45,resolved.plan.get('expires_at',time.time()+45))))
                    self.jobs[replay_id]['supabase_clip']={'id':receipt['id'],'sha256':receipt['sha256'],
                        'bucket':receipt['bucket'],'object_path':receipt['object_path']}
                with self.replay_context.lock, self.render_lock:
                    if plan is not None:
                        self.replay_context.eligible(replay.report["plan"])
                    if self.render_cancel.is_set() or self.stop.is_set():
                        raise ValueError("Replay rendering canceled")
                    if canonical:self.replay_work.eligible(replay)
                    self.replay_work._reserve_asset()
                    usage=sum(r.path.stat().st_size+sum(map(len,r.frames)) for r in self.replays.values())
                    if usage+replay.path.stat().st_size+sum(map(len,replay.frames))>self.foundation.settings.replay.ready_bytes:
                        raise ValueError('capacity_reached: ready replay storage')
                    self.replays[replay_id] = replay
                    self.jobs[replay_id].update(state="ready", render_s=time.monotonic() - start)
                    self.jobs[replay_id]['stages']['asset_ready_utc']=time.time()
                self.log("replay_ready", id=replay_id, render_s=round(time.monotonic() - start, 3))
            except Exception as error:
                path = self.cfg.runtime / "replays" / f"{replay_id}.mp4"
                path.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
                with self.render_lock:
                    self.replays.pop(replay_id, None)
                    self.jobs[replay_id].update(state="failed", error=str(error), canceled=self.render_cancel.is_set())
                self.log("replay_failed", id=replay_id, error=str(error))
            finally:
                if resolved:
                    resolved.release()
                with self.render_lock:
                    self.rendering = False

        self.render_thread = threading.Thread(target=worker, daemon=True)
        try:
            self.render_thread.start()
        except RuntimeError:
            resolved.release()
            with self.render_lock:
                self.rendering = False
                self.jobs[replay_id].update(state="failed", error="Replay worker could not start")
            raise ValueError("Replay worker could not start")
        return {"id": replay_id, "state": "rendering"}

    def cancel_render(self, job_id):
        if self.replay_work.cancel(job_id):return
        with self.render_lock:
            job = self.jobs.get(job_id)
            if not job or job['state'] != 'rendering':
                raise ValueError('This preparation is no longer running')
            self.render_cancel.set()

    def human_action(self, data, op=None):
        # Compatibility routes retain their response shape, but use the same authority path.
        aid = data.get('id', uuid.uuid4().hex)
        args = {k: v for k, v in data.items() if k not in ('id', 'action', 'revision', 'expected')}
        request = {'id': aid, 'op': op or data.get('action'), 'args': args}
        if 'expected' in data:
            request['expected'] = json.loads(json.dumps(data['expected']))
            if 'revision' in data and isinstance(request['expected'], dict):
                request['expected']['program_revision'] = data['revision']
        elif 'revision' in data:
            target = {}
            if type(args.get('slot')) is int:
                target['slot'] = args['slot']
            if isinstance(args.get('replay_id'), str):
                target['replay_id'] = args['replay_id']
            with self.control.lock:
                existing = self.control.actions.get(aid)
                request['expected'] = json.loads(json.dumps(existing['expected'])) if existing and existing['expected'] is not None else self.control.expected(target)
            request['expected']['program_revision'] = data['revision']
        record = self.control.submit(request)
        if record['state'] == 'Rejected':
            raise ValueError(record['reason'] + ('; crew paused after takeover' if record.get('takeover') else ''))
        if request['op'] in ('live', 'audio', 'replay', 'holding', 'graphics'):
            return self.program.status()
        if request['op'] == 'prepare':
            return {'id': record['job_id'], 'state': self.jobs[record['job_id']]['state']}
        return record

    def close(self):
        if self.closed: return
        self.closed = True
        self.stop.set()
        self.examples.close()
        self.video_analysis.close()
        if self.direction.thread.is_alive():self.direction.thread.join(timeout=3)
        if self.control.thread.is_alive():self.control.thread.join(timeout=3)
        self.render_cancel.set()
        if self.replay_work.thread.is_alive():self.replay_work.thread.join(timeout=3)
        if self.monitor_thread:
            self.monitor_thread.join(timeout=8)
        self.program.close()
        self.direction.drain_receipts()
        with self.sources_lock:
            sources = list(self.sources.values())
        for source in sources:
            source.close()
        stop_process(self.gateway_process)
        for server in self.servers:
            server.should_exit = True
        if self.recording_thread:
            self.recording_thread.join(timeout=15)
        if self.render_thread:
            self.render_thread.join(timeout=35)
        self.foundation.close()
        self.leases.db.close()


def sample(args):
    access = json.loads((args.runtime / "access.json").read_text())
    context = ssl.create_default_context(cafile=args.ca_cert) if args.ca_cert else None
    leases, processes = [], []
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        for _ in range(args.count):
            lease = request_json(access["public_url"] + access.get('public_path_prefix', '') + "/api/leases",
                                 {"code": access["join_code"], "client": uuid.uuid4().hex}, context=context)
            leases.append(lease)
            (args.runtime / ("sample-"+lease["lease_id"])).touch(mode=0o600)
            slot = lease["slot"]
            command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
            if args.file:
                # Unequal MP4 audio/video durations otherwise leave a gap at every
                # loop. Prepare a video-only CFR fixture, then add a labeled test tone.
                normalized = args.runtime / f"sample-input-{lease['lease_id']}.mp4"
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(args.file),
                                "-an", "-filter_threads", "1", "-vf",
                                f"fps={access['fps']},scale={access['width']}:{access['height']}:force_original_aspect_ratio=decrease,"
                                f"pad={access['width']}:{access['height']}:(ow-iw)/2:(oh-ih)/2",
                                "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                                "-g", str(access["fps"]), "-bf", "0", "-movflags", "+faststart", str(normalized)],
                               check=True, capture_output=True, timeout=60)
                command += ["-re", "-stream_loop", "-1", "-i", str(normalized),
                            "-f", "lavfi", "-i", f"sine=frequency={220 + slot * 110}:sample_rate=48000",
                            "-map", "0:v:0", "-map", "1:a:0"]
            else:
                command += ["-re", "-f", "lavfi", "-i",
                            f"testsrc2=size={access['width']}x{access['height']}:rate={access['fps']}",
                            "-f", "lavfi", "-i", f"sine=frequency={220 + slot * 110}:sample_rate=48000"]
            command += ["-filter_threads", "1", "-vf",
                        f"setpts=N/({access['fps']}*TB),scale={access['width']}:{access['height']},drawtext=fontfile={os.environ['BREADCAST_FONT']}:"
                        f"text='SAMPLE CAMERA {slot} - TEST TONE':x=20:y=80:fontsize=24:fontcolor=white:box=1:boxcolor=black",
                        "-r", str(access["fps"]), "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
                        "-tune", "zerolatency", "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-bf", "0",
                        "-g", str(access["fps"]), "-c:a", "libopus", "-ac", "1",
                        "-f", "rtsp", "-rtsp_transport", "tcp",
                        f"rtsp://publisher:{lease['token']}@127.0.0.1:{access['rtsp_port']}/{lease['source_path']}"]
            log = open(args.runtime / f"sample-{slot}.log", "ab")
            try:
                processes.append(subprocess.Popen(command, stderr=log))
            finally:
                log.close()
            print(f"Publishing SAMPLE CAMERA {slot}", flush=True)
        deadline = time.monotonic() + args.seconds if args.seconds else float("inf")
        while not stop.wait(0.2) and time.monotonic() < deadline:
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("A sample publisher exited; read sample-N.log")
    finally:
        for process in processes:
            stop_process(process)
        for lease in leases:
            try:
                request_json(access["public_url"] + access.get('public_path_prefix', '') + f"/api/lease/{lease['lease_id']}/release", {},
                             token=lease["token"], context=context)
            except OSError:
                pass
            (args.runtime / f"sample-input-{lease['lease_id']}.mp4").unlink(missing_ok=True)


def main():
    import sys
    if sys.argv[1:2] == ['stack-probe']:
        from provider_probe import main as probe_main
        return probe_main(sys.argv[2:])
    if sys.argv[1:2] == ['stage-server-videos']:
        from server_videos import main as videos_main
        return videos_main(sys.argv[2:])
    if sys.argv[1:2] == ['one-camera-check']:
        from sprint_one_check import main as sprint_one_main
        sys.argv.pop(1)
        return sprint_one_main()
    if sys.argv[1:2] == ['five-camera-check']:
        from sprint_two_check import main as sprint_two_main
        sys.argv.pop(1)
        return sprint_two_main()
    if sys.argv[1:2] == ["check"]:
        from check import main as check_main
        sys.argv.pop(1)
        return check_main()
    if sys.argv[1:2] == ["foundation-check"]:
        from foundation_check import main as foundation_main
        sys.argv.pop(1)
        return foundation_main()
    if sys.argv[1:2] == ["direction-check"]:
        from direction_check import main as direction_main
        sys.argv.pop(1)
        return direction_main()
    if sys.argv[1:2] == ["replay-check"]:
        from replay_check import main as replay_main
        sys.argv.pop(1)
        return replay_main()
    if sys.argv[1:2] and sys.argv[1] in ("foundation-capabilities","foundation-query","foundation-worker"):
        from foundation_cli import main as foundation_main
        return foundation_main(sys.argv[1])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Start the headless browser studio")
    runtime = Path(os.environ.get("BREADCAST_RUNTIME", str(ROOT.parent / ".runtime")))
    serve.add_argument("--runtime", type=Path, default=runtime)
    serve.add_argument("--bind", default=os.environ.get("BREADCAST_BIND", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("BREADCAST_PORT", "8080")))
    serve.add_argument("--port-offset", type=int, default=int(os.environ.get('BREADCAST_PORT_OFFSET','0')))
    serve.add_argument("--public-url", default=os.environ.get("BREADCAST_PUBLIC_URL"),
                       help="Reachable browser origin for QR and page links; HTTP is supported for demos")
    serve.add_argument("--ice-host", action="append", help="Reachable media IP or DNS name")
    serve.add_argument("--ice-servers", type=Path, default=os.environ.get('BREADCAST_ICE_SERVERS') or None, help="JSON array of MediaMTX STUN/TURN settings")
    serve.add_argument("--webrtc-port",type=int,default=int(os.environ['BREADCAST_WEBRTC_PORT']) if os.environ.get('BREADCAST_WEBRTC_PORT') else None)
    serve.add_argument('--viewer-transport', choices=('webrtc', 'hls'), default=os.environ.get('BREADCAST_VIEWER_TRANSPORT', 'webrtc'))
    serve.add_argument("--tls-cert",default=os.environ.get('BREADCAST_TLS_CERT') or None)
    serve.add_argument("--tls-key",default=os.environ.get('BREADCAST_TLS_KEY') or None)
    serve.add_argument("--delay", type=float, default=float(os.environ.get('BREADCAST_DELAY','3')))
    serve.add_argument("--reservation-seconds", type=float, default=float(os.environ.get('BREADCAST_RESERVATION_SECONDS','60')))
    serve.add_argument("--reconnect-seconds", type=float, default=float(os.environ.get('BREADCAST_RECONNECT_SECONDS','20')))
    serve.add_argument("--program-proof",type=Path,help="Isolated validation: duplicate encoded packets into a local proof file")
    serve.add_argument("--foundation-config", type=Path, default=os.environ.get("BREADCAST_FOUNDATION_CONFIG") or None)
    serve.add_argument("--quiet", action="store_true", help="Keep page addresses out of service logs")
    serve.add_argument('--public-path-prefix', default=os.environ.get('BREADCAST_PUBLIC_PATH_PREFIX', ''))
    serve.add_argument('--operator-auth', choices=('local', 'proxy', 'token'), default=os.environ.get('BREADCAST_OPERATOR_AUTH') or None)
    serve.add_argument('--operator-token-file', type=Path, default=os.environ.get('BREADCAST_OPERATOR_TOKEN_FILE') or None)
    serve.add_argument('--server-videos-config', type=Path,
        default=os.environ.get('BREADCAST_SERVER_VIDEOS_CONFIG') or ROOT.parent / 'config/server-videos.json')
    serve.add_argument('--crew-mode', choices=('automatic', 'human'),
        default=os.environ.get('BREADCAST_CREW_MODE', 'automatic'))
    samples = sub.add_parser("sample", help="Publish labeled sample media; this is not a live phone test")
    samples.add_argument("--runtime", type=Path, default=runtime)
    samples.add_argument("--count", type=int, choices=range(1, 6), default=1)
    samples.add_argument("--file", type=Path, help="Loop a local video instead of generated test video")
    samples.add_argument("--seconds", type=float, default=0, help="Zero runs until Ctrl-C")
    samples.add_argument("--ca-cert", help="CA certificate for a private HTTPS server")
    args = parser.parse_args()
    args.runtime = args.runtime.resolve()
    if args.command == "sample":
        return sample(args)
    if not 0 <= args.delay <= 10 or not 0 < args.reservation_seconds <= 120 or not 0 < args.reconnect_seconds <= 60:
        parser.error("Delay must be 0–10s, reservation 0–120s, reconnect 0–60s")
    if not 0 <= args.port_offset <= 40000 or not 1024 <= args.port <= 65535:
        parser.error("Use an unprivileged port and a port offset of 0–40000")
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("Supply both --tls-cert and --tls-key")
    if not args.public_url and not loopback_host(args.bind):
        parser.error('Set BREADCAST_PUBLIC_URL or --public-url when listening outside loopback; QR codes need the reachable browser address')
    try:public_url=public_origin(args.public_url or f"{'https' if args.tls_cert else 'http'}://localhost:{args.port}")
    except ValueError as error:parser.error(str(error))
    remote = not loopback_host(urllib.parse.urlsplit(public_url).hostname) or not loopback_host(args.bind)
    operator_auth = args.operator_auth or ('token' if remote else 'local')
    if remote and operator_auth == 'local':
        parser.error('Remote operation requires token access or a verified private authenticated proxy')
    if remote and urllib.parse.urlsplit(public_url).scheme != 'https':
        parser.error('Remote phone capture requires a trusted HTTPS public origin')
    if args.public_path_prefix not in ('', '/app'):
        parser.error('Public path prefix must be empty or /app')
    for file in (args.tls_cert, args.tls_key):
        if file and not Path(file).is_file():parser.error('TLS certificate and key must be readable files')
    if operator_auth == 'token':
        # Validate the secret before creating a runtime lock or media resources.
        try:Config(args.runtime, public_url, operator_auth='token', operator_token_file=args.operator_token_file)
        except ValueError as error:parser.error(str(error))
    args.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.runtime, 0o700)
    lock = open(args.runtime / "server.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("This runtime directory already has a running studio")
    cfg = Config(args.runtime, public_url, args.bind, args.port, args.port_offset,
                 delay=args.delay, reservation_seconds=args.reservation_seconds,
                 reconnect_seconds=args.reconnect_seconds, certificate=args.tls_cert, private_key=args.tls_key,
                 ice_hosts=tuple(args.ice_host or
                                 [h.strip() for h in os.environ.get("BREADCAST_ICE_HOSTS", "").split(",") if h.strip()]
                                 or []),
                 ice_servers=tuple(json.loads(args.ice_servers.read_text())) if args.ice_servers else (),
                 print_access=not args.quiet, foundation_config=args.foundation_config or None,program_proof=args.program_proof,webrtc_port=args.webrtc_port,
                 public_path_prefix=args.public_path_prefix, operator_auth=operator_auth, operator_token_file=args.operator_token_file,
                 server_videos_config=args.server_videos_config, crew_mode=args.crew_mode,
                 viewer_transport=args.viewer_transport)
    app = App(cfg)
    server = uvicorn.Server(uvicorn.Config(web_api(app), host=cfg.bind, port=cfg.port, workers=1,
        reload=False, access_log=False, log_level="warning", proxy_headers=False,
        ssl_certfile=cfg.certificate, ssl_keyfile=cfg.private_key))
    def end_when_stopped():
        app.stop.wait()
        server.should_exit = True
    threading.Thread(target=end_when_stopped, daemon=True).start()
    try:
        server.run()
    finally:
        app.close()
        lock.close()



if __name__ == "__main__":
    raise SystemExit(main())
