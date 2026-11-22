---
id: SPEC-character-sounds
companions:
  - brownfield.md
  - acceptance.md
sources: []
---

# Character sounds

## Why

Jarad wants voices to carry a character's audible personality, not only its wording: a Daddy Pig jingle around Flume's replies, occasional oinks, or Rick's burps. Make these reusable character settings rather than special cases for individual agents.

## Capabilities

- **CAP-1**
  - **intent:** Associate optional intro/outro clips and a reusable interjection pool with any character.
  - **success:** Documented commands import, inspect, assign, clear and remove supplied clips for two different characters without changing cloning references; assets remain resolvable after restart.
- **CAP-2**
  - **intent:** Frame a complete spoken reply and occasionally insert a character sound without changing its words.
  - **success:** Decoded fixture audio contains one intro, all original speech in order, only boundary-safe interjections, and one outro across synthesis chunks and delivery parts.
- **CAP-3**
  - **intent:** Control repetition and turn effects off without risking speech delivery.
  - **success:** Frequency, persistent scoped cooldown, count and duration limits are enforced; a disable override, absent configuration and broken optional assets retain baseline speech.
- **CAP-4**
  - **intent:** Apply the same character behavior at Hermes's whole-reply boundary, including sentence-based playback.
  - **success:** Completion produces one outro; tool boundaries, retries and file splits do not repeat cues; interruption cancels pending cues without losing eligible speech fallback.

## Constraints

- Configure audio separately from persona text rewriting and cloning references. No sound words or clip directives are sent as speech text.
- Existing callers remain unchanged unless character sounds are configured and a caller explicitly declares ownership of the complete reply. Unknown/internal chunk calls stay speech-only.
- Missing or invalid optional cues fail open. No certified safe boundary means no interjection, not a guessed cut through speech.
- Reuse existing codecs and CPU-only core; no GPU model change, new paid synthesis call for a cue, remote asset fetch at playback, or encoded-OGG byte concatenation.
- Import supplied clips into a separately configured persistent effects directory, preserving originals and provenance. Do not acquire third-party clips or repurpose cloning references.
- Character selection is snapshotted once per reply. Budgets span chunks; cooldown persists across separate CLI invocations and is isolated by character/output scope. No new daemon or recurring schedule.
- Intro is emitted only once speech is available. Aborted, failed, dropped or stale replies receive no pending outro; cue-only delivery must not suppress required speech fallback.
- Voxxy owns policy/assets/composition. Hermes owns reply boundaries and delivery. Its unbound source ownership must be routed through the fleet coordinator before integration, not guessed.
- Every code delivery requires independent specification and quality review. This pass changes planning and tickets only; no live activation, restart, push or deployment.

## Non-goals

- Generating or guessing the missing Daddy Pig jingles; acquiring third-party audio; enabling effects on Flume or Rick now.
- Background music, overlapping speech, a general soundboard, or a new TTS engine.
- Repairing unrelated pre-existing streaming loss/truncation behavior or changing the existing persona rewrite policy.

## Success signal

A local fixture demonstration for Daddy Pig and Rick, followed by an unseen third character configured without code changes, produces complete speech with configured framing and bounded interjections. Tests prove silence from optional assets never becomes silence from the speaker; Hermes coverage is accepted only against its actual reply lifecycle, not a provider-only simulation.

## Assumptions

- Start with sparse documented defaults, including at most one interjection per reply; frequency and timing remain configurable. These are reversible implementation choices, not an inferred owner preference for exact values.
- Exact Daddy Pig intro/outro recordings will be supplied separately. Synthetic test tones are fixtures only, never substitutes for those recordings.
