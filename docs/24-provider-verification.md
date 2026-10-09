# Breadcast provider verification record

**Updated:** 2026-10-09

The registered-video archive adapter now performs tenant and source checks at
runtime before upload/inspection. Its real VM run is pending. The user requested
that implementation checks be skipped while the full stack is connected. No
external gate closes from this code change alone.

This is the authority for external capability facts and integration decisions in
[PRD 22](22-live-stack-integration-prd.md). The integration coordinator updates it
from sanitized evidence. An unverified row does not establish permission,
availability, or a working API contract.

Recorded 2026-10-09 against application commit `c06e385` and workshop commit
`0c6b7561856cf54a609dd3f0cf1e8f80c62c88ea`. No live provider gate is verified.

## 1 Current environment

| Check | Observed result | Meaning |
|---|---|---|
| Workshop files versus upstream | `.cursor`, workshop Markdown and `config.example` match the checked upstream revision | Documentation is current for that revision; services remain untested |
| Workshop environment names | No nonempty variables from `config.example` found in this session | This process has no workshop configuration |
| `/config` | Absent | Team VM configuration is not mounted here |
| Local `.env` | Breadcast variable names only; secret values were not printed | It does not configure workshop services |
| Docker | CLI exists; daemon connection unavailable | No container build or runtime acceptance was run |
| Python dependencies | `av`, `pydantic`, `pydantic_ai`, and `pytest` absent from system Python at review | System Python cannot run the application suites as configured |
| Missing design files | Restored from the named backup; see [manifest](restoration-manifest.json) | Restores references/build inputs; historical tests are not rerun results |

The next access step is to execute probes in the assigned workshop environment or
connect this session to that authorized environment. Do not ask for passwords,
tokens, or raw config files in chat. List missing variable names and where the
workshop expects them. Do not inspect another team's files.

## 2 Gate register

Allowed status values are `unverified`, `verified`, `failed`, and `blocked`.
Verification needs an actual request and the proof below. A blocked gate states
the exact external prerequisite. All timestamps are UTC.

| Gate | Owner | Current status | Exact blocker or next proof |
|---|---|---|---|
| G01 Team access | W01 | blocked | No assigned workshop environment in this session; obtain authenticated identity and backend config there |
| G02 New camera ingestion | W02 with W01 | unverified | Resolve upload-versus-reingest conflict with an actual permitted upload and indexed result |
| G03 Perception and timing | W03 | unverified | Capture real video reasoning/detection output and decode its timing/geometry |
| G04 Search | W04 | unverified | Query the team index, identify filters/version/scope, and play a result |
| G05 Live capacity | W03 with W07 | unverified | Measure the selected path across five sources under PRD 22 targets |
| G06 W&B roles | W05 | unverified | Discover usable models; verify structured output and segmentor images |
| G07 Speech | W05 | blocked | NVIDIA Magpie NIM and optional OpenAI-compatible speech adapters connected; actual team endpoint and voice remain unverified |
| G08 Hosting and phones | W06 | unverified | Verify packaging/host, resources, HTTPS, operator boundary and reachable WebRTC route |
| G09 Tracking | W03 | unverified | Determine provider state ownership; otherwise verify the local tracker and behavior |

Each unknown has an owner, a resolution procedure, and blocked behavior. Agents
must not invent an external value to make a row appear complete.

## 3 Known documentation conflicts

