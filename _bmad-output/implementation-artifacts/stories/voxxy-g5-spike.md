# G5 calibration spike: speaker match, ASR model, phrase rule

Date: 2026-11-23. Status: done (spike; no app code changed).
Script: `scripts/g5_calibrate.py` (isolated `uv run --with ...`, never imported by the app).
Data and results: `~/.local/state/voxxy/g5/` (`score-*.json`, `phrase-*.json`, `asr-bench-*.json`).
Logs: `~/.local/state/voxxy/test-runs/g5-*.log`.

Level (pillar `belsky-doctrine`): these numbers belong to Voxxy's one consent gate, so they
apply to every path that makes a likeness from a human recording. Today that is the own-voice
recording; a later upload or clone path uses the same gate and the same thresholds. One config
holds them (`VOX_CONSENT_ACCEPT`, `VOX_CONSENT_REJECT`, ...).

## Verdict

| Decision | Choice |
|---|---|
| Speaker model | **NeMo TitaNet-large, sherpa-onnx, CPU.** `https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_large.onnx`, 101,405,493 bytes, sha256 `d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f699f3425daca8e77` (downloaded and checked 2026-11-23; the same file as `~/.cache/phonograph/`) |
| Before embedding | **VAD-trim both clips** (silero, as bundled with faster-whisper: no extra model file) and require at least 6 s of voiced speech in the consent clip and 8 s in a reference |
| Speaker thresholds (TitaNet-large, raw cosine) | **ACCEPT >= 0.70**, **REFUSE < 0.10**, review queue in between |
| ASR | **faster-whisper `base.en`, int8, CPU, in-process**, timestamps on (`without_timestamps=False`), no `initial_prompt` or hotwords |
| Phrase rule | sentence score >= **0.80** and code within **1 edit** (rule below) |

The working tree's placeholders are `accept 0.65 / reject 0.40` with TitaNet-small. Reject at 0.40
would refuse a third of genuine people on real recordings (33% of same-person phone pairs, 26%
of account-voiceprint checks with TitaNet-large). Use 0.10.

## 1. Speaker embedding

### Data

Nothing here is a browser consent recording; none exist yet. Ground truth comes from Wax, in
three tiers kept apart:

| Tier | What | Truth | Size |
|---|---|---|---|
| phone | channel-separated calls: channel A is the owner, channel B the contact the caller ID names | by construction, independent of any embedding | 71 calls, 6 people |
| room | diarized desk or phone-mic recordings, speaker labelled by Wax | Wax labelled it with TitaNet-large at 0.60, so labels carry diarization slips | 85 recordings, 19 people |
| clips | `~/d/Voices`, one clip per registered voice, names and merges set by a person | human; merged clips were registered as new voices first, so same-person leans low | 46 clips, 33 people |

From each recording and speaker the script cuts a consent window (6.5 to 12 s of voiced speech,
median 9) from the first turns and a reference window (9 to 28 s, median 18) from the last turns.
Consent windows also go through an Opus 32 kb/s round trip, because the browser records via
MediaRecorder. Pairs: consent against reference, same recording ("same session"), other
recordings of the same person ("cross-recording"), other people. "Voiceprint" pairs score a
consent window against the mean of up to 5 references from the person's other recordings (step 5).

### Results (cosine on VAD speech, Opus consent side)

Same person (min / p10 / median):

| Pairs | n | TitaNet-large | TitaNet-small |
|---|---|---|---|
| same session, phone | 82 | 0.108 / 0.459 / 0.637 | 0.329 / 0.599 / 0.735 |
| same session, room | 107 | -0.087 / 0.132 / 0.627 | -0.029 / 0.161 / 0.627 |
| cross-recording, phone (independent truth) | 1820 | -0.045 / 0.271 / 0.471 | -0.006 / 0.319 / 0.544 |
| cross-recording, room | 1268 | -0.177 / 0.062 / 0.440 | -0.221 / 0.075 / 0.432 |
| cross-channel, room and phone | 2250 | -0.146 / -0.008 / 0.222 | -0.121 / 0.005 / 0.180 |
| curated clips | 29 | -0.048 / 0.003 / 0.292 | -0.002 / 0.034 / 0.285 |
| account voiceprint | 170 | -0.105 / 0.186 / 0.538 | -0.070 / 0.150 / 0.570 |

