# Acceptance and local proof

These are tests to implement, not results claimed by this planning pass. Use synthetic tones and a fake speech provider; no live GPU engine, paid provider, character recording, playback or restart is needed.

## Voxxy foundation

1. **Configuration/assets (CAP-1):** Import/list/assign/clear/remove supplied clips through documented commands and inspect associations through CLI/API. Configure Daddy Pig, Rick and an arbitrary third character without special-case code. Assets resolve after process restart from persistent effects storage. Originals, clone references and voice metadata stay intact; removal of a referenced asset has documented safe behavior. Preserve persona precedence/explicit `none`; audio enablement is separate from text rewriting.
2. **Speech order (CAP-2):** Decode fixture intro, several fake speech segments and outro. Assert one intro before all speech and one outro after the final frame. Removing cue samples recovers every baseline speech frame exactly at equal format. Provider text, order, voice, seed and routing options match baseline; no sound directives reach the provider.
3. **Safe boundaries (CAP-2):** Insert only at declared complete-speech boundaries or verified pauses. A word spanning a provider chunk, punctuation with continuous audio, code/number content and no-pause fixtures never get a mid-word injection. Without an eligible boundary, omit interjections. Do not re-synthesize words to fake alignment.
4. **Limits/cooldown (CAP-3):** Use a fake clock and seeded selection. Cover probability zero/one and an intermediate fraction with both selected/skipped outcomes, empty pools, cooldown just below/at expiry, count cap, oversize clips and exhausted duration budget. Example test settings: probability one, thirty-second cooldown, at most two interjections, two seconds per clip, four seconds total interjections. Intro/outro have separate caps and a documented total inserted-audio budget. Test persisted cooldown across separate CLI processes, character/destination/profile isolation, atomic concurrent reservation, abort release and crash-expiry. Document sparse shipped defaults, including at most one interjection per reply.
5. **No-op/fail-open (CAP-3):** Unconfigured and disabled raw-WAV replies are byte-identical baseline with unchanged synthesis call count. Missing/corrupt/unsupported/over-limit cues, decoder/compositor error and timeout preserve complete speech. Bad configuration is explicit; runtime optional-asset failure does not fail successful speech. Logs contain bounded metadata, not text/transcripts/audio.
6. **Ownership/format (CAP-2/3):** Complete-reply intent decorates once; internal/unknown chunks do not. Normalize mixed-rate/channel cues to actual speech PCM without clipping, overlap or encoded-byte concatenation. Engine/voice-source headers remain truthful; final duration/byte metadata includes inserted audio. Restrict IDs to the configured store; traversal, escaping symlinks and playback-time network fetch fail safely.
7. **Delivery (CAP-2/3):** Fake engine/repo tests cover HTTP WAV, URL/cache OGG, existing OpenAI-compatible formats and MCP. Mock transport/playback for CLI local/raw/out/sink/via. Assert one composition owner, disable propagation, complete ordered audio and one composed sink item. Decode OGG with codec-appropriate ordering/duration tolerances, not lossless sample claims.

## Hermes integration

8. **Whole-reply packing (CAP-2/4):** Use the actual whole-file entrypoint, fake provider and small text/upload limits. Multiple provider chunks/files contain one global intro/outro. Internal sentence/ack calls never decorate independently; provider HTTP calls carry effects-off/internal intent. Disabled/unconfigured behavior matches baseline text, artifacts and call count.
9. **Completion vs boundary (CAP-4):** Drive real gateway and local speaker/Desktop lifecycles with fake PCM/sinks. Sentences, tool flushes, commentary and tail never restart framing. Outro follows successful complete speech, not provider iterator end. If a surface lacks the contract, leave effects explicitly off and its criterion open; do not claim full coverage.
10. **Interruption/fallback (CAP-3/4):** Cover failure before speech, cue-only failure, fallback, partial failure, finish failure, drop, timeout, barge-in, disconnect, stale generation and late chunks. Cues cannot swallow speech or masquerade as speech success. Fallback retains identity without duplicate intro; aborted/partial output has no pending cues and complete speech is not replayed after partial output.
11. **Isolation/cleanup (CAP-3/4):** Overlap replies in one destination and different profiles. Check identity, character snapshots, budgets, cooldown scope and no cross-character leakage. Composition/packing errors and pre-existing paths cannot delete unrelated files, return oversized files or leave indefinite audio artifacts.
12. **Review:** Independent specification and quality reviews precede PM acceptance. Attach real commands/output and fixtures; distinguish foundation from integration and name uncovered surfaces. Fakes are not deployment proof or permission to enable live profiles.

## Local verification commands

Use Python 3.12 and clear inherited foreign Python paths. Plugin tests additionally need the Hermes source import path; discovery tests use an isolated HERMES_HOME. Explicitly exclude the opt-in live synthesis test.

- Routing baseline: `env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -m pytest -q tests/test_synthesize_routing.py`.
- Provider baseline: `env -u PYTHONHOME PYTHONPATH="$HOME/.hermes/hermes-agent" .venv/bin/python -m pytest -q plugins/tts/vox/tests/test_vox_tts_plugin.py -k 'not live_vox_synthesis'`.
- CLI, from `cli/`: `env -u PYTHONPATH -u PYTHONHOME uv run pytest -q`.
- Hermes in an isolated fork clone: `HERMES_PYTHON=<test-python> scripts/run_tests.sh tests/tools/test_tts_transform_hook.py tests/tools/test_tts_long_form_chunking.py tests/tools/test_tts_plugin_dispatch.py tests/gateway/test_streaming_tts_consumer.py tests/gateway/test_streaming_tts_gateway_regression.py`.

Extend fake-engine fixtures in `tests/conftest.py`/`tests/test_synthesize_routing.py` and add focused sound tests in the owning suites. Verify runner availability in the chosen Hermes clone; do not edit the installed checkout or invent a CI command.