| Conflict | Sources | Resolution rule |
|---|---|---|
| New uploads | [Architecture](../ARCHITECTURE_REFERENCE.md) restricts the challenge path to existing segments; [upload skill](../.cursor/skills/ingest/upload-video/SKILL.md) documents new files | G02 needs an actual permitted upload and full processing result |
| GPU addresses and authentication | [Config example](../config.example) uses independent endpoints and says no GPU token; [GPU guide](../.cursor/skills/gpu/README.md) uses a fixed host and bearer | Use the assigned environment and verified contract. Do not overwrite addresses or send credentials to the fixed host without proof |
| Canary discovery | Config comments suggest model discovery; GPU guide says `/v1/models` can be absent | Check documented health paths and actual metadata; a model-list 404 alone does not prove failure |
| Custom DataEngine functions | Restored [integration design](03-stack-integration.md) proposes custom functions; workshop says use the existing graph | PRD 22 selects existing VSS processing and the local render worker |
| Deployment | Compose exposes media ports; [workshop app skill](../.cursor/skills/deployment/deploy-app-no-registry/SKILL.md) describes a small HTTP app under `/app` | G08 verifies packaging and media reachability; a page loading is insufficient |
| Historical acceptance | Restored reports describe earlier validation | New acceptance uses the current commit/configuration and cannot inherit those pass flags |

## 4 Probe sequence

Run dependent steps sequentially. Independent read-only health checks may run
concurrently within the verified request budget. Read the matching skill before
using a provider route. These are documented candidates, not tested tenant APIs.

| Step | Documented surface | Proof to retain |
|---|---|---|
| 1 Identity | `POST /api/v1/auth/login`, `GET /api/v1/auth/me` | Redacted identity, backend contract, token refresh behavior; no token value |
| 2 Configuration | Backend health from deployment skill; `GET /api/v1/config` | Returned backend version, upload limits, supported fields and request limits |
| 3 Metadata | `/api/v1/metadata/schema`, `/api/v1/metadata/values`, `/api/v1/metadata/ingest-config` | Exact filters, camera metadata semantics, prompt/scenario choices |
| 4 Existing archive | `/api/v1/videos/explore`, `/api/v1/videos/metadata`, `/api/v1/videos/detections`, `/api/v1/videos/stream` | Indexed clip and actual timing/detection shapes; label external corpus |
| 5 Search | `POST /api/v1/search` | Query/response, scores, index/embedding identity, scope and playable result |
| 6 New clip | `POST /api/v1/videos/upload`, then documented index reads | Selected file/hash, authorized visibility/prompt, object identity, pipeline and original-byte proof |
| 7 Models | Per-model health and inference from GPU skills | Video request/response, formats, sample times, actual model/config revisions |
| 8 W&B | Verified serverless model discovery and inference contract | Actual models, role result, image input result, limits and scope |
| 9 Voice | Selected TTS service API | Voice/language, requested words, generated audio and measured duration |
| 10 Network | Actual app entry, HTTPS and media session | Phone/viewer reachability, ICE route, prefix and operator protection |

Do not modify the shared corpus or pipeline as a health check. An upload or
re-ingest follows the matching skill's input and authorization rules. This record
does not authorize uploading unrelated material.

## 5 Required proof per gate

### G01 Team and API access

Record team/namespace, backend configuration fingerprint, tested route/protocol,
returned backend version or explicit unknown, and access scope. Record credential
variable names, with values only in the runtime secret store. Login to the wrong
team fails the gate.

### G02 New ingestion

Record input SHA-256, size, codec/container/duration, upload size limit, exact
prompt mode/revision, visibility, camera metadata, returned object key, parent
and segment URIs, readable original checksum, pipeline execution/trace when
exposed, and indexed completion time. A missing provider trace stays null; keep
the separate application correlation ID.

Verify duplicate and ambiguous upload behavior. State whether idempotency is
documented and tested. If exact reconciliation is impossible, record the
`submission_unknown` outcome. Record object read permissions and how private
media reaches models. Do not publish signed URLs.

### G03 Perception

For each model record actual ID, version or version basis, protocol, auth mode,
input bounds, formats, orientation, sampling, timestamp origin/units, response
limits and sample output. Use independent visual labels. Establish whether
descriptions apply to full segments or supported subintervals. Record missing
fields explicitly.

### G04 Search

Record route/payload, valid filters, private/public behavior, index/collection
identity, embedding model/version/dimensions, score/distance semantics, candidate
limits, pagination, timing, parent/segment mapping and original retrieval. If a
provider revision is absent, prove and label the application-owned compatibility
fingerprint. Do not invent a server version.

