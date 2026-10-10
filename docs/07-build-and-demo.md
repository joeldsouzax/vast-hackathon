# Hackathon build and demo plan

**Updated:** 2026-10-09

Current user override: keep the build stages and acceptance scenarios, but use
Gemini for AI and Supabase for private clips and pgvector. Use the bundled video
first. Physical camera validation remains deferred. Follow [migration 28](28-gemini-supabase-migration.md)
for the new deployment. Skipped checks remain unverified.

Current work is specified by [PRD 22](22-live-stack-integration-prd.md) and
[parallel plan 23](23-parallel-implementation-plan.md). They replace the older
work assignment and custom-pipeline deployment choices below. Keep the inherited
acceptance scenarios. Historical status statements are not current test passes.

Build one continuous live broadcast with up to five cameras joined through an event QR code. Validate one camera first. Then expand to five with the same media and message contracts. Provider access remains the first check. Hackathon length and team size are still unknown.

Use the [three remaining integration tasks](#three-remaining-integration-tasks) below for the next phase. The earlier [stack review and work split](10-stack-review-and-work-split.md) records preparation boundaries; its packages are not a second backlog.

The [browser media experiment](12-studio.md) implements local capture, admission, recording, buffered output, and manual replays in one Docker container. [Its validation record](11-media-experiment-validation.md) distinguishes sample and browser-fixture checks from the required physical-phone rehearsal.

## Scope

| Required | Defer until the core works |
|---|---|
| Event setup and prebuilt graphics | Generative artwork and elaborate animated transitions |
| QR camera joining, server-enforced five-slot limit | Public participant accounts and administration for multiple tenants |
| Five-feed health/preview view and primary feed | Automatic cross-camera identity and precision sports analytics |
| Continuous program output and return-to-live | Broadcast distribution at internet scale |
| YOLO observations and Cosmos scene reasoning | Model training or fine-tuning |
| W&B LLM director and witty spoken commentary through verified TTS | Multiple commentators, voice cloning, and custom voice training |
| Live camera, microphone/mute, prepared graphics, and bounded static crop/zoom control | Automatic moving crops and camera pan/tilt control |
| VAST chunks, triggers, persisted results | Large infrastructure deployment beyond provided services |
| Replay with speed presets, static crops, calibrated multi-camera hard cuts, and explicit alternate-angle repeats | Moving crops, dissolves, split screens, and advanced transitions |
| Semantic search over captured moments | Demonstrated hours-scale performance without a large test corpus |

## Build order and exit criteria

Complete each stage only when its exit evidence passes. Local work can proceed before provider access using the milestones below. Once camera playback, setup, and the evidence contract are ready, live direction and replay production can proceed independently. Live camera decisions must not wait for replay completion. Combine these paths for search and final rehearsal.

| Stage | Build | Exit evidence |
|---|---|---|
| 0. Confirm access | Provider adapters, endpoint/version record, camera network plan | One real response from each required service, or an explicit unresolved blocker |
| 1. One-camera playback | One phone through gateway, buffer, compositor, viewer | Five minutes continuous playback; visible delay measured with a clock/clap |
| 2. Five-camera joining | QR page, atomic leases, publisher authorization, reconnect handling | Five active feeds; concurrent sixth join rejected; released slot reusable |
| 3. Setup and graphics | Context editor, templates, package validation, preloading | Ready screen plus opening, overlay, holding, and closing graphics |
| 4. Ingestion and evidence | Finalized chunks to VAST, trigger, YOLO/Cosmos adapters, scene ledger | Live action produces source-linked observation and deduplicated scene |
| 5. Replay production | Plan compiler, retained buffer chunks, media render, output checks | Real action appears in a validated replay with speed/crop changes |
| 6. Direction and commentary | W&B live proposals, controller validation, bounded context, speech and caption scheduling | Live camera/audio/graphics/framing actions and contextual spoken commentary pass Task 2; replay scheduling and return pass Task 3 |
| 7. Semantic recall | Index metadata/embeddings or supplied search integration | Natural-language query returns and plays a previously captured moment |
| 8. Rehearsal | Failure injection, five-feed load, trace capture | All required acceptance scenarios below recorded |

The local implementation uses Python for coordination, MediaMTX for transport, and a persistent FFmpeg encoder with application composition. Keep this working media path. Provider integration does not require a new compositor.

Keep compositor control behind one small application adapter. [The architecture](02-architecture.md) defines the media process and alternative compositor choice.

## Three remaining integration tasks

Status: Task 1 is local-ready; its live verification remains open. Task 2 has core application paths; full local acceptance remains open. Task 3 has core replay/search paths; its full local, live, and device gates remain open. Local media, controls, graphics, and replay rendering provide the base. Existing fixture evidence does not prove real provider access, autonomous editorial decisions, physical-phone behavior, or audience capacity. These tasks group the remaining stages above; they do not add services or separate agent deployments.

Task 1 has an [implementation PRD](17-event-understanding-and-provider-adapters-prd.md) and [local handoff](18-event-foundation.md). Task 2 has a [live direction and spoken commentary PRD](19-live-direction-and-commentary-prd.md). Task 3 has a [timely replays and broadcast validation PRD](20-timely-replays-and-broadcast-validation-prd.md) and [implemented handoff](21-timely-replays.md); full acceptance remains open. The user's delivery goal is to finish application behavior before the hackathon so event work focuses on connecting the supplied stack, tuning, and testing. Missing provider access must not block work that can be proved locally.

Use established libraries to reduce custom work. Task 1 specifies Pydantic, Pydantic AI, FastAPI, verified provider SDKs, and a single local check command. Preserve the existing browser pages and media pipeline. Later tasks reuse these choices and shared services; do not add another framework or service without a demonstrated need.

Each task has two milestones: **local-ready before the hackathon** and **live-verified with the supplied stack**. Build application behavior now against Breadcast's own contracts. Use clearly labeled, timestamped fixtures and injectable provider responses, including failure responses. Keep real media capture, encoding, replay rendering, and browser playback in these tests. A scripted fixture can prove control behavior but cannot prove that AI recognized or chose the correct action.

Provider calls belong behind small adapters in the existing coordinator/jobs. Adapters translate verified external responses into authoritative Breadcast records. Fixture and provider adapters must pass the same application contract checks. Do not guess SDK methods, event payloads, model IDs, or deployment configuration. Provider mode must fail visibly when access is missing; it must never silently substitute fixture output. No extra service is needed to simulate the contract boundary.

Every PRD must specify the adapter boundary, configuration, local scenarios, completion evidence, and live connection checklist. Prepare templates without secrets, startup capability checks, and repeatable validation commands. The intended handoff changes adapters and configuration, not program ownership, event records, UI behavior, or the media path. Actual API differences, model quality, quotas, networking, or execution limits may still require adapter changes; connection-only work is a goal, not a guarantee. Physical-phone and multi-viewer checks can run before the hackathon on an available trusted network, then repeat at the venue.

### Scope guard and PRD handoff

This is one event, up to five cameras, one shared program, and a tested small browser audience. Preserve the existing Studio, Broadcast, and Join pages and persistent encoder. Keep the current tested output profile as the baseline; higher resolution/frame rate requires separate measurements, not a new default requirement.

The [coverage audit](#studio-coverage-audit) assigns each feature to one task. Each PRD must cite its coverage IDs, state what code it reuses, define remaining changes, and give observable pass conditions. Specify configurable job/queue/storage limits, decision deadlines, and behavior when those limits are reached. Task 1 establishes shared records and adapter contracts before Tasks 2 and 3 extend them. Do not create separate stores of official facts, event memory, action history, or media identity in each PRD. Resolve required source-time and archive boundaries locally; do not leave application redesign to the hackathon.

Required additions are event-specific setup, provider/evidence adapters, live crew decisions, bounded live framing, witty contextual spoken commentary, caption fallback, semantic recall, and their validation. Speech generation, scheduling, mixing, and interruption checks are required. Captions preserve playback during failure; they do not complete the commentary scope. Keep one designated live microphone; a full mixing desk, automatic speech transcription, voice cloning, and microphone separation are not required.

Defer unrestricted natural-language control, automatic identity or official score recognition, moving crops/PTZ, new replay effects, timeline editing, multiview/picture-in-picture, remote camera talkback, additional graphics generation, multiple events, accounts, public access controls, CDN/HLS scaling, social-platform simulcasting, and broadcast failover. These do not become requirements merely because commercial studios offer them. Natural-language moment search remains required; expose it through one small existing Studio surface. Existing direct commands and human controls remain available.

Use labeled fixture adapters through the same scheduler, validators, state store, and media worker as live adapters. Provider selection is configuration, not a second application or a product mode toggle. A missing optional provider uses its declared fallback. A missing required provider leaves live verification incomplete. A local-ready report must include real-media checks, not only mocked calls or screenshots.

### Task 1 — Build event understanding and provider adapters

Implementation specification: [Event understanding and provider adapters PRD](17-event-understanding-and-provider-adapters-prd.md). Historical status: local-ready; live verification remains open. See the [evidence record](evidence/event-foundation.json) and [handoff](18-event-foundation.md).

Outcome: fresh camera evidence updates one shared event history that the director, commentator, segmentor, and search can use.

Scope:

- Verify actual VAST, YOLO, Cosmos, search, W&B/CoreWeave, and speech-provider access. Record returned model IDs, versions, formats, limits, and real requests before depending on provider APIs.
- Upload finalized chunks, verify their bytes, and publish ready manifests last. Use narrowly filtered VAST DataEngine triggers to run bounded analysis functions. Recover missed uploads and handle repeated or out-of-order results without duplicate effects. Keep upload retention bounded and report unavailable media.
- Analyze configurable overlapping windows. Publish useful observations while an action is still developing; do not wait for a closed scene or replay. Preserve per-source tracker state, timestamps, mapping uncertainty, and analysis progress.
- Maintain the existing evidence ledger and scene revisions. Keep observed actions, inferred outcomes, and official facts distinct. Preserve unresolved actions, conflicting evidence, corrections, and source references. Reuse these records for bounded role context and semantic indexing.
- Implement the single-event `EventContext`/`EventState` boundary: supplied title/profile/participants, official-fact authority, audio/editorial policy, revision changes, and run identity. Preserve explicit human confirmation for official facts. Task 2 adds setup controls and graphics binding against these same records.
- Resolve original recording timestamps, normalized analysis/replay timestamps, and image geometry through versioned mappings. The current buffer contains proxy PTS, not original capture timestamps. Preserve transforms and uncertainty rather than assigning guessed event times. Include source-to-program mapping in the handoff to Task 2.
- Resolve the retained-media contract with Task 3. Immutable chunk/asset references must still identify the old camera after a slot is reused. Define bounded upload/pinning/deletion rules and archive availability. Verify how hosted models read private media; endpoint access alone is insufficient. Local adapters exercise unreadable, expired, and missing object references without exposing credentials in prompts or logs.
- Measure actual chunk closure, upload, trigger queue, inference, and evidence-availability times. Determine the supported live observation cadence from these measurements. If it cannot meet Task 2 deadlines, record the blocker and evaluate a bounded shorter-window/frame path using verified provider capabilities and the same evidence contracts.

Local-ready milestone:

- Implement record validation, chunk/manifest construction, bounded upload/retry state, analysis window selection, result normalization, scene merging, corrections, and role context assembly. Exercise these through an in-process storage/trigger/model test boundary with real recorded chunks and labeled observations. Keep external provider payload parsing separate until verified.
- Replay delayed, repeated, missing, conflicting, and out-of-order responses. Show analysis progress and source-linked searchable fixture results. Supply commands and expected outcomes for these scenarios; report them as local validation only.

Live-verified completion checks:

- One real camera action spanning chunks produces source-linked YOLO/Cosmos observations and one updated logical scene in durable storage. Developing observations arrive before a final replay is available.
- Duplicate delivery, older results, model timeout, and VAST failure preserve valid state and continuous playback. Source loss cannot block analysis of other cameras.
- A query returns the correct captured interval, evidence, and playable media. Search freshness is visible. Record actual provider traces and stage timings.

Start here. Check available access early; missing access does not block the local-ready milestone. Follow the [stack integration workflow](../skills/breadcast-stack-integration/SKILL.md) and [VAST integration design](03-stack-integration.md). Use the existing [context contracts](05-context-and-contracts.md); do not create a second event memory or message specification.

### Task 2 — Direct and commentate the live program

Implementation specification: [Live direction and spoken commentary PRD](19-live-direction-and-commentary-prd.md). Status: core application paths implemented on 2026-10-07; full local acceptance and live verification remain open. The [evidence record](evidence/live-direction.json) records the checks and Docker access blocker. The PRD includes the local live-receipt mapping work needed to consume Task 1 evidence safely.

Outcome: the AI crew manages the current show automatically and describes the moment viewers are seeing, with human takeover always available.

Scope:

- Run bounded director and commentator calls through the existing coordinator using W&B-hosted application LLMs. Feed the director recent observations, camera health, audio evidence or configured policy, event facts, and actual program state. Camera decisions must not depend on replay planning or rendering.
- Propose live camera selection, designated microphone and mute changes, prepared graphics, and bounded static live crop/zoom. Reuse supported controller operations. Live crop/zoom is new work: define its typed contract in the authority document, bind it to the source lease/epoch, validate oriented-frame bounds and output aspect ratio, support full-frame reset, and implement it in the persistent compositor. Do not claim the existing replay crop is a live control.
- Apply fresh, valid proposals automatically through `Coordinator.propose(request, actor="Provider crew")`. Only the program controller commits airtime. Preserve synchronization checks, stable action IDs, expiry, human priority, and actual media receipts.
- Capture the decision's state and source/evidence revisions before starting a model call. Carry that snapshot through response validation and commit. Do not attach a newly captured revision to old model intent. Rejected stale decisions require new context and a new decision, not an automatic retry with fresh revisions.
- Complete event setup in existing controls using Task 1 records. Prepare opening, overlay, lower third, replay, holding, and closing assets from the existing package. Validate supplied text, unknown facts, long/non-ASCII names, fonts, dimensions, context revision, and preload readiness. Keep neutral holding available before readiness; setup alone never starts the program or publishes a broadcast. Preload and validate a new package before switching its revision.
- Preserve manual camera/audio/graphics/replay controls, local commands, policy settings, previews, cancellation, takeover/release, and server-owned activity across browser reloads. Add a direct full-frame reset for live crop. Join-code rotation, camera removal, official updates, and ending the event remain human operations. Closing graphics are separate from the confirmed shutdown action.
- Build commentator context from the authoritative records described in [audience-aligned context](05-context-and-contracts.md#audience-aligned-context-planned-integration). Include developing action, relevant earlier events, actual aired commentary, and the accepted picture's source interval. Prevent early outcomes, repeated narration, and claims unsupported by evidence.
- Implement the [commentary personality and delivery](04-agent-instructions.md#commentary-personality-and-delivery): spicy, funny, witty, and playful, with contextual callbacks, varied jokes, expressive delivery, and deliberate pauses. Preserve event facts and use one voice. Include its small listening review in the PRD; a technically audible but flat or repetitive narrator is not sufficient.
- Make spoken commentary a core output, using a verified TTS adapter with an expressive voice. Riva is a candidate, not a confirmed integration. Include timed caption fallback in the encoded program, with defined graphic priority, legibility, expiry, and clearing. Missing speech preserves playback but leaves live verification incomplete. Audio content judgments require actual audio evidence or a verified speech-recognition service; TTS does not analyze microphone audio.
- Apply the [program audio rules](05-context-and-contracts.md#program-audio-and-caption-policy-planned-integration) for source selection, loss, graphics, replay, and speech. Confirm actual audio/video timing. A browser's local mute must never mute the shared broadcast.
- Measure observation-to-applied-action and evidence-to-caption/voice timing separately from viewer delay. Select the configured broadcast delay during setup from measured results. The current 3-second local delay and proposed 8-second design delay are not measured end-to-end guarantees. Expire late cues instead of changing delay during the show.

Local-ready milestone:

- Implement the director/commentator scheduling loop, bounded context, prompts, response schemas, controller translation, setup, live framing, speech playback/mixing, and caption fallback. Drive real output with labeled scripted responses, not model-generated shell commands. Test speech scheduling, measured duration, mixing, interruption, and level restoration with real test audio. This proves the local audio path, not live TTS integration or generated voice quality.
- Exercise the live action and contextual commentary checks below with fixture evidence, including changing pictures, delayed outcomes, repeated narration, corrections, takeover, and unavailable audio analysis. Inspect actual encoded output and capture traces. Record editorial quality as unverified until real models run.

Live-verified completion checks:

- With real provider output and no ready replay, the crew selects an eligible camera, changes microphone/mute, binds a prepared graphic, and applies/resets a valid live crop. Verify decoded video and audio, not only action acknowledgements.
- Invalid crop, stale observation, slot reuse, model timeout, and human takeover cannot apply obsolete actions or interrupt media. Unsupported audio claims cause abstention or the configured fallback.
- An action developing across windows produces coherent commentary without announcing its outcome before the matching program interval. Earlier context remains useful; repeated windows do not repeat aired text. A correction or cue change cancels or regenerates affected pending commentary.
- Real W&B-generated commentary and the verified speech provider pass the listening review for action description, humor, callbacks, quiet intervals, and replay. Save generated audio and human review notes alongside timing evidence. Caption-only output or prerecorded fixture jokes cannot pass this live gate.
- Record actual latency distributions, deadline misses, rejected proposals, and source-to-program mappings. A healthy stream alone does not prove useful live AI response time.

Local work depends on Task 1's contract boundary, not provider access. Live verification depends on actual evidence and model access. Fixtures do not close the live-verified milestone. Follow the existing [runtime role instructions](04-agent-instructions.md).

### Task 3 — Deliver timely replays and prove the complete broadcast

Implementation specification: [Timely replays and broadcast validation PRD](20-timely-replays-and-broadcast-validation-prd.md). Status: core application paths and `replay-check` implemented; full local/live/device acceptance remains open. See the [handoff](21-timely-replays.md) and [evidence](evidence/timely-replays.json). The PRD records existing code, library choices, archive compatibility, deadlines, resource limits, and local/live/device gates. It also preserves the unfinished Task 2 acceptance conditions.

Outcome: evidence-backed replays and recall fit the live show, and real phones feed continuous playback to several viewers.

Scope:

- Connect the segmentor to developing scenes and semantic search. Select lead-in, action, aftermath, angles, speed, and static crops. Pin retained media across chunk boundaries and reuse the existing validated replay worker. Verify DataEngine execution limits before moving rendering there; a function may submit to the existing worker.
- Support recall from retained archived media after the local rolling buffer expires. Resolve immutable source identity and evidence through Task 1 records; do not silently substitute the new occupant of a camera slot. Use the [archive playback boundary](05-context-and-contracts.md#retained-archive-playback-planned-integration). Preview/search results do not grant airtime; playback still needs a separately valid proposal.
- Prepare replays while live direction continues. The director schedules only ready, still-useful assets. It skips a missed opportunity and retains eligible media for later recall. Action duration, required aftermath, and remaining processing time are separate measurements.
- Recheck commentary against the replay's output-to-source map, including slow motion and repeated angles. Label historical playback, cancel replay narration on interruption, and resume current delayed live with refreshed context. Capture and analysis continue throughout.
- Prove one physical phone first, then five-camera QR admission, a concurrent sixth rejection, disconnect/reconnect, synchronization, and released-slot reuse. Use a reachable HTTPS/media setup and test one camera with at least three simultaneous viewer devices before repeating under five-camera load. The camera cap does not limit viewers.
- Run the acceptance scenarios below, including at least five minutes of continuous program output and the 15-minute five-feed load test. Record per-viewer delay, stalls/disconnects, outgoing bandwidth, CPU, queue/disk growth, and failures. This establishes the tested audience size, not internet-scale delivery.
- Preserve failure and lifecycle behavior: unhealthy inputs fall back to eligible live media or holding; unavailable audio becomes silence; provider errors leave media running. After a process restart, start a new run in holding and reject old airtime work. Detect/report encoder or gateway failure and verify explicit restart recovery; seamless failover is deferred. Test confirmed shutdown and save the demo output/traces before cleanup.
- Bound model concurrency, pending actions, rendering, retries, source buffers, and archive storage. Under load, drop expired decision work and reduce analysis before compromising media. Show source/media health and analysis freshness separately in existing diagnostics, with one useful operator-facing failure reason. Keep provider configuration and secrets out of the audience pages.

Local-ready milestone:

- Connect scripted scene updates and segmentor/director responses to the real replay worker, scheduler, commentary lifecycle, and search/playback path. Use real sample recordings to prove cross-chunk edits, slow motion, ready-only scheduling, deadline skips, interruption, and return to live.
- Prepare a repeatable rehearsal and one command to run the local acceptance suite. Complete available physical-phone/multi-viewer checks independently of providers. Include a connection checklist for credentials, capability probes, one real chunk trigger, one real live proposal, one spoken commentary cue, replay/recall, and venue playback.

Live-verified completion checks:

- A real action spanning chunks becomes a decoded, validated replay, airs once at a useful opportunity, and returns to current delayed live. An urgent live action or human command interrupts it and cancels related narration.
- Record action end, required aftermath end, media finalization, evidence/plan readiness, render start/end, controller application, and viewer-visible playback. Report end-to-end distributions and missed opportunities as well as render time. The existing 15-second replay-readiness target alone does not prove a replay is timely for the show.
- Late rendering, provider failure, and replay cancellation leave the live program running. Natural-language recall plays an earlier retained moment with correct historical context.
- Complete physical-phone and simultaneous-viewer checks with real program audio, including generated live and replay commentary. If speech is unavailable, demonstrate the failure fallback and record the access gap; the spoken-commentary gate remains open.

Depends on Task 1 evidence and Task 2 scheduling/context. Local fixtures allow replay work before provider access; phone/viewer validation can start earlier. Follow the [replay workflow](../skills/breadcast-replay-production/SKILL.md). Store detailed results once in a next-phase evidence record under `docs/evidence/` and link it here. No new broadcast publication or unrelated infrastructure change is authorized by this plan.

## Studio coverage audit

Historically audited against the design docs, the local Studio PRD, application code, and recorded validation. The findings below retain that audit baseline; they are not a new runtime test or provider validation. At that audit baseline, [PRD 1](17-event-understanding-and-provider-adapters-prd.md) is local-ready, [PRD 2](19-live-direction-and-commentary-prd.md) has core application paths with full acceptance still open, and [PRD 3](20-timely-replays-and-broadcast-validation-prd.md) has core replay/search paths with full acceptance still open. The PRDs must cover the IDs below and retain the existing acceptance scenarios and [local Studio checks](16-autonomous-studio-prd.md#acceptance-criteria). Each row has one primary task; references identify its supporting contracts or audit findings.

| ID | Required capability and audit finding | Primary task | Required evidence in that PRD |
|---|---|---|---|
| S01 | Event identity, profile, participants, authoritative facts, and revisions. Local control currently pins context to `1`; a production event record remains work. [Contract](05-context-and-contracts.md#four-kinds-of-context), [code](../app/control.py) | 1 | Context edit invalidates affected proposals; unknown identity/score stays unknown; only human confirmation updates official facts |
| S02 | Finalized chunks, VAST triggers, private-media access, YOLO/Cosmos, and safe result ingestion. Providers are disconnected. [Integration](03-stack-integration.md) | 1 | Real bytes locally; duplicate/out-of-order/timeout fixtures; then one real trigger and source-linked result per required provider |
| S03 | Original/proxy/event/program timing and geometry. The current decoder normalizes media and uses receipt-time origin. [Code](../app/media.py), [boundary](05-context-and-contracts.md#local-replayplan-11) | 1 | Known marker maps through analysis, crop, replay, and program output; unknown/expired mapping blocks claims or seamless cuts; no guessed capture time |
| S04 | Developing scenes, memory, corrections, uncertainty, and search index freshness | 1 | Multi-window action retains one logical identity; retraction reaches pending work and search; stale/conflicting evidence cannot become an official fact; visible text or retrieval content cannot supply instructions or grant authority |
| S05 | Event-specific graphics and setup readiness. Neutral 24-design package exists without event-context binding. [Package](13-graphics-package.md#preparation-and-ownership) | 2 | Preview is off air; required package preloads; long/unknown fields fit; old package remains valid if replacement fails; opening/holding/closing work |
| S06 | Live director: stable camera selection, audio/mute, graphics, hold/return, bounded static zoom. Live crop is new. [Controller](../app/control.py) | 2 | Fixture and real-provider actions use the same gate; decoded media proves all actions; crop resets on source change/loss and never crops overlays; uncertain framing keeps full view |
| S07 | Authority and asynchronous freshness. Capture decision context before inference. [Rules](05-context-and-contracts.md#decision-snapshots-planned-integration) | 2 | Takeover/release, source reuse, evidence/context/policy change during inference all reject old intent; duplicate retry acts once; forbidden human operations stay forbidden |
| S08 | Witty, expressive spoken commentary follows audience time and shared event history | 2 | Review real generated text and audio for humor, relevance, delivery, and pacing; no early outcome, repeated joke, obsolete identity, or overlapping utterance; cue change/retraction cancels pending output; replay interruption cancels narration; actual airtime is logged |
| S09 | Program audio and caption/graphics interaction. One microphone and replay silence exist; narration mixing is not implemented | 2 | Actual output verifies microphone/mute/loss, live/replay source silence during replay, full-screen suppression, restoration, local-monitor isolation, caption legibility; required speech passes duration/mix/cancellation checks; captions-only output cannot close the gate |
| S10 | Human controls, crew cards, manual replay/graphics preview, prepare-only/cancel, official input, and keyboard/phone UI already exist | 2 | Preserve UX1–UX4 and C1–C8; new controls use the same actions; no page redesign, forced approval mode, or provider toggle; public model payload cannot set trusted actor |
| S11 | Evidence-backed replay plans and validated edits exist locally; hosted selection is missing | 3 | Complete cross-chunk coverage, valid cut/repeat mappings, speed/crop/duration checks, persistent REPLAY label, historical score policy, and explicit source/audio rules |
| S12 | Timely scheduling, live monitoring during replay, cancellation, and return to current delayed live | 3 | Slow jobs never block a live decision or human return; miss deadline and skip; urgent action interrupts; clip readiness and editorial usefulness measured separately |
| S13 | Semantic recall needs retained-media playback, not only current camera buffers. Existing replay eligibility depends on the active source. [Code](../app/control.py), [archive contract](05-context-and-contracts.md#retained-archive-playback-planned-integration) | 3 | Query and play an older retained moment after buffer eviction and source disconnect/reuse; retracted/deleted media cannot air and has a precise unavailable reason |
| S14 | QR joining and five-camera enforcement exist with local/fake-camera evidence. [Camera requirements](09-camera-joining.md) | 3 | One physical phone first, then five; last-slot race, direct unauthorized sixth publish, ignored/denied permission, QR expiry/rotation, stop/remove, lock/background/rotation/network loss, safe reconnect and on-air indication |
| S15 | Multiple viewers share one WebRTC program; remote device capacity remains unverified. [Hosting](12-studio.md#host-with-docker) | 3 | At least three viewer devices see advancing video and correct audio; reload/reconnect and local mute affect only that viewer; measure delay and bandwidth on the intended host/network |
| S16 | Holding, end, restart, source loss, model failure, overload, and retention need integrated validation | 3 | Run five-minute continuity and 15-minute five-feed checks; bound queues/disk; detect encoder/gateway failure; restart in holding with no old airtime; confirmed end stops processes; retain demo evidence |
| S17 | Provider drop-in handoff must cover capabilities as well as endpoint strings | 1 | Shared contract suite for fixture/live adapters; explicit provider configuration, private-object transport, packaging/runtime limits, timeout/retry and version checks; unsupported features fail visibly; no credentials in event/model context |
| S18 | End-to-end connection rehearsal and measured limits | 3 | Reproducible local command plus live checklist; recording/traces with model/config versions; report latency samples, p95, misses and failures; preserve search top-3 and existing timing targets without claiming fixture AI quality |

The audit found missing ownership for setup (S01/S05), native/proxy timing (S03), pre-inference freshness (S07), audio/caption composition (S09), and archive recall (S13). Those are assigned above. The user subsequently made witty spoken commentary required. The scope guard reflects that change and still excludes extra studio products. The audit itself changed no code. Current implementation status is recorded in the task sections and linked evidence above.

## Acceptance scenarios

| Scenario | Pass condition |
|---|---|
| Five phones join | Each gets a distinct source/lease; previews and media health visible |
| Two people race for the last slot | Exactly one gets it; active plus reserved leases never exceed five |
| Sixth camera tries direct publishing | Gateway rejects unauthorized publisher even if UI is bypassed |
| Permission denied or ignored | Clear phone UI; reservation expires; no permanent slot leak |
| Phone locks or loses network | Feed marked unhealthy; program uses another eligible source or holding; reconnect cannot collide with new owner |
| Unsynchronized angle | Preview allowed; seamless cut blocked until mapping quality passes |
| Duplicate/out-of-order VAST results | One logical scene/update; one replay command; no rollback to old revision |
| Model timeout/invalid output | Broadcast continues, stale actions expire, reason recorded |
| Shot with uncertain outcome | Commentary describes action; score remains unchanged |
| Witty spoken commentary | Real generated voice passes the linked listening review; jokes fit observed events, callbacks use history, pauses leave room for event sound, and captions alone do not pass |
| Action spans chunks | Replay contains build-up, action, aftermath without missing interval |
| Slow/fast/crop replay | Measured timing matches plan; subject remains in frame; REPLAY shown |
| Urgent action during replay | Return-to-live command succeeds and replay narration is canceled |
| TTS missing/late | Caption or ambient-only fallback works; no stale speech; required speech acceptance stays incomplete |
| Unknown score / long name | Fields hidden or fitted; no sample data accidentally aired |
| Search query | Top results point to correct event/time, source evidence, and playable media |
| Five-feed sustained run | Queue and disk use stay within configured limits; capture and output remain healthy |

## Proposed measurements

| Measurement | Initial acceptance target |
|---|---|
| Program continuity | No unexpected black frames or process restart during a 5-minute rehearsal |
| Camera cap | Never over five occupied leases; zero accepted unauthorized publishers |
| Return-to-live command | Applied within 1 second in local controlled rehearsal |
| Replay readiness | Within 15 seconds after required aftermath is finalized, at p95 |
| Search usefulness | For 10 labeled queries, correct available moment in top 3 for at least 8 |
| Grounding | Every aired factual claim resolves to evidence or authoritative context |
| Speech/video alignment | Within 500 ms for spoken commentary; measure using program timestamps |

These targets are engineering goals. They are not measured results or provider guarantees. For p95 targets, report the latency at or below which 95% of measured results fall. Record end-to-end latency, including player buffering.

Before the five-camera demo, run a load test for at least 15 minutes. Check phone heating, Wi-Fi changes, clock drift, and buffer growth.

## Three-minute demo script

1. **Prepare:** show event context and the generated graphics package. Display the camera-join QR.
2. **Join:** connect cameras, show numbered previews and “5/5 connected”; demonstrate a sixth join gets a capacity message.
3. **Broadcast:** start the primary wide feed with graphics. Show the operator's on-air and analysis status separately.
4. **Recognize:** perform a clear live action. Show its YOLO/Cosmos evidence and the resulting scene.
5. **Replay:** show the automatically prepared clip with slow motion/zoom and grounded commentary, then return to live.
6. **Recall:** ask a natural-language question about the action and play the returned moment.
7. **Recover:** stop one feed and demonstrate fallback while the others continue.

Use a staged action that the actual models detect in rehearsal. Do not present hardcoded results as a live integration. Fixture mode uses test data and must be clearly labeled.

Save the final recording, trace IDs, measured timings, known limits, and provider versions as demo evidence. A trace ID links records from the same operation. Keep this evidence in one project record and link to it.

## Remaining decisions

| Decision | Working default |
|---|---|
| Event content | Soccer-style demo; stage profile documented |
| Input | Live phones via shared event QR; maximum five, confirmed by user |
| Distribution | Private browser viewer during hackathon |
| Official score/clock | Operator input; unknown until supplied |
| Commentary voice | Required: one expressive voice with witty, playful delivery; verify provider access. Captions are a failure fallback |
| Multi-camera cut tolerance | 150 ms initial acceptance target for combined uncertainty; actual measurements are recorded separately |
| Venue topology | Gateway on reliable reachable network; test phone-to-gateway connectivity early |

Provider endpoints, media formats, venue network layout, compute capacity, event identity, and source material remain unverified. Resolve them at stage 0. Do not put unverified assumptions into prompts.

## Multi-camera local acceptance

The [multi-camera implementation](15-multi-camera-replays.md) adds source-linked shot review and encoded previews. Its [authoritative evidence](evidence/multi-camera-replay.json) records actual samples, render times, marker error, rejections, and browser results. Synthetic recordings prove the local timing and editing mechanics. They do not close stage 0 provider access or the physical-phone rehearsal.


## Crew studio local boundary

The local Studio phase adds shared action records, human takeover, exact local commands, and an explicitly started local rehearsal. Fresh validated crew proposals can run by default; human takeover pauses them until release. There is no per-action approval mode. It preserves the stages and provider requirements above. Its [PRD](16-autonomous-studio-prd.md) defines UX1–M3. [Phase evidence](evidence/autonomous-studio.json) records the actual local checks and remaining phone/provider work. A local rehearsal does not complete hosted-model direction or scene recognition.
