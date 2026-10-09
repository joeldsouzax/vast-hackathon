# Breadcast deployment record

**Updated:** 2026-10-09

S01 has a deployment package and [tracked run commands](../README.md). The actual
production host and physical-phone acceptance remain unverified. This record
holds verified host facts when the user's production session supplies them.

| Required fact | Current result |
|---|---|
| Release | S01 candidate; tag `breadcast-s01-r01` after verified push |
| Runtime package | Pinned Dockerfile, Compose token secret, one controller, persistent runtime volume |
| Local Docker build | Blocked: Docker service and Compose plugin unavailable in this workspace |
| Local media smoke | Passed with MediaMTX v1.20.1, FFmpeg 9.0.1, Python 3.13.13; synthetic source only |
| Production host and resources | Unknown; record the user's tested host and limits |
| Trusted HTTPS origin | Unknown; configure `BREADCAST_PUBLIC_URL` before deployment |
| Public prefix | Empty or `/app`; application contract checks pass, host route pending |
| WebRTC route | Unknown; prove actual phone/viewer TCP/UDP or verified TURN reachability |
| Operator access | Compose token mode; scoped application access checks pass |
| Physical phone and viewer | Pending five-minute production test on the pushed SHA |
| Provider services | Unavailable in the S01 live configuration |

Use the README's setup, start, health, logs, stop, and production checklist.
Record the deployed SHA before testing. Keep the token, TLS key, private footage,
and full runtime logs outside Git. A page loading does not establish live media.

For a failed check, record the case, expected/observed result, elapsed interval,
tested SHA, browser/device/network details, and private artifact path/checksum.
Use an opaque run ID and no calendar-date run label. A fix needs a new commit/tag;
do not overwrite a release tag or carry its pass status to changed code.

Stop the test session before rebuilding or changing host routing. Keep the named
runtime volume when retaining evidence. Restart must enter holding and require
a new human Start. There is no previously accepted sprint release yet.
