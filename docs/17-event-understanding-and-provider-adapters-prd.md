# Breadcast event understanding and provider adapters PRD

**Updated:** 2026-10-09

Build the shared event history and provider boundaries that the live director, witty spoken commentator, and replay segmentor will use. Prove the application pipeline locally with real recorded media before hackathon services are available. Later, connect verified providers through the same boundaries and run the same contract checks.

Historical status: local-ready. E01–E17 passed with real media and labeled fixtures. Live verification remains open. See the [evidence record](evidence/event-foundation.json) and [run guide and handoff](18-event-foundation.md). This is Task 1 in the [build plan](07-build-and-demo.md#three-remaining-integration-tasks). Its audience is the engineer or coding agent implementing that task. It owns coverage S01, S02, S03, S04, and S17. It supplies the media and context boundaries needed by S07, S08, S13, and S18.

Spoken commentary is required for the complete product. This PRD prepares its event memory, style settings, model interface, and speech interface. PRD 2 implements the commentator, expressive voice playback, and mixing. A text-only fallback cannot complete the later speech gate.

## Outcome and completion boundaries

At local completion, a real camera or labeled sample recording passes through chunk finalization, immutable storage, analysis windows, simulated provider responses, evidence validation, scene updates, context selection, and search. A returned interval resolves to decodable retained media even after its camera disconnects. Duplicate, delayed, malformed, or failed work cannot change official facts, acquire airtime authority, or stop the program.

The local and live milestones are separate:

| Milestone | Required result |
|---|---|
| Local-ready | Complete application behavior, labeled fixture adapters, real-media proof, failure tests, configuration template, and connection checklist. No provider credentials are required. |
| Live-verified | Actual VAST storage and trigger execution, YOLO/Cosmos evidence, semantic retrieval, and W&B/speech capability requests pass their checks. Record real versions, results, and latency. |

PRD 2 can start after local-ready. Do not wait for credentials to finish local work. Do not report a fixture pass as live verification. The desired hackathon work is connection, adapter translation, tuning, and rehearsal. Unknown APIs, quotas, media access, or model behavior can still require adapter changes.

## Authority and existing code

Read [project instructions](../AGENTS.md), [README](../README.md), [context and contracts](05-context-and-contracts.md), [VAST integration](03-stack-integration.md), and the [studio coverage audit](07-build-and-demo.md#studio-coverage-audit). Apply the [stack integration workflow](../skills/breadcast-stack-integration/SKILL.md). Use the setup and replay workflows when changing their boundaries.

The contracts document remains the authority for records, IDs, clocks, evidence, and ownership. This PRD specifies behavior and acceptance; it does not define another copy of the message schemas. Implement runtime validation for the records this phase consumes or produces. Document proven contract additions there, including any compatibility migration, before dependent code relies on them.

| Existing code | Reuse and necessary integration |
|---|---|
| [studio.py](../app/studio.py) | Keep camera leases, gateway, and run lifecycle. Move HTTP routing to FastAPI while preserving existing routes and browser behavior. Integrate bounded ingestion jobs and provider diagnostics. Coordinate recording cleanup with storage ownership. |
| [media.py](../app/media.py) | Preserve the encoder and live path. Add the source timing/geometry provenance needed to relate original recordings, analysis proxies, and actual program frames. |
| [control.py](../app/control.py) | Preserve action IDs, takeover, current-state checks, and single controller ownership. Replace fixed context revision assumptions with the authoritative context revision. Do not start autonomous direction in this phase. |
| [replay.py](../app/replay.py) | Reuse calibration and source interval validation. Expose validated archive inputs without removing existing live-source eligibility checks. Replay planning and archive replay scheduling belong to PRD 3. |
| [graphics.py](../app/graphics.py) | Preserve existing human score confirmation. Connect confirmed facts to the shared fact history; graphics reads the committed values. Do not introduce another independent score writer. |
| [unit](../tests/unit/), [media](../tests/media/), [browser](../tests/browser/) | Extend focused contract and media checks; preserve admission, controls, replay, graphics, and viewer behavior. |

Use the existing Python application and embedded transactional storage. An outbox is a stored list of notifications awaiting delivery. Reuse that pattern for job/result notifications. Worker logic may also run inside a verified DataEngine function; it returns validated artifacts rather than writing directly to the controller's local database. Do not add a broker, database service, or second media controller.

## Build approach: use libraries and small steps

Use established libraries for common work. Keep custom code for broadcast rules: evidence, clocks, source identity, deadlines, and program authority. Do not build a general agent platform, provider plugin system, HTTP framework, or retry engine. Use explicit configuration and a small adapter registry.

The implementation defaults below are selected for the existing Python application. Pin compatible versions in the existing dependency lock after a small compatibility check. This PRD does not claim those dependencies are installed or that provider compatibility has been verified.

| Need | Selected library or existing component | Breadcast's remaining work |
|---|---|---|
| Validated records and configuration | [Pydantic strict models](https://docs.pydantic.dev/latest/concepts/strict_mode/) and [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | Validate domain relationships, revisions, media references, and deadlines. Reject unknown fields at contract boundaries. Generate schema from the models; do not maintain another handwritten schema. |
| Typed LLM calls | [Pydantic AI](https://ai.pydantic.dev/agents/) | Supply bounded context, role instructions, typed output, and the original decision snapshot. Use a bounded call through the existing coordinator; no new autonomous orchestration service. |
| HTTP API | [FastAPI](https://fastapi.tiangolo.com/features/) with Uvicorn | Replace route parsing and request validation while preserving current paths, response shapes, status codes, media proxy behavior, and access restrictions. Reuse current application services and browser pages. |
| Verified S3-compatible storage | [boto3/botocore](https://docs.aws.amazon.com/boto3/latest/guide/s3.html) | Translate immutable artifact operations after actual VAST S3 access is verified. Use SDK signing and transfers. Local mode uses real files; it needs no storage server. |
| Other provider transport | Verified official SDK first; HTTPX when no suitable SDK exists | Thin request/result translation only. Keep provider URLs, model IDs, and supported capabilities configurable. |
| Local state and bounded jobs | Python `sqlite3`, queues, and existing worker primitives | Small transactions, job limits, notification recovery, and the domain rules above. No ORM, Redis, Celery, or extra workflow service. |
| Media and web UI | Existing FFmpeg, PyAV, MediaMTX, and HTML/CSS/JavaScript | Add required provenance and diagnostics. Keep the current UI; no frontend migration is needed. |
| Tests | Existing test suite plus library test adapters | Use [Pydantic AI FunctionModel/override](https://ai.pydantic.dev/testing/), [botocore Stubber](https://botocore.amazonaws.com/v1/documentation/api/latest/reference/stubber.html), and [HTTPX MockTransport](https://www.python-httpx.org/advanced/transports/). Keep real-media checks around the simulated responses. |

For W&B, use Pydantic AI's [OpenAI-compatible provider support](https://ai.pydantic.dev/models/openai/) only if the supplied endpoint passes a capability probe. Select its supported protocol explicitly; do not assume that chat and Responses APIs are interchangeable. Install only needed extras. Do not substitute an unrelated hosted model. If the endpoint requires another verified transport, contain that change in the adapter.

Run one Uvicorn worker. Use its application lifecycle to start and stop the existing gateway, media pipeline, and workers exactly once. Disable automatic reload in the media runtime. Keep blocking inference outside request handling and media locks. Test shutdown and existing HTTP behavior before adding analysis. A framework change must not create two encoders or two owners of program state.

Use one total retry/deadline budget across the SDK, model library, and application. Disable or account for nested retries so they cannot multiply attempts or extend a live deadline. Enforce required domain checks after library validation; valid JSON alone does not establish valid broadcast evidence.

### One local command and an early working result

Extend the existing launcher with `./scripts/studio foundation-check`. This is a command to implement, not a command that exists today. It must build and run an isolated Docker check, create temporary local storage, load checked-in sample media and labeled AI responses, execute the foundation checks, and write one report with artifact paths. It must exit nonzero on failure and release its resources. Require Docker and the repository only; no credentials, separate database, manual data seeding, or extra service setup. The initial image/dependency build may need network access; fixture execution must make no external provider requests.

The first working increment is one sample camera → one finalized chunk → one fixture observation → one stored scene → one query that resolves real media. Reuse the final contracts and application services for this increment. Then add timing proof, recovery, corrections, and five-source checks in small steps. Do not wait for every provider adapter to exist before proving this path.

Provide one fixture configuration and one live configuration template with the same keys. The handoff must show the startup command, required configuration fields, capability-check command, and where to read failures. Keep existing `serve`, `sample`, and `check` commands working. Do not require developers to run a manual sequence of database, queue, storage, and AI servers.

## Scope

This phase implements event records, media provenance, retained-media access, bounded analysis jobs, evidence and scene storage, context selection, search boundaries, fixture adapters, and provider capability checks. The library adoption above supports those features. It adds only their required internal interfaces, diagnostics, and local commands; it does not redesign the Studio.

Keep these features in their later tasks:

- PRD 2: event setup UI and graphics package binding, live director decisions, live zoom, commentary writing, speech mixing/scheduling, caption placement, and listening review.
- PRD 3: automatic replay editing/scheduling, archive replay integration, user-facing natural-language recall, and the full phone/viewer rehearsal.

Do not add automatic score/identity recognition, general natural-language control, speech transcription, new video effects, new voice personas, model training, multiple events, accounts, or distribution services. Source record support is not permission to change an active broadcast or deploy new infrastructure.

## Local workflow

1. Load a validated single-event brief through a local configuration or internal application interface. Keep existing Studio controls usable.
2. Capture one camera or explicitly labeled sample. Register the source lease and each continuous timestamp epoch.
3. After a recording segment closes, validate its media and publish an immutable chunk plus its manifest. A manifest identifies the files, timing, and provenance of a ready artifact.
4. Dispatch a ready-manifest notification through the selected job adapter. Select an overlapping analysis window from readable, finalized chunks.
5. Obtain provider output through the selected adapter. Local mode returns labeled observations for the actual test interval; it does not pretend to recognize the footage.
6. Validate and persist observations, update matching scene revisions, and update the search index. Expose bounded context for the director, commentator, and segmentor.
7. Query a captured moment, resolve its retained media, and decode the selected interval as proof. This phase does not put that result on air.

Model work must remain outside camera admission, the media lock, frame selection, encoding, and urgent human controls.

## Event records and official facts

Implement one active event identity and the existing unique server `run_id`. Preserve source identity across stored records; a display slot such as Camera 2 is not an immutable source ID. Starting the server still begins a new run in holding. Old records retain their event/run ownership and cannot become new airtime work.

`EventContext` holds supplied title, profile, participants, branding references, editorial/audio policy, commentary style, language, pronunciations, and voice configuration. Use the [commentator personality](04-agent-instructions.md#commentary-personality-and-delivery) as the default style. Unknown names, score, clock, or provider voice ID stay unknown. A voice may be requested in configuration, but it is not verified until the provider confirms support.

Store validated, immutable numbered revisions. Use revision checks on writes so concurrent edits cannot silently overwrite each other. Expose the active revision to existing control requests and future decision snapshots. Context or official-fact changes invalidate affected pending work; they do not rewrite historical evidence or past program output.

`EventState` references existing camera/program state and stores official fact history from the configured human authority. Each fact retains its value, authority, revision, and effective event time. Do not substitute submission wall time when effective event time is unknown. Such a fact cannot be used as a synchronized historical claim. Preserve the existing explicit confirmation form and its unknown-value behavior. Model output, fixture output, visible scoreboards, and retrieved text cannot confirm official facts.

## Source time and image geometry

The current media buffer contains normalized proxy PTS and a receipt-time origin. PTS means presentation timestamp: where a frame belongs in its media timeline. These values do not establish original capture time or alignment between phones.

Preserve the recorded input timeline, time base, discontinuities, dimensions, orientation, and all normalization transforms. Relate each proxy frame/window to its recorded source interval. Record crop/scale/padding transforms so model coordinates can be converted back to the correct image. Original media timestamps mean the recorded input's native timeline; do not claim that an unavailable phone sensor clock is known.

Use versioned mappings for source-to-event and source-to-program time. Keep mapping uncertainty and valid intervals. Reconnects, timestamp resets, and incompatible format changes must not silently continue an old mapping. A new revision does not move the source interval of an already accepted record.

Existing calibration can support event mapping when its evidence is valid. Do not use local arrival time as a replacement. If event mapping is unknown, retain source-local evidence and explicitly withhold synchronized cross-camera or audience-time claims. The director may later make an explicitly independent view change under the existing controller rules.

Record enough provenance at actual frame submission to reconstruct which source interval the program showed. Keep encoder submission distinct from viewer delivery. Save compact interval mappings or equivalent bounded records rather than an unbounded new copy of every frame. Program history remains owned by the media/controller path.

Local completion requires a marker-based proof from recorded input through proxy coordinates and program output. Native/proxy mapping cannot be left as a stub for the hackathon. Document unavoidable source limits and test their rejection path.

## Chunk storage and retained media

Accept only closed, readable media. File existence or a stable size observed once does not prove finalization. Use a verified completion notification with a recovery scan of finalized files. Associate the recording with its actual source lease/epoch; reject or split a segment that crosses an unresolvable discontinuity.

Write media bytes first, verify size/hash/readability, then publish the ready manifest. Use immutable object identity and the existing logical work key. An identical retry is harmless. Reusing an identity for different bytes fails. A partial upload, missing part, wrong checksum, or unreadable object never produces a ready manifest.

The application owns the media retention decision. The current MediaMTX five-minute deletion and the monitor's age-based unlink loop must not race uploads, analysis, or pins. During implementation, replace or configure these competing cleanup paths under one tested policy. Do not solve the race by allowing unlimited disk growth.

Provide a bounded interval resolver and pin/release operations for original or normalized retained media. A pin prevents deletion while a job is using the bytes. Resolve by event, immutable source/epoch, interval, and mapping revision. Return coverage, provenance, transforms, and availability; do not return a current camera merely because it occupies the same slot.

After buffer eviction and source disconnect, a retained interval must still resolve and decode. A missing interval produces an explicit unavailable result. A current source is not required to read a valid archive object. This does not remove current live replay protections or implement PRD 3 playback. Reuse the [archive boundary](05-context-and-contracts.md#retained-archive-playback-planned-integration).

Cleanup may remove an unpinned local copy after verified storage, or mark an interval unavailable when retention policy permits loss. It must never advertise deleted bytes as playable. Keep small unavailable records long enough to explain search results and retries. Before deleting durable media, invalidate its availability and dependent index entries. Hash verification and retained manifest checks apply when media is resolved again.

## Analysis jobs and evidence

Use configurable overlapping windows over finalized chunks. The starting target is a six-second window every two seconds. Actual recording closure may be longer than two seconds; measure it. Process a shorter available window when the verified model supports it, otherwise report the readiness delay. Do not fabricate padding or claim that a frame-sampling model understood unseen frames.

Maintain progress independently for each source. A gap or failed interval must be visible. A watermark is the time through which processing is complete; it cannot jump over a missing interval without recording that gap. Fair scheduling must prevent a busy source from starving another source.

Track detector state separately per source epoch when tracking is supported. If the provider returns only detections, retain the minimal local tracking state or report detections without stable tracks. A track ID never becomes a roster identity.

Validate provider output against the issued request, referenced bytes, source interval, schema, and configuration revision. Preserve observed facts, inferred labels, uncertainty, and model provenance. Assign fixture/provider origin through trusted adapter code. Never relabel a provider result as operator evidence to pass the current replay interface.

Persist each accepted result and its notification together. Repeated delivery produces one logical result. Older results may add valid historical evidence but cannot roll back a newer scene or official fact. A late live result stays in history when useful; its expired decision window does not restart.

Capture context, source, evidence, and mapping revisions when constructing a request. Carry them with its response. Do not refresh revisions after inference to make stale intent appear current. Return the reviewed snapshot to downstream roles under the [decision snapshot rules](05-context-and-contracts.md#decision-snapshots-planned-integration).

## Scene history and bounded context

Publish observations while an action develops. Each observation has a finite supported interval. A scene can accumulate later observations without waiting for the action to end or a replay to exist.

Merge observations only when their evidence supports the same action. Overlapping windows alone are not proof that two actions are identical. Use stable scene IDs and increasing revisions. Preserve conflicting candidates when association is uncertain. Corrected or retracted evidence invalidates dependent scene claims, pending context, and search entries; retain history for explanation.

Build role context from authoritative records, not a growing chat transcript. Director context includes fresh source health, observations, policies, and program state. Commentator context includes eligible developing/recent actions, earlier relevant moments, official facts effective at the requested time, and actual aired commentary. Segmentor context includes the candidate interval, source availability, mappings, and evidence revisions.

The caller supplies the target program/source interval and the reviewed state. Context selection must exclude future outcomes from audience-facing commentary. If the interval or mapping is unknown, return that limit rather than using the newest capture result. Refer to [audience-aligned context](05-context-and-contracts.md#audience-aligned-context-planned-integration).

Read actual aired text and pending cues through the program-history boundary. PRD 1 must test this boundary with labeled records even though production speech is built in PRD 2. Keep earlier facts and commentary references for witty callbacks and repetition avoidance. Do not add a separate joke memory or summarization agent. If context is trimmed, retain references and explain omissions without converting a summary into an authoritative fact.

## Search foundation

Index supported scene/window descriptions with event, immutable source/epoch, interval, evidence and scene revisions, media availability, and embedding/index version. Use the supplied semantic search adapter when verified. If embeddings are required, indexing and queries use the same model, dimensions, normalization, and distance convention.

Filter by event and requested time eligibility. Reject obsolete revisions, retracted claims, unavailable bytes, and incompatible index versions before returning playable hits. Merge overlapping hits only where the same action is established. Recheck availability when resolving a hit; search similarity is not factual confirmation.

The local adapter can use a small deterministic labeled retrieval fixture. Label its ranking as simulated; it does not prove semantic quality. Exercise the real ledger, filtering, invalidation, and media resolver around those fixture results. Provide an internal query/inspection command for this phase. The user-facing search flow and replay playback remain in PRD 3.

## Provider adapter boundaries

An adapter converts between a verified external service and Breadcast's internal contracts. It must not own event truth or program control. The following are application capabilities, not claimed provider API routes or SDK method names.

| Boundary | Application request and result | Local proof | Later connection proof |
|---|---|---|---|
| Storage | Immutable media/manifest references; put, inspect, read, and policy-controlled delete | Filesystem adapter with real bytes and injected partial/missing/corrupt objects | Upload/read/hash check from the actual execution environment |
| Job dispatch | Ready manifest reference, logical job key, configuration revision, deadline; accepted/completed/failed result | In-process event delivery through the same worker entry, with duplicate/missed/delayed events | Narrowly filtered DataEngine trigger invokes the packaged worker; outputs do not retrigger input analysis |
| YOLO | Source-linked frame/window references and tracker context; validated detections/tracks | Labeled detections for known intervals and malformed/late responses | Real request; classes, sampling, geometry, and tracker ownership verified |
| Cosmos | Readable bounded clip/window, timestamps, vocabulary; observations with supporting intervals | Labeled observed/inferred results and conflicts | Real reasoning request with supported preprocessing and response format |
| Search | Versioned index entries and bounded query/filter; source-linked hits and freshness | Controlled ranking plus real filtering and media resolution | Real indexing/query, correction, and playable retrieval |
| W&B LLM | Role, bounded context, expected output schema, snapshot, deadline; validated result or failure | Structured fixture response with malformed/timeout cases; no airtime side effects | Actual supplied model ID and structured-output behavior tested with one bounded request |
| Speech | Text, configured language/voice/pronunciation preferences, deadline; decodable audio reference, format, duration, provenance | Real prerecorded audio through the result validator, explicitly marked fixture | Actual synthesis with supported voice controls and measured duration; Riva is a candidate |

The worker core takes a Breadcast manifest reference. A fixture notification calls it locally; a verified VAST event adapter resolves the provider payload into that same input. In provider mode, DataEngine owns dispatch for that flow. Do not let both a local scheduler and DataEngine independently submit the same logical analysis work. Keep deduplication at the receiving boundary regardless.

The coordinator remains the writer of active event state. Remote workers publish validated result artifacts. Import those through a bounded result reader or a verified authenticated notification path, using the same ingestion function as local fixtures. Do not require DataEngine to access the local SQLite file. Select the supported delivery method during provider verification and keep it inside the adapter.

Package worker dependencies reproducibly and run the generic container entry locally. Do not invent a VAST handler signature, deployment file, signed-URL API, GPU allocation, or SDK version. Add vendor-specific code only after checking actual access and its contract. Hosted model access to private media must be tested from the service's real execution context.

For W&B and speech, this phase delivers adapter contracts, fixture implementations, capability probes, and verified live calls when access exists. It does not deliver director policies, joke generation, speech mixing, or proof of entertaining delivery. Those remain required in PRD 2.

## Configuration and operating limits

Provider selection must be explicit per boundary. Fixtures and real providers use the same application services. Mixed configurations keep provenance for every result; they never count as fully live verification. With no provider configuration, the current manual Studio still starts, and analysis remains inactive until explicitly configured. Selecting live mode with missing credentials or unsupported capabilities reports the reason; it never silently switches to fixtures or starts a rehearsal.

Provide a checked configuration template with no secrets. Include provider endpoints, secret references, returned model IDs, versions, supported media/voice formats, storage locations, trigger filters, worker resources, deadlines, concurrency, retention, and context limits. Use no guessed working endpoint or default model ID. Keep secrets and signed URLs out of persisted prompts, event records, audience pages, and diagnostic exports.

The following are initial local validation defaults, not provider capacity or performance claims. Keep them configurable and record the exact values in evidence.

| Limit | Starting value or rule | Exhaustion behavior |
|---|---|---|
| Analysis window / step | 6 seconds / 2 seconds; maximum window 12 seconds | Use supported finalized coverage only; record gaps and unavailable windows |
| Analysis concurrency | 2 active window jobs overall; at most 1 per source | Retain only the newest pending live window per source; record skipped intervals |
| Provider call timeout | 10 seconds, further limited by the job's remaining deadline | Cancel/ignore late result for live use; record supported archive evidence separately |
| Live analysis deadline | 8 seconds after the last included frame's local receipt; persist the first deadline | Chunk closure, queueing, and retries consume the same budget; receipt time is not a capture-time claim |
| Retry count | At most 2 retries, with 1-second then 2-second waits, only within the original deadline | Permanent/auth/schema errors fail immediately; do not extend expiry |
| Pending storage work | At most 256 chunks and 1 GiB of unuploaded bytes | Stop admitting new archive work; mark lost coverage; never block live encoding |
| Local retained archive | At most 30 minutes and 4 GiB, whichever is reached first | Evict oldest eligible unpinned media; reflect availability in search |
| Disk reserve | Keep at least 2 GiB free on the runtime volume | Stop new artifact work and report degraded analysis before consuming reserve |
| Context per request | At most 60 recent seconds, 32 scenes/observations total, 20 aired utterances, 5 archive hits, and 64 KiB serialized context | Keep required facts and references; return truncation/unknown status when a valid slice cannot fit |
| Search results | At most 10 candidates per query | Reject unbounded requests; filter before exposing playable hits |

Pending storage bytes count toward the archive byte cap. Existing live source-buffer and replay-worker limits remain unchanged. Pins have a bounded job owner/deadline and release on success, failure, cancellation, or shutdown. Never evict active pins to make a test pass. Bound database/index/log growth as well as media files; persist replay-safe operation keys for their supported retry lifetime. Do not discard a key while a duplicate can still cause a side effect.

The live deadline is a fixture-test policy, not a promise that AI will fit the program delay. Reject windows already too old instead of starting their clock at upload completion. Measure the actual closure and inference path before tuning. Do not add a new low-latency service if the deadline fails; document the result and test smaller supported windows through the same boundaries first.

## Failures and diagnostics

| Condition | Required response |
|---|---|
| Duplicate or reordered event | Reuse the logical result; preserve current revisions; acknowledge without a second effect |
| Model timeout or invalid output | Record failure/expiry; keep program and human controls responsive |
| Missing/corrupt chunk or mapping | Reject dependent analysis and interval resolution; expose the affected coverage |
| VAST upload/trigger failure | Retry within limits; recover missed finalized work; report analysis lag separately from media health |
| Evidence retraction or context change | Invalidate dependent context/search and notify consumers without rewriting historical output |
| Source disconnect or slot reuse | Close the old epoch; keep old immutable archive identity; never inherit live eligibility |
| Storage pressure | Apply the declared bounded policy; no silent loss marked ready and no capture stall |
| Application restart | Start a new run in holding; reject prior-run live work; retain stored artifacts only under their original ownership |
| Required provider unavailable | Keep manual broadcast usable; show capability failure; leave live verification open |

Use existing diagnostics rather than a new dashboard or connection toggle. Report source health, analysis/index progress, gaps, provider capability state, active/pending jobs, storage usage, and the last concrete failure. Record trace/job IDs and stage times from finalization through context availability. Record event/source intervals separately from processing clocks. Redact secrets.

## Acceptance criteria

These checks are required evidence, not a statement that the features exist. Run state-changing tests in an isolated instance. Use deterministic fixtures for failure paths and real encoded media for timing, geometry, retention, and playback claims.

| ID | Coverage | Scenario and pass condition |
|---|---|---|
| E01 | S01 | Load/edit context and confirm an official fact. Revisions advance once; stale edits fail; unknown values stay unknown; model/fixture attempts to confirm facts fail; current controls use the active revision. |
| E02 | S02 | Record and store a real closed chunk. Bytes fully decode, hash and format match, manifest appears only after readiness, and an identical retry creates no extra logical job. Partial or altered objects fail. |
| E03 | S03 | Marker footage maps original input to normalized frames and actual program output. Report observed error and uncertainty in source time-base ticks/output frames. Geometry checks cover orientation and padding. No calibrated claim passes outside its valid interval. |
| E04 | S03 | Reconnect, timestamp reset, source loss, and camera-slot reuse cannot inherit old mappings or identities. Source-local analysis works with unknown event mapping; synchronized claims remain blocked. |
| E05 | S02/S04 | An action spanning at least two chunks produces developing observations before any replay exists. Overlapping windows update one logical scene when evidence matches; two distinct adjacent actions remain distinct. |
| E06 | S02/S04 | Deliver duplicate, older, conflicting, malformed, and late results. No duplicate side effect, scene rollback, fabricated certainty, or renewed live deadline occurs. Other sources continue progressing. |
| E07 | S04 | Change or retract evidence. Context and search stop presenting the old claim; dependent consumers receive an invalidation; original records remain inspectable. Instruction-like visible/retrieved text cannot alter authority. |
| E08 | S04/S07/S08 | Build director, commentator, and segmentor context for known intervals. Future outcomes are excluded from commentary; earlier eligible actions and aired text remain available for callbacks. Pending/canceled text is not recorded as said. Bounds and snapshot revisions are preserved. |
| E09 | S02/S13 | Pin an interval, fill the buffer, disconnect the camera, and reuse its slot. The retained old interval still resolves and decodes correctly. Cleanup cannot race the pin or upload. After permitted deletion, lookup returns unavailable. |
| E10 | S04/S13 | Query labeled records. Event/time filters, model/index version checks, correction handling, freshness, and media resolution pass. Fixture ranking is labeled simulated. Wrong-event, retracted, or deleted hits never become playable. |
| E11 | S17 | Run the same adapter contract suite for local implementations and any available live implementations. Missing capability/auth, unreadable private media, changed hashes, wrong versions, timeout, and invalid response are explicit failures. No silent fixture substitution or credential leakage. |
| E12 | S17 | W&B and speech fixture calls pass their result validators. Structured responses preserve reviewed context. Real test audio decodes with a measured duration and fixture provenance. No call or returned text can change airtime in this phase. |
| E13 | S02/S17 | Lose a completion notification, interrupt a write, and restart the test instance. Recover valid stored work without duplicate results; partial files remain unready; old-run work cannot enter current context or control. |
| E14 | S02/S17 | Inject slow providers, one stalled source, full queues, and the configured disk threshold. Actual limits hold; skipped/lost ranges are recorded; urgent human controls and media remain independent. |
| E15 | Preservation | One sample camera passes first. Then use five sources with background ingestion/failures. Record at least five minutes of decoded program output with no unexpected black frames or encoder restart; measure command application separately from viewer delivery. Retain admission, graphics, replay, browser, and lifecycle regressions affected by changes. |
| E16 | Handoff | One documented command reproduces local checks in a clean environment without provider credentials. Configuration examples, fixture assets, diagnostics, coverage results, and PRD 2/3 interface usage are included. |
| E17 | Preservation/S17 | Framework adoption preserves existing HTTP and browser behavior, request rejection, access restrictions, and startup/shutdown. One runtime starts exactly one media pipeline. Fixture execution blocks external model/transport requests. SDK and application retries obey one measured attempt/deadline budget. |

For E03, document the mapping algorithm and test tolerance before evaluating the output. Base tolerance on the actual frame/time-base precision and recorded uncertainty. Compare decoded markers rather than merely checking that two copies of a computed timestamp agree. The existing calibrated-cut tolerance still applies where relevant.

Local-ready requires E01–E17. Relevant media regressions must pass after any recorder, timing, context, or cleanup changes. A pure schema test cannot substitute for E02, E03, E09, or E15. Physical-phone/venue and real semantic/model quality gates remain separate.

## Live connection checklist

1. Record actual VAST tenant version, storage view/bucket, credentials source, registry access, runtime limits, and permitted worker placement. Record all returned model IDs and speech capabilities.
2. Configure verified adapters. Upload and retrieve one real closed chunk from the intended execution environment. Verify private media transport to each consuming provider.
3. Install the narrowly filtered ready-manifest trigger using the tenant's actual packaging/API. Invoke one real worker. Repeat delivery and prove one logical result. Confirm derived outputs cannot loop into input triggers.
4. Run real YOLO and Cosmos requests. Save source-linked observations and a scene update. Check bounds, model provenance, sampling, and tracking behavior on the actual footage.
5. Index the result, query it, and decode its retained interval. Run the build plan's ten-query search check when a sufficient labeled corpus exists; a single successful query does not prove retrieval quality.
6. Run a bounded W&B structured-output request and a real speech synthesis request through the adapters. Save validated text/audio and actual durations. Commentary style, live scheduling, and entertaining delivery still require PRD 2.
7. Capture closure, upload, trigger/queue, inference, validation, persistence, and index latency. Report sample counts, p50/p95 where meaningful, and deadline misses. Failure and provider limits must remain visible rather than being hidden by fixture results.
8. Re-run adapter contract checks and the connected ingestion/media rehearsal. Keep PRD 1 live status separate from PRD 2/3 completion. Do not change unrelated infrastructure or a running show to obtain evidence.

## Implementation order and deliverables

1. Preserve the current working tree and establish the relevant existing test baseline. Check and pin library compatibility. Adopt Pydantic records and the bounded FastAPI routing change; prove existing controls and media still work. Implement event/source identity and context revisions with authoritative contract updates.
2. Deliver the first one-camera path and `foundation-check` command with real finalized media, local storage, a fixture observation, a stored scene, and a query. Keep incomplete acceptance checks visible in its report; this increment is not local-ready.
3. Complete native/proxy/program timing and geometry proof, retained-media resolution, pins, and one cleanup owner. Add bounded job dispatch, deduplication, and recovery before provider-specific code.
4. Add scene history, corrections, context selection, and search filtering/resolution. Add W&B and speech boundary fixtures without starting live crew execution.
5. Add redacted diagnostics, configuration validation, worker packaging, the local suite command, and the connection checklist. Implement verified external adapters only when access supports them.
6. Run E01–E17 and relevant regressions. Record failures and fixes. Hand the stable application interfaces to PRDs 2 and 3.

Deliver code, focused tests, fixture assets with provenance, configuration templates, run instructions, and one authoritative evidence record under `docs/evidence/`. That record must distinguish local-ready from live-verified; include commands, environment, source/model/config versions, decoded media paths, trace IDs, measured timings, limit tests, failures, and unresolved provider/phone gaps. Do not create a passing evidence record before the checks run.

The PRD 2 handoff must show how to load/update event context, obtain a reviewed bounded context slice, consume corrections, read actual program history, and call LLM/speech adapters. The PRD 3 handoff must show how to query, validate, pin, read, and release archived source intervals after a camera disconnects. Both consume the same records and existing program controller. Neither should need to recreate event memory, timing, or provider transport.
