# Current integration and ownership

Read alongside [SPEC.md](SPEC.md) and [acceptance.md](acceptance.md). This is a source audit, not a claim that character sounds exist or are live.

## Verified source seams

Audit date: 2026-11-22. Voxxy began clean at `7aaf5e0`. The installed Hermes workspace is under `~/.hermes/installs/8939a02a6bce15ab/environments/66d6edda458d474984806ee187ad2093/workspace`; source ownership points to fork checkout `~/.hermes/hermes-agent`, inspected at `d6100d8`. Worker comparison found the inspected delivery files identical; the PM independently read the whole-file and streaming seams below.

| Surface | Source evidence | Consequence |
| --- | --- | --- |
| Character wording | `plugins/voice-persona/__init__.py:588-607` registers `transform_tts_text` | Replacement text before synthesis, not an audio hook. |
| Core speech | `app/main.py:327-359` generation; `:430-465` conversion/cache | Compose after generation and before encoding/cache only with explicit complete-reply ownership. Sidecars never add cues. |
| Voice metadata | `app/voices.py:19-45` persona/reference fields | Separate effects from reference WAVs; additive metadata preserves persona precedence and explicit `none`. |
| CLI | `cli/voxxy/commands/speak.py:181-238,350-488` | Raw/local uses WAV, out/sink uses URL/OGG, via uses remote raw; one composition owner per reply. |
| Hermes Vox provider | `plugins/tts/vox/__init__.py:137-225` | One HTTP request per invocation, no reply markers; provider-only cues repeat per chunk. |
| Hermes whole-file | `tools/tts_tool.py:469-560` | Whole text is transformed, chunked, synthesized, then packed; reply-wide composition belongs before packing. |
| Gateway streaming | `gateway/streaming_tts_consumer.py:76-96,117-174,195-208` | `on_delta(None)` flushes a segment, not a reply. Every PCM write currently counts as audible; a jingle alone could suppress speech fallback. |
| Other sentence consumers | `tools/tts_tool_speaker.py:126-155,332-407`; `hermes_cli/web_routers/audio.py:411-419,601-782` | Local speaker/Desktop own reply lifecycles even with nonstreaming providers; a whole-file hook alone is incomplete. |

Hermes paths are relative to its source root. Vox has no native PCM streaming override today. Gateway commentary/pre-tool flushes, final answer, whole-file packing and local sentence playback must declare whether they share a reply. Internal sentence/ack calls cannot acquire separate framing.

## Delivery slices

1. **Voxxy foundation:** A reusable CPU-only audio Decorator/Composite, policy/asset resolution, explicit complete-reply API intent and CLI forwarding. Independently useful for one complete `voxxy speak` or explicit complete-reply API call. Plain synthesis/internal chunks remain unchanged. Expose the same policy/composer to the later Hermes adapter, not parallel implementations.
2. **Fleet intake for Hermes:** Flume coordinates an accountable fork-source owner and the smallest provider-neutral reply/audio seam. Hermes has no board binding in the inspected checkout; FLUME is the verified fleet coordination board, not assumed Hermes code ownership. The owner accepts source work before dispatch. Use an isolated fork clone, never the installed workspace. No rollout is authorized.
3. **Voxxy Hermes integration:** Consume the accepted seam, reuse the foundation, disable per-chunk server effects, and prove actual whole-file and sentence paths. This waits on the foundation and upstream seam; the foundation does not wait on Hermes.

No file-scope collision with VOXXY-1's skill-only clone work was found. VOXXY-3 concerns telemetry in core and remains Backlog. If later accepted work overlaps the same generation seam, sequence those actual scopes with a blocking relation before concurrent edits.

## Minimum upstream contract

- Stable reply identity, effective character snapshot, output/profile scope, purpose (reply vs internal segment/ack), terminal outcome.
- Ordered successful speech artifacts and safe boundaries for whole-file composition before upload packing: one intro/outro globally, regardless of file count.
- First available speech, safe boundary, successful finalization and abort callbacks or equivalent session contract; identity and cue state survive fallback.
- Track cues separately from speech. Cue-only output never suppresses eligible speech fallback; fallback cannot replay an emitted intro. Buffer until speech is available to avoid intro-only streaming output.
- Abort, drops, late output, timeout and stale generation discard pending cues; incomplete speech gets no outro. Logical completion is not proof a human heard it.
- Validate format, budgets and ownership; cleanup only invocation-owned files. Missing hooks/assets/composition errors preserve original speech.

## Configuration and supplied assets

Use effective character/persona resolution, not Daddy Pig/Rick branches. A manifest references optional intro/outro IDs and an interjection pool with frequency/probability, cooldown, maximum count, individual and total durations. Intro/outro also have finite caps. Document units/defaults/precedence; request-level disable wins. Generated personas cannot fabricate clip references or enable sounds automatically. Audio effects remain independent of whether text rewriting is enabled.

Provide documented local import/list/remove for supplied WAV/OGG clips into a configured effects directory separate from references and ephemeral audio cache. Copy/normalize without overwriting source recordings, record provenance, validate bounded decoding, and preserve imported assets in host bind storage. Test import, assignment, resolution after restart, unbinding and removal; referenced removal must refuse clearly or safely produce speech-only, never affect a clone reference. This is a small asset path, not remote fetching or an acquisition workflow.

Cooldown persists across separate CLI commands in configured host-local state. Scope it to resolved character plus destination (documented local/default scope or sink); Hermes uses profile/destination scope. Atomic bounded reservations prevent concurrent double-selection; abort/crash cannot leave a permanent hold. Fake-clock tests cover process restart, expiry and profile isolation. No daemon, native timer or recurring scheduler is needed.

Normalize clips to actual speech format; preserve speech samples when no conversion is needed. Insert only between complete speech segments or certified acoustic pauses: punctuation is not a timestamp. If segmentation is needed, preserve every token and generation option, and compare composition with that exact unadorned segment baseline. Never add an LLM call per interjection.

## Missing assets and decision basis

Daddy Pig/Flume jingles and character recordings are examples, not supplied assets. Do not infer their content/timing or repurpose `voices/daddy-pig.wav`/`voices/rick.wav`: those are cloning references. Real asset selection is later owner input, not a blocker for fixture-tested engineering.

Chosen over character branches and LLM-written sound words: canonical `~/code/33GOD/momo/PILLARS.md` #3 (Build LEGO, Not Statues), #4 (Gang of Four by Default), plus global `belsky-doctrine` and `zero-dollar-defaults`. Target any character with existing codecs and an opt-in decorator, not a new sound service. The ecosystem map still mentions retired Momo execution; current Krebs lifecycle v2 overrides that stale guidance.
