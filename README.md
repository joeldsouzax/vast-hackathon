# Breadcast

Breadcast is an idea for an autonomous live broadcast studio. People join an event through a QR code and share live video from their phones. The studio selects camera views, prepares replays, and adds commentary.

This hackathon repository starts on October 9, 2026, with idea notes only. Earlier implementation exists in a separate repository and is excluded from this submission. New implementation must be written during the hackathon. Commit completed work with its actual date and time.

## Build goal

- Prove continuous playback from one live phone camera first.
- Admit up to five cameras. Enforce the limit on the server.
- Keep playback running while models analyze video.
- Prepare event graphics before the broadcast. Bind known event facts at runtime.
- Add evidence-based spoken commentary and a replay that returns to live.

## Supplied stack

Use VAST for media storage and data orchestration, NVIDIA Cosmos for video reasoning, YOLO for detection and tracking, semantic search for earlier moments, and W&B-hosted application language models on CoreWeave. Verify available endpoints before writing provider integrations.

## First milestone

Write a new camera join page and prove that one phone can publish video to a viewer. Record the playback delay and any failures. No application code or assets are included yet.
