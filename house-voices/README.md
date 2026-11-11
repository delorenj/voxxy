# House voices

Four synthetic voices that belong to nobody, for clinical text-to-speech:

| Voice | Description given to the model | Seed | Clip | Median pitch |
|---|---|---|---|---|
| `house-f1` | warm, clear woman in her thirties; calm, reassuring, steady pace | 1 | 13.6 s | 221 Hz |
| `house-f2` | older woman in her sixties; calm and kind, low pitch, slow, unhurried | 2 | 14.6 s | 202 Hz |
| `house-m1` | warm, clear man in his thirties; calm, reassuring, steady pace | 1 | 13.1 s | 128 Hz |
| `house-m2` | deep, steady man in his fifties; gentle, unhurried, clear diction | 4 | 12.8 s | 153 Hz |

All four are registered in Voxxy with tags `house`, `clinical_ok`, `synthetic` and `female` or `male`,
so `GET /voices` lists them. Each speaks English and Spanish. None is modelled on a real person.
The celebrity and real-person voices stay registered (other homelab callers use them); callers that
need a voice with no real person behind it pick one tagged `house`.

```bash
curl -sS https://vox.delo.sh/synthesize -H 'content-type: application/json' \
  -d '{"text":"Su cita está confirmada.","voice":"house-f1","language":"es","engines":["voxcpm"]}' \
  -D- -o out.wav        # x-vox-engine: voxcpm   x-vox-voice-source: registered
```

Send `engines: ["voxcpm"]` for these voices. Every registered voice also carries a VibeVoice reference
and, if VibeVoice is ever deployed again, would be routed to it first for English; VibeVoice is
research-only licensed and watermarks its output. VoxCPM2 is Apache-2.0.

## How they were made

VoxCPM2 "Voice Design": the text is `"(" + description + ")" + transcript`, with no reference audio, and
the model invents a voice that fits the description. The transcript is one English and one Spanish sentence
(`voices.toml`: `transcript_en`, `transcript_es`), so one clip holds the same voice in both languages and
registering it with that transcript as `prompt_text` (VoxCPM continuation cloning) keeps both languages'
phonetics in the prompt.

Everything that determines a clip is in `voices.toml`: description, transcript, `seed`, `cfg` 2.0,
`steps` 10, and the model weights (`openbmb/VoxCPM2`, `model.safetensors` sha256 `f7f964cf...`). The
engine's `seed` runs the sampler inside `torch.random.fork_rng` with `torch.manual_seed`, so the same
inputs on the same engine build and GPU give the same waveform. Regenerated clips were byte-identical
across separate runs (2026-11-11), and `voices.toml` records the sha256 of each.

```bash
python3 house-voices/build.py generate     # synthesize into house-voices/out/ and compare sha256
python3 house-voices/build.py all          # generate, then register (create or replace) all four
python3 house-voices/build.py check        # all four listed, tagged, with the right prompt_text?
uv run house-voices/evaluate.py            # English and Spanish through the registered voices
```

`build.py` is stdlib only and takes `--url` (default `$VOX_URL` or `https://vox.delo.sh`) and `--out`.
If a regenerated clip's sha256 differs (new engine build, GPU stack or weights), it exits 2 and does not
register: the clip is a valid voice of the same description but a different waveform. Run `evaluate.py`,
listen to it, then update the sha256 in `voices.toml`. If a description stops producing a good clip, sweep
seeds 1 to 8 with the same description and pick by the criteria below.

## How the seeds were chosen

Per description, 3 to 8 seeds were generated and scored on 2026-11-11 (faster-whisper `small` on the CPU,
NeMo TitaNet-large speaker embeddings, a plain autocorrelation pitch track):

- the clip itself: word error rate against its transcript, both languages present (some seeds spoke only
  the Spanish half and were dropped);
- length at most 15 s, the limit for a per-request clip, so a house clip is also a valid one;
- cloned English and Spanish test sentences from the clip, in both modes (with and without `prompt_text`):
  word error rate and detected language;
- TitaNet cosine similarity: each cloned sentence against its own clip, and each clip against the other
  voices, so the four stay distinct.

Results for the chosen four, through the registered voices (`evaluate.py`, seed 1234): word error rate 0.000
and detected language correct (English 0.97 to 1.00, Spanish 0.98 to 0.99) for every voice in both languages.
Cloned sentences (with `prompt_text`) scored 0.79 to 0.88 against their own clip in English and Spanish and
at most 0.41 against any other voice's clip (0.45 without `prompt_text`). Clip-to-clip similarity: `house-f1` to `house-f2` -0.03, `house-m1` to `house-m2` 0.35, and
0.13 to 0.26 between a female and a male voice. (Results were measured on the clips of the final four; the
candidates that were not chosen are not recorded.)

## Limits

- Judged by machine only. A listener, ideally a native Spanish speaker, has not reviewed them. The voices
  are designed against an English description, so the Spanish is intelligible and correctly detected, but
  whether the accent reads as native is not measured.
- `house-m2` was prompted "deep" and came out at about 153 Hz, higher than `house-m1`'s 128 Hz. It is
  distinct by timbre, not by depth.
- Reproducibility is bound to the engine build, GPU stack and weights; see above for what a mismatch means.
- The WAVs are not committed (`voices/` and `house-voices/out/` are ignored). They live in the Voxxy
  voices volume and regenerate from `voices.toml`.
