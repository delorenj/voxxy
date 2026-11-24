# Story: G5 consent check

Status: deployed 2026-11-23 (commit 19a1277; migration 004 applied; consent models preloaded). VOX_OPERATOR_KEY is commented out in .env.template until the vault item exists, so the review queue answers 503.
Pillar: `belsky-doctrine`
Calibration: `_bmad-output/implementation-artifacts/stories/voxxy-g5-spike.md` (the numbers below are its recommendations)
Decision source: `_bmad-output/planning-artifacts/voxxy-heyma-integration-scope.md` (Q4: own voice only in the hosted edition, with voice fingerprinting)

## Level up

Asked for: the consent check on the hosted "Record yours" flow.
Built: one consent gate (`app/consent.py`, `ConsentGate.check`) that is the single entry for ANY route that makes a likeness. Own-voice recording is its first caller. A later clone path (a Wax-clip seed, a customer upload) calls the same gate and gets the same challenge, ASR check, speaker match, decision, record and review queue without new machinery. Challenges are bound to a principal (`sha256(API key)[:16]`, or `open` with auth off), so per-customer keys inherit the gate.

## Acceptance criteria

1. Challenges live in postgres (`consent_challenges`): one random sentence plus a 6-digit code, bound to the caller, valid 10 minutes, redeemed once and atomically. An in-memory store with the same rules backs the tests.
2. Principal is derived in one helper, `app/auth.py: principal_for`.
3. ASR check: faster-whisper `base.en` int8 on CPU, beam 5, timestamps on, never prompted with the sentence or code. The spike's phrase rule: spoken numbers are folded to digits ("eighty-two", "N hundred [and] M", "double", "oh", homophones between digits); the sentence score is 1 minus the word error against its best-matching stretch of the transcript (extra words either side are free); the code is the edit distance between it and the transcript's digit stream. Sentence score at least 0.80 and the code within 1 edit passes; a sentence score of 0.50 to 0.80 goes to review; below 0.50 or a code 2+ edits off is refused.
4. Speaker check: TitaNet-large through sherpa-onnx (192 numbers, raw cosine), run on speech only (silero VAD, from faster-whisper, cuts the silence first) with at least 6 s of speech in the consent clip and 8 s in a longer sample. The consent clip is compared with an optional longer reference clip and with the account voiceprint. The first accepted own voice fixes the account voiceprint; two first recordings at once cannot both fix it (the loser is scored against the winner's and decided again).
5. Decisions:
   - accepted: voice tagged `own` and `consent-verified`.
   - review: voice stored tagged `consent-pending`; synthesis with it answers 403 (`X-Vox-Error: consent_pending`).
   - rejected: 422 with a plain, non-accusatory reason; no voice is saved, only the consent record and its audio are kept.
6. Thresholds are env-configurable, defaults from the spike: `VOX_CONSENT_ACCEPT` 0.70 and `VOX_CONSENT_REJECT` 0.10 (cosine at or above accept passes, below reject is refused, in between goes to review), `VOX_CONSENT_PHRASE_OK` 0.80 and `_PHRASE_REVIEW` 0.50, `VOX_CONSENT_MIN_SPEECH` 6 and `_MIN_REFERENCE_SPEECH` 8 (seconds). The wide review band is deliberate: the spike measured 14.5% to 16.5% equal error, so most genuine pairs land in review, which suits manual review of the first customers; 0 of 4800 phone strangers and 1 of 1899 voiceprint strangers reached accept, and 1.3% to 7.1% of genuine pairs fall below refuse.
7. Review queue, operator only (`VOX_OPERATOR_KEY`; 503 "no operator key configured" when unset, 403 for any other key): `GET /consent/reviews`, `GET /consent/reviews/{id}/audio`, `POST /consent/reviews/{id}`. CLI: `voxxy consent reviews|approve|reject|audio`.
8. Edition: hosted enforces. Self runs and records the checks but only warns (voice tagged `consent-unchecked-self`); an unloadable model never blocks a self install.
9. Models load lazily, off the event loop, with a 503 "consent check unavailable" when they cannot load (the challenge is not burned). Models live under `VOX_MODELS_DIR` (`/data/models`), download on first use (TitaNet-large 101,405,493 bytes, sha256 `d51abcf3...daca8e77` checked; whisper base.en 139 MB at a pinned revision; about 240 MB together), and sit on a compose volume (`./models`). `mise run models:fetch` pre-fetches them.
10. Web: Make a voice > Record yours shows sentence and code, an optional longer-sample step, outcome copy for verified, under review and not accepted, and consent state in the library and picker.
11. v1 voices tagged `unverified-consent` are retagged at startup: `consent-pending` in hosted, `consent-unchecked-self` in self. Hosted also refuses to speak with a `consent-unchecked-self` voice (a self-edition leftover must not start talking after a switch) and reports it as `pending`.
12. Tests: fakes for every branch, plus a slow real-model test.

### Review round (findings fixed)

13. Identity comes only from the header that passed the key check (`auth.matched_key`); a made-up `Authorization: Bearer` beside the real `X-API-Key` no longer becomes its own account. A request with no passing key has no principal (401).
14. `POST /voices/own` takes clips up to `VOX_REF_AUDIO_MAX_SECONDS` (30) plus one second of timer jitter and trims to exactly 30 s BEFORE the gate; the gate and the stored voice use that same file, so no audio becomes a voice without being judged. Longer clips are 422.
15. The voice carries a `consent-record:<id>` tag (hidden from clients). A review decision acts only on a voice that carries its own record id, and registering a new voice supersedes the older queued records for that name (rejected, reviewer `system`).
16. The two ungated clone paths, a per-request `reference_audio_b64` (`/synthesize`, `/synthesize-url`, `/sink/{key}/say`) and plain `POST /voices`, hang off ONE function, `consent.ungated_clone_allowed(principal)`: true in the self edition and for the root key, false for any other principal in hosted. Today every caller holds the root key, so nothing changes yet; per-customer keys flip it with no other edit.
17. A challenge lost in a race still writes a rejected record and keeps its audio; `GET /consent/reviews?status=all` (and any status other than `review`) returns the newest 200, oldest first within them; `VOX_OPERATOR_KEY` equal to `VOX_API_KEY` stops startup (and the review routes answer 503 meanwhile); review audio is served with its own content type.

## What was built

Server (`app/`):

- `consent.py` (rewritten): challenges, `Thresholds`, the pure `decide()`, `ConsentGate` (peek challenge, analyse in a worker thread, redeem, score and decide with the voiceprint race handled, write the record), review resolution and supersession, tag helpers `consent_state`, `synthesis_block_reason`, `record_tag`, and the single `ungated_clone_allowed` switch.
- `consent_store.py`: `MemoryConsentStore`, `PgConsentStore`.
- `asr.py` (the spike's phrase rule), `voiceprint.py` (TitaNet-large, VAD), `models.py`: the two model providers (injectable) and the model fetch.
- `auth.py`: `matched_key`, `principal_for`, `root_principal`, operator-key handling (the operator key opens `/consent/reviews*` only and may not equal the API key).
- `voices.py`: `set_tags`, `retag`.
- `main.py`: gate wiring in the lifespan (startup retag, background model preload), the 403 guard in `_resolve_voice`, principal-bound `POST /voices/consent/challenge`, `POST /voices/own` (optional `reference` file, at least 10 s), the review routes, `consent` on `VoiceOut`, new `/app-config` features `consent_verification` and `consent_reference_upload`.
- `migrations/004_consent.sql`: `consent_challenges`, `consent_records`, `account_voiceprints` (idempotent; rollback in the header).

CLI: `cli/voxxy/commands/consent.py`, client methods, `VOX_OPERATOR_KEY` / `VOX_OPERATOR_KEY_REF`.

Web (`app/web/ui/`): `sample.js` (optional longer sample), `make.js`, `api.js`, `picker.js`, `voices.js`, `app.css`.

Packaging: `faster-whisper`, `sherpa-onnx`, `numpy` in `pyproject.toml` and `uv.lock`; `compose.yml` (models volume, operator key, thresholds); `.env.template` (operator key reference); `.gitignore` (`models/`); `mise.toml` (`models:fetch`).

## Operator notes

- Apply the migration first (`mise run migrate`), then recreate the core. The first start downloads about 240 MB of models (TitaNet-large 101 MB, whisper base.en 139 MB); with `VOX_CONSENT_PRELOAD=1` it loads them in the background and `/voices/own` answers 503 until they are ready.
- Create the vault item behind `VOX_OPERATOR_KEY` (`.env.template` names `op://DeLoSecrets/voxxy-operator-key/credential`) before using the review queue. Until then the queue answers 503.
- Review audio is kept under `$VOX_VOICES_DIR/_consent` and survives voice deletion: it is the evidence behind a record.

## Caveats

- **Future customer keys must not reach the ungated clone paths.** Plain `POST /voices` (any audio, any tags, including `consent-verified`) and a per-request `reference_audio_b64` on `/synthesize`, `/synthesize-url` and `/sink/{key}/say` are the fleet's tools and carry no consent check. Both now call `consent.ungated_clone_allowed(principal)`; when per-customer keys ship, that function returns false for them and the web flow (`/voices/own`, `/voices/design`) is all they have. This is the first item of that story.
- **One shared key means one principal.** Today hosted has a single `VOX_API_KEY`, so every user shares one account voiceprint: a second person recording their own voice is rejected as "not the same person". This is correct once per-customer keys exist and wrong until then.
- The thresholds are the spike's, measured on phone, desk-mic and hand-named Wax clips and on synthesized readings, not on real browser consent recordings (none exist yet). Same-person scores here are a floor: a person reading into their own microphone should score higher. Recalibrate with `scripts/g5_calibrate.py` after about 50 reviewed real consent pairs.
- A clone beats the check: a clone made from a TV clip scored 0.87 to 0.90 against that clip, above accept, and a clone can read the sentence and code aloud. This is the deepfake limit product doc 4.1 already states; the review queue and the terms are the backstop.
- A mixed-speaker consent clip cannot be caught from the clip alone: the spike measured single-voice clips at a median internal consistency of 0.81 and two voices spliced together at 0.69, too close to threshold on. With no longer sample and no account voiceprint yet, only the ASR check and the review queue stand behind a first voice.
- Phone audio pulls different people closer together; the gate treats a speaker score as evidence, with `review` as the middle band. The spike suggests a second refusal in a row should go to review rather than block; that is not built.

## Deferred

- Recalibration on real consent pairs (about 50) and the "second refusal goes to review" rule.
- A web page for the operator queue (CLI and API only for now).
- Per-key voice namespaces and per-customer principals.
- Liveness against a played-back recording of the person (a recording of someone is not stopped by this gate; the random sentence and code make a pre-recorded one hard, not impossible).
