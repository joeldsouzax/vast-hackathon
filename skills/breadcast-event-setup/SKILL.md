---
name: breadcast-event-setup
description: Prepare Breadcast event context, QR camera admission settings, and a validated startup graphics package before a live event.
---

# Prepare a Breadcast event

Read [context and contracts](../../docs/05-context-and-contracts.md), [graphics](../../docs/06-graphics-and-replays.md), and [camera joining](../../docs/09-camera-joining.md). Use the [example context](../../docs/examples/event-context.example.json) as a fixture, not as real event facts.

1. Collect the event identity, selected profile, supplied participant names, branding, official-state source, and audio policy. Keep unknown facts null; request only missing information that prevents setup.
2. Create a validated context revision. Keep camera leases, scores, running clocks, and credentials out of the stable brief.
3. Configure up to five camera slots and the designated primary/audio policies. Generate the QR from the reachable event join URL; test it on a phone. Enforce admission at the backend and publishing gateway.
4. Bind the chosen graphics templates from context. Render opening, profile overlay, lower third, replay, holding, and closing assets. Check sample-text replacement, font availability, long names, alpha, and output dimensions.
5. Publish a graphics manifest with context revision and hashes; preload assets before setting READY. If optional artwork fails, use the neutral template package.

Completion evidence: context revision, graphics manifest, preview, required-field validation, working camera-join check, and unresolved provider dependencies. Do not claim on-air readiness if the media path has not been checked. Preparing an event does not itself start a public broadcast.