Different people (median / p90 / p99 / max):

| Pairs | n | TitaNet-large | TitaNet-small |
|---|---|---|---|
| phone | 4800 | 0.148 / 0.347 / 0.500 / 0.648 | 0.271 / 0.457 / 0.585 / 0.704 |
| room (other recordings) | 10022 | 0.107 / 0.429 / 0.759 / 0.882 | 0.148 / 0.424 / 0.736 / 0.920 |
| cross-channel | 15298 | 0.078 / 0.291 / 0.565 / 0.672 | 0.106 / 0.276 / 0.528 / 0.694 |
| curated clips | 1006 | 0.088 / 0.279 / 0.619 / 0.740 | 0.096 / 0.296 / 0.603 / 0.726 |
| account voiceprint | 1899 | 0.109 / 0.355 / 0.597 / 0.769 | 0.155 / 0.418 / 0.579 / 0.761 |

Equal error rate:

| Set | Large | Small |
|---|---|---|
| phone (independent truth) | **14.5%** | 19.9% |
| room | 28.5% | 28.1% |
| cross-channel | **34.8%** | 39.2% |
| curated clips | 34.5% | 33.9% |
| account voiceprint | **16.5%** | 19.4% |
| phone, large, without VAD | 30.0% | |

The room tail is mostly label noise and room acoustics: 66 of the top 100 different-person room
pairs are the owner against one frequent visitor, both taken from the owner's own room
recordings. The curated-clip false matches above 0.70 (3 of 1006) all pair the owner's clips from
one recording device with one other person's clips.

### Bands at the recommended thresholds (TitaNet-large)

| Set | Wrongly accepted at >= 0.70 | Genuine auto-accepted at >= 0.70 | Genuine refused at < 0.10 | Impostors refused at < 0.10 |
|---|---|---|---|---|
| phone | 0 / 4800 | 5% | 1.3% | 33% |
| same session | | 32% | 5.3% | |
| room | 2.4% | 15% | 15% | 48% |
| cross-channel | 0 / 15298 | 0% | 30% | 57% |
| curated clips | 0.3% (3) | 3% | 24% | 54% |
| account voiceprint | 0.05% (1) | 12% | 7.1% | 47% |

For comparison, the placeholders 0.65 / 0.40 give: wrongly accepted phone 0, clips 0.9%, voiceprint
0.3%; genuine refused phone 33%, room 47%, voiceprint 26%, same session 17%.

What this says: a ~9 s consent read against a 10 to 30 s clip from another recording is a weak
signal on real audio. At a false-accept rate near zero, most genuine pairs land in review. That
fits N13 (Jarad reviews the borderline queue while subscribers are few). Browser reads into the
person's own mic will score higher than these conversational phone and desk recordings, so treat
the same-person rows as a floor. Recalibrate on the first ~50 reviewed real consent pairs.

### Cost (sherpa-onnx 2.0.3, CPU; host load average 25 to 30 on 32 threads, so numbers run high)

| | Large | Small |
|---|---|---|
| File | 101.4 MB | 40.3 MB |
| Load | 0.2 to 0.34 s | 0.1 s |
| 10 s of speech, 1 / 4 threads | 355 / 127 ms | 105 / 54 ms |
| 30 s of speech, 1 / 4 threads | 918 / 354 ms | 240 / 193 ms |
| Peak RSS (30 s input, 4 threads) | 476 MB | 384 MB |

### Why large

- It is better where the truth is independent: EER 14.5% against 19.9% on phone pairs, and 34.8%
  against 39.2% across channels. At the same 1.3% genuine-refusal rate on phone pairs, large
  refuses 33% of impostors and small 20%. Room and clips are a tie.
- Small spreads different people higher (phone median 0.27 against 0.15). The band between
  "clearly someone else" and "same person" is narrower with small.
- The cost is trivial for a check that runs once per voice: about 60 MB more disk, about 90 MB
  more RSS, about 70 ms more per 10 s embedding at 4 threads.
- Wax runs TitaNet-large. The local Wax pre-check (N16) and the hosted gate then share one score
  scale. Not verified: that Wax's NeMo/PyTorch vectors equal the sherpa-onnx export's.

