---
name: breadcast-replay-production
description: Construct and validate Breadcast replay plans with source timing, speed changes, crop or zoom, and explicit audio policy; diagnose replay rendering failures.
---

# Produce a replay

Read [replay production](../../docs/06-graphics-and-replays.md) and the `ReplayPlan`/`ReplayAsset` contracts in [context and contracts](../../docs/05-context-and-contracts.md). Use [the example plan](../../docs/examples/replay-plan.example.json) only to understand structure.

Resolve the requested moment to actual source intervals and evidence IDs. Search results identify candidates; inspect media before selecting an edit. Include lead-in and aftermath, confirm finalized coverage across chunks, and pin the required media.

Choose the simplest edit that explains the action. Specify piecewise speeds and normalized crops in the plan; calculate airtime from source durations divided by speed. Keep crops within the oriented frame and the target aspect ratio. If tracking is weak, widen or use full frame. Alternate-angle cuts require valid synchronization.

Submit a validated plan to the deterministic worker. Never execute model-generated shell/filter text. Preserve the plan hash, source map, rendering configuration, and explicit audio policy. Mark ready only after complete decoding, measured-duration checks, crop inspection, and output availability.

If a render fails, simplify once when time permits or skip it. Expired, retracted, or unavailable material cannot become ready through repeated retries. Return the asset and validation report to the director; scheduling airtime belongs to the program controller.
