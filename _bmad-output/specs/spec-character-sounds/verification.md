# Character-sounds planning verification

2026-11-22 — voxxy-pm

## Scope produced

- Contract: [SPEC.md](SPEC.md), [brownfield.md](brownfield.md), [acceptance.md](acceptance.md); decisions and reviewer fixes in `.memlog.md`.
- [VOXXY-4](https://plane.delo.sh/33god/browse/VOXXY-4/): reusable complete-reply foundation; readiness and execution state live on the board.
- [VOXXY-5](https://plane.delo.sh/33god/browse/VOXXY-5/): Hermes integration; prerequisite relations from VOXXY-4 and FLUME-110 were verified through bound reads.
- [FLUME-110](https://plane.delo.sh/33god/browse/FLUME-110/): fleet coordination intake; Hermes fork code ownership is not assumed. Flume owns accepting/routing this request.
- Ticket writes used verified Voxxy PM native identity. The board-driver lease was acquired/refreshed/released with one literal turn owner, using the actual role runtime path read from the launcher. No `agent:working` edits or implementation dispatch.

## Independent review

A read-only reviewer confirmed preservation of whole-reply ownership, unchanged speech/defaults, safe boundaries and bounded/fail-open cues. It found missing asset provisioning, undefined cross-command cooldown, and insufficient fractional-probability coverage. The revised contract supplies local import/inspect/remove and persistent separate asset storage, atomic scoped cooldown across CLI processes, and seeded intermediate-probability tests.

## Local test execution

Core routing command:

`env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -m pytest -q --tb=short tests/test_synthesize_routing.py`

Returned: `34 passed in 0.78s`.

Provider command:

`env -u PYTHONHOME PYTHONPATH=/home/delorenj/.hermes/hermes-agent .venv/bin/python -m pytest -q --tb=short plugins/tts/vox/tests/test_vox_tts_plugin.py -k 'not live_vox_synthesis'`

Returned: `1 failed, 7 passed, 1 deselected in 0.15s`. The category-discovery test cannot import `ruamel.yaml` from the Voxxy test environment. Its import-path issue was resolved before this run; this remaining dependency failure is visible, not a passing regression gate. The existing Hermes install interpreter has no pytest, so switching to it did not provide an alternative test environment. No shared dependency installation or runtime repair was attempted.

An earlier combined run returned `34 passed, 9 errors`; provider test collection lacked the Hermes `agent` import path. No test body made a live synthesis request. The later provider command explicitly deselected the opt-in live test.

These are existing regression checks, not new character-sounds acceptance results. No character-sounds implementation or audio artifact exists from this pass.

## Boundaries

Planning documents and tickets only. No application code, voice asset/configuration, live profile, service, schedule, deployment, gateway restart, merge or push changed. Real Daddy Pig jingles remain unsupplied; synthetic tones in future tests must never be presented as those clips.
