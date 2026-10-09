---
name: breadcast-stack-integration
description: Integrate or troubleshoot Breadcast's required VAST, Cosmos, YOLO, semantic-search, and W&B/CoreWeave services using the actual hackathon endpoint contracts.
---

# Integrate the supplied stack

Read [the integration design](../../docs/03-stack-integration.md) and [contracts](../../docs/05-context-and-contracts.md). Preserve supplied services and keep model latency outside continuous media playback.

Record actual versions, endpoint capabilities, permissions, and request limits before depending on them. Public docs establish possible features; they do not prove tenant access. Use provider-returned model IDs. Keep secrets outside event context and prompts.

Start with one finalized live-camera chunk: store bytes, verify access, then publish the immutable manifest. Configure narrowly filtered VAST triggers. Normalize provider events into application messages; preserve stable logical work keys. Check repeated and out-of-order delivery without duplicate side effects.

Verify Cosmos preprocessing and timestamps on the provided model. Verify who owns YOLO tracker state, preserving it per source epoch. For search, keep query/index embedding versions consistent and return timed evidence references. For LLMs, validate structured responses and implement a bounded fallback.

Choose render placement only after checking execution limits and scratch resources. A VAST orchestration function may submit a render job to a persistent worker; do not assume it is a native media-editing API or a GPU allocation.

Report one real request/result per integrated service, latency, trace ID, and remaining blockers. Label fixture mode explicitly. Stop live proposal retries at their deadline; retain useful archive results without putting them on air late.
