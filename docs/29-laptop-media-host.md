# Laptop media host

The laptop runs the persistent media runtime. Supabase hosts the Gemini function,
private clips and vector database. The public pages are available through a
temporary Cloudflare Quick Tunnel. Video uses WebRTC with configured TURN relays.
TURN passes media between the laptop and viewers on other networks.

```mermaid
flowchart LR
  Viewer[Viewer browser] --> Tunnel[Cloudflare HTTPS]
  Tunnel --> Laptop[Laptop runtime]
  Laptop --> TURN[TURN relay]
  TURN --> Viewer
  Laptop --> Supabase[Supabase backend]
  Supabase --> Gemini[Gemini API]
```

The tunnel carries pages and media session requests. The TURN relay carries
video when a direct media connection is unavailable. Gemini streaming runs
between the laptop and Supabase, independently of browser playback.

## Current state

The ARM container built and started. Docker reports `healthy`. Local health,
public health and public Viewer requests returned HTTP 200. The initial startup
failed because a public origin had only a loopback ICE address. Clearing
`BREADCAST_ICE_HOSTS` corrected that failure. ICE addresses identify possible
media connection endpoints.

The program remains in holding. The agent did not start the bundled video.
Public video playback, TURN allocation, Gemini inference, speech, clip receipts,
vectors and latency remain unverified. Acceptance checks were skipped under the
user's standing instruction. The authoritative observations are in the
[host record](evidence/laptop-media-host.json).

The laptop has 24 GiB RAM. Its isolated `breadcast-local` Colima profile has
4 CPUs, 6 GiB RAM and a 30 GiB data disk. Its Docker context is
`colima-breadcast-local`. The existing `breadcast-experiment` profile remains
separate. HTTP binds to `127.0.0.1:9080`. Media uses TCP and UDP port 9189.

## Check status

Run these commands from the repository root:

```sh
docker --context colima-breadcast-local ps -a --filter name=breadcast-studio
curl --fail http://127.0.0.1:9080/healthz
DOCKER_CONTEXT=colima-breadcast-local ./scripts/studio logs --tail=50 studio
```

`Up ... (healthy)` means the container health probe passed. That probe checks
the media gateway and advancing program frames. A successful health response
does not prove playback on a public viewer's network or Gemini inference.

The current public origin is in ignored `.runtime/laptop-origin`. Append
`/operator` to open Studio or `/watch` to open Viewer. Use the existing credential
in ignored `.runtime/operator-token` for Studio. Keep it private. Viewer does
not need that credential.

## Restart the app

```sh
DOCKER_CONTEXT=colima-breadcast-local ./scripts/studio restart
```

Use this command after source, Compose or `.env` changes. It preserves the named
runtime volume. Each app restart begins in holding. If Colima is stopped, start
only the laptop profile:

```sh
colima start --profile breadcast-local --cpus 4 --memory 6 --disk 30 \
  --vm-type vz --activate=false --ssh-config=false --port-forwarder grpc
```

## Tunnel and media settings

The ignored `.env` selects these non-secret host settings:

```dotenv
BREADCAST_HTTP_BIND=127.0.0.1
BREADCAST_HTTP_PORT=9080
BREADCAST_PORT=8080
BREADCAST_WEBRTC_PORT=9189
BREADCAST_ICE_HOSTS=''
BREADCAST_ICE_SERVERS=/run/breadcast/tls/ice-servers.json
BREADCAST_OPERATOR_TOKEN_SOURCE=.runtime/operator-token
BREADCAST_CPUS=4
```

`BREADCAST_PUBLIC_URL` must match the current tunnel origin. Supabase server
credentials and the shared function credential also remain in ignored `.env`.
The TURN file is ignored `.runtime/tls/ice-servers.json`. It uses the provider's
published static authentication configuration and TCP fallback. Actual relay
availability and playback remain pending; this is shared external infrastructure.

Cloudflared is installed through Homebrew. The current tunnel runs in the
background. Its PID and log are in `.runtime/laptop-tunnel.pid` and
`.runtime/laptop-tunnel.log`. To create a new tunnel after it stops, run:

```sh
cloudflared tunnel --url http://127.0.0.1:9080 --protocol http2 --no-autoupdate
```

Keep that process running. Save its new HTTPS origin in `.runtime/laptop-origin`
and update `BREADCAST_PUBLIC_URL` in `.env`. Then restart the app with the command
above. Existing Viewer links become invalid when the origin changes.

[Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
are temporary development endpoints. They do not provide an uptime guarantee or
browser SSE support. This app uses browser polling; hosted Gemini SSE uses a
separate connection to Supabase.

The current `caffeinate` process prevents idle sleep while the tunnel runs. Its
PID is in `.runtime/laptop-caffeinate.pid`. Keep the laptop powered, connected to
the internet and awake. Closing the lid or stopping the tunnel can end access.

The media configuration follows the provider's
[Open Relay static authentication guide](https://www.metered.ca/tools/openrelay/)
and [MediaMTX TURN support](https://mediamtx.org/docs/features/webrtc-specific-features).

## Manual release check

1. Open Studio and enter the operator credential. Prepare event graphics.
2. Press **Start video** below the program video. Confirm Automatic mode.
3. Open Viewer on another network. Enable audio and confirm advancing video.
4. Confirm actual Gemini results, speech, private clip records and scene search.
   Follow the [full acceptance steps](28-gemini-supabase-migration.md#production-acceptance-for-this-candidate).
5. Press **Stop video**. Confirm the program returns to holding.

Record failures and measured latency against the source release. HTTP 200 and
container health are startup observations; they do not complete these checks.
