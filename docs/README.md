# Breadcast documentation

**Updated:** 2026-10-09

Use [the live stack integration PRD](22-live-stack-integration-prd.md) for current
implementation. Use [the sprint plan](26-sprint-delivery-plan.md) for seven runnable
releases, required targeted tests, and production acceptance after every push.
The PRD connects the existing studio to the workshop services. Use
[the parallel plan](23-parallel-implementation-plan.md) to assign independent
agents and [provider verification](24-provider-verification.md) to resolve access
and capability gates.

## Current implementation documents

| Document | Purpose |
|---|---|
| [22 Live stack integration PRD](22-live-stack-integration-prd.md) | Behavior, design, failure handling, acceptance and release gates |
| [26 Runnable sprint plan](26-sprint-delivery-plan.md) | Seven complete user flows, commit/push handoffs, minimum tests and production acceptance |
| [23 Parallel implementation plan](23-parallel-implementation-plan.md) | Work packages, file ownership, sprint assignments, required tests, merge order and agent prompt |
| [24 Provider verification](24-provider-verification.md) | External facts, documentation conflicts, probes and unresolved gates |
| [25 Deployment record](25-live-deployment-runbook.md) | S01 package, verified local facts and pending production host proof |
| [05 Context and contracts](05-context-and-contracts.md) | IDs, clocks, evidence, ownership and new adapter seams |
| [Restoration manifest](restoration-manifest.json) | Source backup and original checksums |
| [Documentation validation](evidence/documentation.json) | Current restoration, link, JSON, build-input and replay-example checks |

## Restored design and handoff documents

| Documents | Subject |
|---|---|
| [01 Sketch review](01-sketch-review.md), [02 Architecture](02-architecture.md) | Product and system design |
| [03 Integration](03-stack-integration.md), [04 Agent instructions](04-agent-instructions.md) | Earlier provider design and runtime role behavior |
| [06 Graphics and replays](06-graphics-and-replays.md), [07 Build and demo](07-build-and-demo.md) | Production rules and inherited acceptance |
| [08 Sources](08-sources.md), [09 Camera joining](09-camera-joining.md), [10 Work split](10-stack-review-and-work-split.md) | Source history, phone admission and earlier assignment |
| [11 Media validation](11-media-experiment-validation.md), [12 Studio](12-studio.md) | Local media behavior and run instructions |
| [13 Graphics](13-graphics-package.md), [14 Layout](14-repository-layout.md), [15 Multi-camera replays](15-multi-camera-replays.md) | Assets, repository and replay mechanics |
| [16 Autonomous studio](16-autonomous-studio-prd.md) | Existing human/crew controls |
| [17 Foundation PRD](17-event-understanding-and-provider-adapters-prd.md), [18 Foundation handoff](18-event-foundation.md) | Evidence, media and providers |
| [19 Direction PRD](19-live-direction-and-commentary-prd.md) | Crew decisions and speech |
| [20 Replay PRD](20-timely-replays-and-broadcast-validation-prd.md), [21 Replay handoff](21-timely-replays.md) | Timely replays, recall and validation |
| [Examples](examples/), [historical evidence](evidence/) | Fictional examples and earlier results |

## Restored document provenance

On 2026-10-09, 51 missing files were restored from local backup
`hackathon-new-docs-20261009T143520710401Z`. These include design documents, five
JSON examples, diagrams, and historical validation records. The backup is under
`/Users/joel/orca/workspaces/breadcast/backups/`. Existing workshop files were not
replaced.

The manifest records each source file's original SHA-256. Later contract additions,
document dates, and removal of test-report date labels are deliberate edits and
may differ from that checksum. `restored_path` identifies a renamed report.
Preserve the source hashes rather than rewriting them to hide edits.

Restored records include work dated before this hackathon. They document prior
design and validation. They do not establish current tests, live access, or
eligibility under event submission rules. Keep historical evidence distinct from
current checks. Restoration does not change the root README's submission scope.

PRD 22 replaces older proposals to deploy custom DataEngine functions for this
workshop. Document 23 replaces old development assignments. Document 26 controls release
order and supersedes blanket historical full-suite requirements. Contract authority
and inherited acceptance remain active with the explicit current refinements.
No historical pass flag closes a current live or physical-device gate.

## Document dates

Every project Markdown document in this folder is marked updated on 2026-10-09.
This is the document edit date. Test report names and run-date metadata omit
calendar dates. Durations, media timing, revision IDs and historical scope remain.
Original artifact paths may contain timestamps; keep those exact paths so the
evidence can still be located. Dependency snapshot versions, fictional media
timestamps and source-review dates are not test-run labels. Supplied workshop
PDFs and images remain source assets.

Project docs are tracked under the user's latest instruction to remove `/docs/`
from `.gitignore`. Keep private media, credentials, and large runtime evidence
outside Git. Existing workshop assets remain tracked.