### Other findings

- **VAD is required.** Whisper segment spans and diarized turns are often more than half silence.
  Two halves of one window scored a median of 0.5 before VAD. Phone EER fell from 30.0% to 14.5%
  with VAD. The gate must embed voiced speech only.
- Opus round trip on the consent side: same-person delta median 0.00, p5 -0.10. No measurable
  harm (phone EER 14.5% with Opus, 16.8% without).
- Taking the cohort's mean voice out (Wax's domain centering, global mean) did not help: EER 29.2%
  against 26.7% overall. Leave it out.
- A "one voice only" check using chunk-to-centroid consistency does not separate clips: one voice
  median 0.81 (min 0.58), two voices spliced half and half median 0.69 (max 0.82). Not worth
  building for v1.
- **Clones.** A live-server clone made from a TV clip scored **0.87 to 0.90** against that same clip,
  above ACCEPT. Clones of three other people scored medians of 0.04 to 0.39 (max 0.68) against
  those people's other recordings. A clone can also read the sentence and code aloud. This is the
  limit 4.1 already states: the gate stops casual misuse, not a deepfake played into the mic.
- People differ. Cross-recording phone medians run from 0.21 (one frequent caller, maybe a shared
  phone) to 0.51 (the owner). One global threshold will always route some people to review.

## 2. ASR (faster-whisper 1.2.1, CPU, int8)

Real 10.7 s speech clip (Opus), beam 5, timestamps on, same host load:

| Model | model.bin | Peak RSS | Latency, 1 thread | Latency, 4 threads | Phrase pass (75 readings) |
|---|---|---|---|---|---|
| tiny.en | 73 MB | 255 MB | 0.90 to 0.96 s | 0.39 to 0.61 s | 69 |
| **base.en** | 139 MB | 340 MB | 1.46 to 1.72 s | 0.69 to 1.08 s | **72** |
| small.en | 462 MB | 622 MB | 4.2 to 4.8 s | 3.1 s | 72 |

