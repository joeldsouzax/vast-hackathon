# Repository layout

**Updated:** 2026-10-09

The studio now uses normal application, test, script, and Docker folders. Start it from the repository root with `docker compose up --build`. See [the run guide](12-studio.md) and [graphics guide](13-graphics-package.md).

| Path | Purpose |
|---|---|
| `app/` | Python server, program controller, graphics engine, and `web/` pages |
| `tests/unit/` | Camera admission and graphics contract tests |
| `tests/browser/` | Playwright browser checks and their Node dependencies |
| `tests/media/` | Real encoded-media checks and Docker lifecycle checks |
| `tests/fixtures/` | Supplied sample video |
| `scripts/` | Studio Compose wrapper; the earlier standalone MediaMTX launcher is absent from this checkout |
| `docker/` | Image entrypoint and health check |
| `Dockerfile`, `.dockerignore`, `compose.yaml` | Image build and local service configuration |
| `requirements.lock` | Exact Python dependency versions and wheel hashes |
| `assets/graphics/` | Original SVG template fixtures |
| `docs/` | Design, run guides, and dated validation records |
| `.runtime/` | Private local recordings, access files, and reports; excluded from Git and image builds |

The Docker image keeps `/opt/breadcast/demo.mp4` as its sample-video path. Existing `breadcast-studio serve`, `sample`, and `check` commands still work. The host wrapper is now `scripts/studio`. The image tag is `breadcast-studio:latest`, and the Compose project is `breadcast`.

## Earlier paths

The folder move preserves local runtime files. Dated evidence records keep the paths and hashes from their original runs. Use this mapping to locate those files after the move. It does not change earlier measurements or claim they were checked in the new layout.

| Earlier path | Current path |
|---|---|
| `_experiments/studio/{studio,media,graphics}.py` | `app/{studio,media,graphics}.py` |
| `_experiments/studio/web/` | `app/web/` |
| `_experiments/studio/test_*.py` | `tests/unit/` |
| `_experiments/studio/tests/*.cjs` and Node manifests | `tests/browser/` |
| `_experiments/studio/check.py`, `graphics_check.py` | `tests/media/` |
| `_experiments/studio/tests/docker-check.py` | `tests/media/docker-check.py` |
| `_experiments/studio/.run/` | `.runtime/` |
| `_experiments/demo.mp4` | `tests/fixtures/demo.mp4` |
| `_experiments/studio/studio` | `scripts/studio` |
| `_experiments/run-mediamtx.sh` | `scripts/run-mediamtx.sh` |
| `_experiments/studio/Dockerfile` | `Dockerfile` |
| `_experiments/.dockerignore` | `.dockerignore` |
| `_experiments/studio/container-*.sh`, `container-*.py` | `docker/` |
| `_experiments/studio/requirements.lock` | `requirements.lock` |
| `_experiments/studio/README.md` | `docs/12-studio.md` |
| `_experiments/studio/GRAPHICS.md` | `docs/13-graphics-package.md` |

[The layout validation record](evidence/studio-layout.json) records the new build, contract checks, media output, browser controls, and container lifecycle. These checks use sample media and fake camera devices. Physical phones and remote hosting remain separate acceptance gates.