### G05 Capacity

Record source count, input resolution/rate, chunk duration, actual window/step,
selected path, provider quotas, application concurrency, sample count, p50/p95/max,
deadline misses, skips, rate limits, backlog and resources. Compare with PRD 22
without adjusting targets after the run. A configuration change requires a new
revision and rerun. A throughput estimate is not five-source proof.

### G06 W&B models

Record endpoint, usable role model IDs, account scope, SDK/version, structured
JSON/tool capability, multimodal shape and image transport, context/output bounds,
request/concurrency limits and one real result per role. Test invalid-output
rejection and cancellation. Segmentor proof includes its inspected images.

### G07 Speech

Record service, verified model/voice IDs, API/SDK revision, language, pronunciation
input, voice access, text limits, output format, quotas, request/decode latency,
text binding basis, audio samples/duration/hash, and listening results. An
unavailable selected voice cannot silently become an arbitrary default voice.
Caption fallback leaves this gate open.

### G08 Hosting

Record permitted placement and deployment method, namespace, artifact transport,
CPU/memory/disk bounds, persistent runtime path, public origin/prefix, trusted
HTTPS, outbound provider access, inbound media route, ICE/TURN behavior, phone
browsers, operator boundary and rollback. TURN passwords remain secrets.

### G09 Tracking

Record provider tracker support, session/source keys, expiry, ordering and reset.
If detections are stateless, record the local tracker version/configuration and
CPU cost. Test similar objects, occlusion, interleaved cameras, missing intervals
and epoch replacement. Cross-camera person identity remains out of scope.

## 6 Explicit implementation decisions

| Decision | Selected rule | Change condition |
|---|---|---|
| Default processing | Existing VSS upload and DataEngine pipeline | Verified G02/G05 outcome selects PRD 22's alternate path |
| Custom pipeline deployment | None | Explicit scope change and permitted tenant capabilities |
| Replay execution | Existing Breadcast worker | No alternate platform in this scope |
| Rehearsal profile | Community, English, neutral graphics, unknown names/scores | Real setup supplies actual values |
| Clip visibility | Private controlled test footage | User-authorized visibility change |
| Model and voice IDs | Unset until verified | G03/G06/G07 proof |
| Camera cap | Five occupied leases | Fixed requirement |
| Operator access | Trusted boundary; event-scoped credential if public hosting lacks one | G08 selects mechanism and W00 freezes it |
| Search scope | Current event/run registered media | Cross-run/corpus browsing requires a scope change |
| No speech | Eligible caption or silence, degraded status | Cannot close R05 |
| Analysis route change | Explicit configuration revision before a run | Never silent runtime fallback |
| Hosting fallback | Unselected until G08 verifies a host and route | No assumed EC2 or Kubernetes privileges |

## 7 Evidence entry format

This is a template, not an executed probe. Replace placeholders only with measured
facts. Keep unknown fields null.

```json
{
  "gate": "G03",
  "status": "unverified",
  "checked_utc": null,
  "application_commit": null,
  "configuration_fingerprint": null,
  "provider_contract_id": null,
  "model_id": null,
  "version": null,
  "version_basis": null,
  "capabilities": [],
  "limits": {},
  "application_trace_id": null,
  "provider_request_id": null,
  "evidence_artifacts": [],
  "blocker": "No live video inference has been run",
  "next_action": "Run the selected model probe in the assigned team environment"
}
```

Each artifact needs a resolvable location, SHA-256, content type and description.
Redact auth headers and secret-bearing parameters before hashing committed
sanitized artifacts. Private proof can remain at an approved private location;
record its sanitized counterpart separately.

A fixture cannot verify a live gate. When a verified service becomes unhealthy,
retain its historical proof and set operational status unavailable. A changed
provider/configuration identity needs a fresh relevant probe before live use.