The 3 base.en and small.en misses are one TTS clip whose code the synthesizer garbled ("Oat 4-2,
9-5-T" for "842 950"). All three models hear the same garble, so base.en and small.en pass every
intelligible reading. tiny.en also loses 3 good readings. Pick **base.en**: same pass rate as
small.en at a third of the latency and about half the RAM. `VOX_ASR_MODEL=small.en` stays the
fallback if real readings show ASR misses.

**Timestamps must stay on.** With `without_timestamps=True`, Whisper stopped at the sentence's full
stop and dropped the trailing code: 50/75 (tiny.en), 53/75 (base.en), 67/75 (small.en).
Timestamps on: 69 / 72 / 72. `vad_filter=True` gives the same result as plain timestamps. A generic
digits hint as `initial_prompt` gave nothing extra. Never put the challenge itself in
`initial_prompt` or hotwords, because that biases the transcript toward what the check wants to
hear. The working tree's `beam_size=1` was not measured; beam 5 is what passed.

**In-process, not the 127.0.0.1:8766 server.** That server is Holocene's: large-v3-turbo on CUDA,
float16, one host's GPU. A self-hosted buyer has no such server, and the CPU-only core container
cannot rely on a host loopback port. In-process base.en is ~340 MB, needs no extra service, and
its pinned revision makes the check reproducible. An optional OpenAI-compatible
`VOX_ASR_URL` could come later for hosts that have a GPU server. The gate must not depend on it.

Cost of the whole gate per consent on this CPU, about 1.5 to 2 s: VAD in milliseconds, base.en
about 1 s, two TitaNet-large embeddings about 0.13 to 0.35 s each.

## 3. Phrase rule

Normalise the transcript and the sentence the same way:

1. Lowercase. Turn hyphens and slashes into spaces. Drop apostrophes and punctuation. Join digit
   groups split by commas (`482,917`). Split letter-digit joins. Fold British spellings
   (harbour to harbor).
2. Fold spoken numbers to digit tokens: units (zero, one ... nine); "oh" and "o" as 0 only next to
   another digit; teens; tens plus unit ("eighty-two" to 82); "N hundred [and] M" to three digits;
   "double / triple X". Common homophones (for 4, to/too 2, ate 8, won 1) count only between two
   digits.

**Sentence check:** a semi-global word alignment of the sentence against the transcript. Extra
words before and after (the code, "um", a restart) are free. Inside the span every substitution,
insertion or deletion costs 1. A token pair matches when it is identical, or has 4+ letters and a
character similarity of at least 0.8. Score = 1 - edits / sentence words. **Pass at >= 0.80.**
Review between 0.50 and 0.80, fail below 0.50.

**Code check:** concatenate every digit token in transcript order into one stream. The 6-digit code
must sit within **edit distance 1** of a substring of that stream. The code proves freshness (the
recording was made after the challenge was issued), not a secret, since it is on screen. One edit
of tolerance accepts about 55 of 10^6 codes by chance.

Measured on 25 synthesized readings x 3 variants (clean, Opus, Opus plus 10 dB room noise) x
3 models, codes read as digits, grouped ("four eighty-two, nine seventeen") and raw:

- sentence score of a genuine reading: min 0.95, almost all 1.0
- the same audio checked against another challenge sentence: max 0.235, median 0.06
- half of the sentence read: 0.47
- code edits of a genuine reading (base.en): 0 for 66, 1 for 6, 2 to 3 only for the garbled TTS clip
- the same audio checked against a wrong code: at least 4 edits in all 225
- false accepts: 0 of 150 negative checks per model, in every decode mode

`python scripts/g5_calibrate.py selftest` holds 15 cases (digits, grouped, "hundred", "oh" and
"double", homophones, one ASR slip, one wrong digit, two wrong digits, no code, code only, other
sentence, half sentence); all pass. Synthesized speech is not a person: it tests the transcript
rule only.

## 4. Deltas for the G5 build

- Default speaker model: TitaNet-large, the URL and sha256 above. First run downloads 101 MB into
  `VOX_MODELS_DIR`.
- `VOX_CONSENT_ACCEPT=0.70`, `VOX_CONSENT_REJECT=0.10`. Use the same pair for step 4 (consent
  against reference) and step 5 (consent against the account voiceprint, the mean of verified
  consent embeddings).
- VAD (`faster_whisper.vad.get_speech_timestamps`, min silence 300 ms, pad 100 ms) before
  embedding. Refuse with "record a little longer" under 6 s of voiced consent speech.
- ASR: keep timestamps on. Beam 5, or measure beam 1 before shipping it.
- Phrase thresholds: ok at 0.80 to 0.85 (genuine min was 0.95), review at 0.50.
- A refusal invites a re-record. A second refusal goes to review, so the model alone never
  permanently blocks a person.
- Own-voice path: the consent clip is the reference, so step 4 is trivially 1.0. The real checks
  there are the phrase (live and fresh) and, from the account's second voice on, the voiceprint.
- Record every score, the model id and the reviewer's verdict in the consent record. Recalibrate
  after about 50 reviewed real pairs.

## 5. Limits

- No browser consent recordings existed. Phone and desk-mic conversation make same-person scores
  pessimistic. Different-person scores are realistic to pessimistic: one room's acoustics pull
  people together.
- The room tier is labelled by Wax's own TitaNet-large at 0.60. The phone tier is the honest one.
- ASR was tested on synthesized readings only; the latency clip was real speech.
- Latency was measured while the host ran at load average 25 to 30 on 32 threads.
- Wax's NeMo TitaNet-large against this ONNX export: same weights, vectors not compared.

## Reproduce

```bash
E=(uv run --no-project --isolated --with sherpa-onnx --with faster-whisper --with soundfile --with numpy python -I scripts/g5_calibrate.py)
heavy $E build
VOX_API_KEY=$(op read op://DeLoSecrets/mpxfavdfzeqh7ahqc2r3qa6aki/credential) $E synth \
  --clones damian=voice-849669 chaya=voice-d38edd carlin=voice-8f11d2 brule=voice-e0fd2d
heavy $E embed --model large; heavy $E embed --model small
$E score --consent opus; $E latency
$E asr-bench; heavy $E phrase --mode timestamps; $E selftest
```

The windows under `~/.local/state/voxxy/g5/windows` (237 MB) are cuts of private recordings.
Delete them when the numbers are no longer needed.
