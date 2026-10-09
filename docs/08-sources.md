# Sources and verification limits

**Updated:** 2026-10-09

The source review took place on **2026-10-05**. The official product docs below establish public capabilities. They do not verify enabled features, credentials, latency, or quotas in the hackathon environment. Architecture choices and numeric targets are proposed design settings.

| Source | Used to verify |
|---|---|
| [VAST DataEngine overview supplied by user](https://kb.vastdata.com/documentation/docs/overview-of-vast-dataengine-1) | Trigger/function/pipeline model |
| [VAST trigger creation](https://kb.vastdata.com/documentation/docs/creating-a-trigger) | S3 element triggers and object filters |
| [VAST function creation](https://kb.vastdata.com/documentation/docs/creating-a-dataengine-function) | Container packaging and Python entrypoints |
| [VAST pipeline deployment](https://kb.vastdata.com/documentation/docs/building-and-deploying-a-pipeline-on-vast-dataengine) | Deployment resources, retry/timeout/ordering options, acyclic graph |
| [VAST source-view provisioning](https://kb.vastdata.com/documentation/docs/provisioning-source-views-for-trigger-events) | Tenant prerequisites for source triggers |
| [VAST vector search](https://kb.vastdata.com/documentation/docs/vector-search) | Version-specific vector capability and limits |
| [NVIDIA Cosmos Reason1 NIM API](https://docs.nvidia.com/nim/vision-language-models/1.4.0/examples/cosmos-reason1/api.html) | Video input, sampling, temporal localization preprocessing |
| [NVIDIA Cosmos Reason2](https://docs.nvidia.com/cosmos/latest/reason2/index.html) | Reasoning-model family; does not identify the provided endpoint |
| [Ultralytics tracking](https://docs.ultralytics.com/modes/track) | Persistent per-stream tracker state |
| [W&B / CoreWeave serverless API](https://docs.coreweave.com/products/inference/serverless/api-reference) | Hosted model API and model discovery |
| [MediaMTX browser publishing](https://mediamtx.org/docs/publish/web-browsers) | Browser capture integration option |
| [MediaMTX WebRTC features](https://mediamtx.org/docs/features/webrtc-specific-features) | Connectivity and codec constraints |
| [MediaMTX recording](https://mediamtx.org/docs/features/record) | Recording and completed-segment upload hooks |
| [MDN getUserMedia](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia) | Permission, HTTPS, and camera constraints |
| [OBS remote control](https://obsproject.com/kb/remote-control-guide) | Persistent compositor control option |
| [FFmpeg filters](https://ffmpeg.org/ffmpeg-filters.html) | Deterministic media editing primitives |
| [FFmpeg formats](https://ffmpeg.org/ffmpeg-formats.html) | Segmenting and delivery format behavior |

The source review did not test a camera, VAST tenant, CoreWeave deployment, model endpoint, search service, text-to-speech (TTS) endpoint, or broadcast destination. At the start of that review, the workspace contained only `espn.svg` and an empty `docs` directory. The review found no application implementation or deployment.

## Local artifact validation

Completed on 2026-10-05:

- All 11 Mermaid diagrams rendered successfully with Mermaid CLI.
- The architecture overview and graphics contact sheet were rendered in Chrome and visually inspected.
- Local Markdown links and code-fence closure passed checks; all JSON and SVG files parsed.
- Graphics manifest binding IDs resolve to actual SVG elements.
- The example replay computes to 9 seconds; its speeds, crop bounds, aspect ratio, and zoom satisfy the example event policy.
- All three project skills passed the skill-creator validator using an isolated `uv` environment with PyYAML.

These checks validate the documentation and starter assets. Live-camera, model, media-render, and deployment acceptance tests remain open. [The build plan](07-build-and-demo.md) defines those tests.

## Explanation-style review

Completed on **2026-10-06**. The README and all nine design docs now use shorter sentences and explain necessary terms. Diagram labels are shorter. Each complex flow has a diagram and a plain-language statement of its main point. The build plan now includes a dependency diagram.

- Checked all 14 Markdown files. Code fences close correctly.
- Checked 63 local links, including heading anchors. All targets resolve.
- Rendered all 12 Mermaid diagrams successfully with Mermaid CLI.
- Compared the contract tables and numeric values with the previous docs. Contract fields and numeric targets remain unchanged.

The source review date above remains 2026-10-05. This style review did not recheck provider access or run live-media acceptance tests.

## Stack reuse review

Completed on **2026-10-06**. The [stack review and work split](10-stack-review-and-work-split.md) records the local MediaMTX findings, public provider checks, GitHub candidates, and selected Muxshed source inspection. It separates documented capabilities from untested integration. No candidate application, live camera, or provider endpoint was run in this review.

Checked the four edited Markdown files: 29 local links resolve and code fences close. `git diff --check` passed. The new Mermaid flow was not rendered in this review.

The subsequent [media experiment validation](11-media-experiment-validation.md) is the authority for implementation versions, measured output, completed tests, and remaining gates. Its sample and browser-fixture results do not verify provider access or physical phones.

## Current workshop transport sources

Reviewed on **2026-10-09**. The local `.cursor/skills/` upload, videos, search,
GPU guide and model health documents define the supplied VSS and GPU calls.
[CoreWeave's Serverless API](https://docs.coreweave.com/products/inference/serverless/api-reference)
defines the W&B endpoint, bearer key and model listing.
[Chat Completions](https://docs.coreweave.com/products/inference/serverless/api-reference/chat-completions)
defines the optional `OpenAI-Project` header.
[PydanticAI's OpenAI provider](https://pydantic.dev/docs/ai/models/openai/)
defines custom Chat Completions transport and typed model output.
These sources establish request contracts, not access to the assigned tenant.
The user requested skipped checks while connections are implemented.

[NVIDIA's TTS HTTP API](https://docs.nvidia.com/nim/speech/26.07.0/reference/api-references/tts/http-tts.html)
defines Magpie NIM model metadata, voice listing and multipart synthesis returning
WAV. [NVIDIA Dynamo voice APIs](https://docs.nvidia.com/dynamo/dev/multimodal/voice-pipelines)
document the separate OpenAI-compatible speech route. Neither establishes a
running speech service on this team's VM.

The user then selected ElevenLabs. Its [speech conversion API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert),
[model listing](https://elevenlabs.io/docs/api-reference/models/list), and
[voice listing](https://elevenlabs.io/docs/api-reference/voices/search) establish
the key header, available speech identities, and standard MP3 output. The adapter
converts MP3 locally rather than requiring a higher-tier WAV output format.


## Docker packaging checks

The historical media validation completed. The media experiment now uses one Docker image. [The validation record](11-media-experiment-validation.md#docker-validation) links the exact build inputs, full media/browser checks, and container lifecycle results. Image digests were resolved from the official Docker registry. Python wheel hashes came from each fixed release's PyPI metadata. The Debian package set uses the 2026-10-06 snapshot.

[Docker stop documentation](https://docs.docker.com/reference/cli/docker/container/stop/) and [MediaMTX Docker networking](https://github.com/bluenviron/mediamtx/blob/main/docs/1-kickoff/2-install.md) informed signal handling and published media ports. Runtime tests establish only the local ARM64 container behavior recorded in the validation record.

## Task 3 library and implementation review

Reviewed on **2026-10-07**. The [Task 3 PRD library decisions](20-timely-replays-and-broadcast-validation-prd.md#library-decisions-and-research) record the GitHub and maintainer sources, selected reuse, and alternatives considered. The review used the current dependency lock, application code, Git history, design docs, and checked-in validation records. It selects existing PyAV, Pydantic AI, FFmpeg, storage, HTTP, and browser tooling. It adds no runtime dependency or integration claim. Task 3 media, provider, and physical-device acceptance remain open.

## VM startup and model selection correction

Reviewed on **2026-10-09**. The teammate's
[VM problem report](27-vm-integration-problems.md) establishes the reported
configuration and control failures.
[CoreWeave model listing](https://docs.coreweave.com/products/inference/serverless/api-reference/list-models)
establishes account catalog discovery.
[CoreWeave available models](https://docs.coreweave.com/products/inference/serverless/models)
establishes the exact default preference IDs and their text/vision types.
These documented candidates are selected only if the live account catalog
returns them. Their actual image/tool output and latency remain unverified.
[NVIDIA NIM health reference](https://docs.nvidia.com/nim/large-language-models/2.0.13/reference/api-reference.html)
and the supplied workshop GPU health matrix define HTTP 200 as the supported
readiness/liveness result. JSON inference contracts remain separate.
