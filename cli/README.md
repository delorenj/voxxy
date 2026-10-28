# voxxy CLI

Unified command-line interface for the vox-tts service.

See [docs/specs/voxxy-cli.md](../docs/specs/voxxy-cli.md) for the full spec and
[docs/specs/voxxy-cli.plan.md](../docs/specs/voxxy-cli.plan.md) for the task plan.

## Install (dev)

```bash
cd cli
mise x -- uv sync
mise x -- uv run voxxy --help
mise x -- uv run pytest -q
```

## Install (global)

```bash
mise x -- uv tool install --force /home/delorenj/code/voxxy/cli
voxxy --help
# with the clone/analysis stack (voice clone, voice candidates, voice add --auto):
uv tool install --editable "$HOME/code/voxxy/cli[clone]" --force
```

## Cloning a voice from a recording or from a description

The biggest quality lever for voxcpm is giving it the **exact transcript of the
reference clip** (`prompt_text`): it switches the model from voice-clone to
continuation mode. Measured on `arnold` (ECAPA speaker similarity vs the source):
first-15-s upload with no transcript 0.59, clean sentence-boundary cut 0.72, the same
cut **with** the transcript 0.82-0.85. 12-15 s clips scored the same as 22 s clips at
lower latency. The commands below automate that recipe.

They need the optional `clone` extra (faster-whisper, numpy, scipy, soundfile, yt-dlp),
which `voxxy health`, `speak` and friends never import:

```bash
uv tool install --editable "$HOME/code/voxxy/cli[clone]" --force
```

A missing dependency prints exactly that line. Whisper is CPU-hungry: run the heavy
commands through `heavy` (`heavy voxxy voice clone ...`).

### `voxxy voice add`: new flags

Plain `voxxy voice add FILE` is unchanged (first `--trim-seconds` of audio, no
transcript). The new flags are the high-quality path:

```bash
voxxy voice add talk.mp3 --auto --name brian             # transcribe, pick the cleanest 10-15 s, clean-export, set prompt_text
voxxy voice add talk.mp3 --start 12.6 --end 26.1 -n brian # you pick the window; the transcript is automatic
voxxy voice add clip.wav -n brian --prompt-text "the exact words spoken in the clip"
voxxy voice add talk.mp3 --auto --min-seconds 8 --max-seconds 12 -n brian
```

`--prompt-text` always wins over the automatic transcript. `--start` alone means
`start + --max-seconds`.

### `voxxy voice candidates PATH`

Ranks the cleanest reference-clip windows in a file and uploads nothing.

```bash
heavy voxxy voice candidates interview.wav --top 5
voxxy voice candidates talk.mp3 --json --markers /tmp/talk.markers.json
```

Flags: `--top N`, `--min-seconds`, `--max-seconds` (defaults 10/15), `--json`,
`--markers FILE` (also write the markers JSON), `--no-cache` (re-transcribe). Exit code 1
when no window passes the gates (the table then shows the best rejected windows and why).

How a window is judged (`voxxy/refclip.py`):

1. ffmpeg decodes to 16 kHz mono float32 (faster-whisper's own PyAV decode is avoided on
   purpose); faster-whisper (`VOXXY_WHISPER_MODEL`, default
   `deepdml/faster-whisper-large-v3-turbo-ct2`, CPU int8) gives word timestamps and
   probabilities. Transcripts are cached in `~/.cache/voxxy/refclip/`.
2. Pauses are runs of >= 80 ms where frame energy is 30 dB under the p90 speech level
   (relaxed in steps for noisy sources).
3. Candidates are word spans that start right after a pause and end right before one, with
   duration in `[min, max]`. A clip may not begin or end on a filler (uh/um/er), a
   low-probability word (< 0.5) or a cut-off fragment. A source shorter than `--min-seconds`
   is used whole.
4. Each window is scored (breakdown kept in the JSON): clarity (mean word probability),
   sentence boundaries (punctuation and pause length), SNR (speech vs gap floor),
   background (gap energy + tonality, i.e. music), speech density, filler count, clipping,
   and a **speaker gate**: ~2 s segments are embedded (MFCC with cepstral mean
   normalization), clustered (Ward), the cluster with the most speech time is the
   dominant speaker, and windows containing other clusters are rejected. The cluster count
   is the largest k whose labelling is temporally coherent, so one speaker stays one cluster.
   This MFCC gate is best-effort: it only finds the most talkative voice, and on a real
   multi-character compilation it reported one speaker for every window. A raw MFCC
   distance cannot tell a change of speaker from ordinary phonetic variation. So there is
   a second, real gate: **ECAPA one-voice** (`voxxy/speakergate.py`). The source is embedded
   in 2 s windows by the same speaker model the vox-tts scoring harness uses, run as a
   separate PEP 723 script (`voxxy/ecapa_embed.py`, via `uv run --script`, so the CLI itself
   never depends on torch; cached per source). Per window it measures *coherence* (how alike
   the sub-windows inside the candidate are) and *dominance* (closeness to the source's main
   voice). Measured on Family Guy audio: a one-character monologue has coherence 0.49-0.55
   (p10 0.34-0.44); a dialogue compilation 0.18-0.45 (p10 <= 0.24). Below 0.45 / 0.30 a
   window is rejected as "several voices". If uv/torch are unavailable the gate degrades
   with a note and `--yes` falls back to requiring `--pick`. Disable with
   `VOXXY_VOICE_GATE=off`. A **words-per-second** gate (< 1.2) additionally rejects windows
   that are energetic but not speech (laughter, gunshots, crowd): whisper finds no words in them.
5. The best ~8 distinct finalists are exported and **re-transcribed in isolation**
   (more accurate and punctuated than the whole-file pass); this settles edges (dangling
   last word, filler), checks the two transcripts agree, and its text becomes the
   `prompt_text`.
6. Top-K selection is non-overlapping; verified windows rank ahead of unverified ones, and
   the finalist slots go to different regions (windows sharing over half their time with an
   already chosen one are skipped).

Export is always a re-encode (stream-copy truncates): `ffmpeg -ss S -t D -i src -af
"highpass=f=55,afade=t=in:d=0.02,afade=t=out:st=<D-0.03>:d=0.03,loudnorm=I=-20:TP=-1.5:LRA=11"
-ac 1 -ar 24000 -c:a pcm_s16le`.

### `voxxy voice clone QUERY`

Describe who you want; get a voice and a persona.

```bash
heavy voxxy voice clone "the dog from Family Guy"            # interactive: confirm identity, audition, pick
heavy voxxy voice clone "Arnold Schwarzenegger" --yes --dry-run --no-persona
voxxy voice clone "Morgan Freeman" --pick 2 --name morgan2 --force
```

1. `web-search search -n 5 --json` plus the clone LLM resolve the intent to strict JSON
   (`display_name`, `franchise`, `voice_actor`, `slug`, `youtube_queries`,
   `persona_brief`, `confidence`); you confirm "Brian Griffin, from Family Guy" (edit or
   abort), unless `--yes`. Without `web-search` or without an LLM key it degrades to your
   own words.
2. `yt-dlp --flat-playlist -J ytsearch8:<q>` for each planned query, deduped, kept if 45 s
   to 40 min, ranked by the LLM for the target's own clean voice (title-keyword fallback;
   live hall events, reactions, dubs, covers rank last).
3. yt-dlp downloads the first `--max-minutes` (default 12) of audio as WAV into
   `~/.cache/voxxy/clone/<slug>/` (`source.wav`, `source.json` with url/title/channel/query/
   identity, `identity.json`). yt-dlp is the "ytmp3" of this host; `python -m yt_dlp` is the
   fallback when the binary is not on PATH.
4. `refclip` analysis writes `markers.json` (timestamp markers for the best clean windows).
   YouTube answers a burst of downloads with a "Sign in to confirm you're not a bot" wall.
   The CLI then retries with cookies from your browser (chrome, chromium, firefox, brave, in
   that order; the first that works is remembered for the run). Pin it with
   `VOXXY_YTDLP_COOKIES_FROM_BROWSER=chrome` (or `firefox:PROFILE`) or a cookies.txt in
   `VOXXY_YTDLP_COOKIES`; `VOXXY_YTDLP_COOKIES_FROM_BROWSER=none` forbids the automatic retry.
5. A weak video (no window passes the gates, or best score < 0.60) falls through to the next
   ranked one, up to `--max-videos` (default 3); the best attempt wins.
6. Interactive: top candidates with transcripts, audition (local playback; `-P/--player`,
   `--no-play`), choose. `--pick N` takes the Nth (1-10, the same list `markers.json`
   holds). `--yes` takes the best only when the ECAPA gate proved the window is one dominant
   voice AND (for a fictional character) the video title names the character or their actor
   ("Brian's apology speech"; "Family Guy scenes" does not count). Otherwise, or when the
   ECAPA gate could not run, `--yes` without `--pick` stops (exit 5) with the candidates and
   `markers.json` in the workdir. An unattended pick is still not auditioned: it prints a
   reminder. Re-running is cheap: the download and the cached transcript are reused.
   `--yes` also refuses an identity the LLM could not resolve (needs `--name`) and a
   low-confidence one (needs `--force`).
7. The single clip is exported to `<workdir>/<slug>.wav` and uploaded **with `prompt_text`**.
   The voice name defaults to the first token of the slug (`brian`); `--name` overrides. An
   existing voice is never silently overwritten (`--force`). `--dry-run` stops before any
   upload and writes nothing into the repo (a persona preview lands in the workdir).
8. Persona: the LLM writes `plugins/voice-persona/personas/<slug>.md` from an existing
   persona as the few-shot template, validated through the plugin's own loader, never
   overwriting an existing file without `--force-persona` (an existing one is reused), then
   associated with `set_voice_persona`. `--no-persona` skips.
9. Verification: one line is synthesized in the new voice and the engine must be `voxcpm`;
   anything else prints a loud "NOT A CLONE" panel (ElevenLabs ignores reference audio) and
   exits 2. The audio URL is printed and played when interactive (on `$VOX_SINK` /
   `--sink` if someone is listening).
10. A summary panel: identity, video, window, transcript, scores, voice, persona, workdir.
    `<workdir>/clip.json` records the choice (window, transcript, video, voice, persona,
    engine, audio URL); the chosen wav stays in `candidates/<k>-<id>/source.wav` with
    `<workdir>/source.wav` linked to it.

Exit codes: 0 ok, 1 failure, 2 degraded (voice created but persona/verification fell
short) or bad invocation, 3 server unreachable, 5 validation or refusal to overwrite.

### `voxxy persona generate`

```bash
voxxy persona generate brian-griffin --character "Brian Griffin from Family Guy" --voice brian
voxxy persona generate stewie-griffin -c "Stewie Griffin, Family Guy" --dry-run
```

The same generator `voice clone` uses. Web-searches the character, writes the persona
file (frontmatter, character bible, "Rules for rewriting" forbidding dropped numbers /
names / paths / commands / warnings / action items, 2-3 before/after examples; accent by
word choice, never phonetic spelling), validates it, refuses to overwrite without
`--force`, and (with `--voice`) puts the voice in `voices:` and associates it if the voice
exists.

### Clone LLM configuration

Same OpenAI-compatible mechanics as the persona LLM, separate settings so you can point the
(rare, quality-over-latency) clone steps at a stronger model. Each unset clone setting
inherits the persona one, except the model. Precedence, highest first, for the URL and key:
`VOXXY_CLONE_*` env, `clone_*` in config.toml, `VOXXY_PERSONA_*` env, `persona_*` in
config.toml, built-in default. The model is `VOXXY_CLONE_MODEL`, then `clone_model`, then
`automaticai/auto`.

| Setting | Env | `~/.config/voxxy/config.toml` | Default |
| --- | --- | --- | --- |
| base URL | `VOXXY_CLONE_BASE_URL` | `clone_base_url` | the persona base URL |
| model | `VOXXY_CLONE_MODEL` | `clone_model` | `automaticai/auto` (does not inherit the persona model) |
| API key | `VOXXY_CLONE_API_KEY` | `clone_api_key_ref` (an `op://` reference) | the persona key |
| timeout (s) | `VOXXY_CLONE_TIMEOUT` | - | `120` |

Other knobs: `VOXXY_WHISPER_MODEL`, `VOXXY_WHISPER_LANGUAGE`, `VOXXY_WHISPER_THREADS`,
`VOXXY_CLONE_DIR` (workdir root).

## Voice personas

A persona is a markdown file under `plugins/voice-persona/personas/` (frontmatter:
`name`, `voices`, `temperature`, `description`; body: the character bible). The
Hermes `voice-persona` plugin uses them to rewrite spoken text in character. The
CLI drives the same plugin module, loaded by file path from the project root
(`--project`, `$VOXXY_HOME`, `config.toml`, or walking up from cwd), so the CLI
and Hermes share one prompt, sanitizer and fact-preservation check.

Which persona does a voice get? Highest first:

1. the **voxxy record** on the server (`voices.persona`), set with `voxxy voice persona`
2. the persona file's own `voices:` frontmatter (default mapping)

A record of `none` means "explicitly no persona" and suppresses the frontmatter
default. No record means "ask the persona files".

```bash
voxxy voice persona peter                 # show record + effective persona and its source
voxxy voice persona peter peter-griffin   # associate (warns, still sets, if the id is not bundled)
voxxy voice persona peter none            # explicitly none
voxxy voice persona peter --clear         # remove the record
voxxy voice list                          # Persona column (frontmatter defaults shown dim)

voxxy persona list [--json]               # bundled personas + voices mapped to each, labelled voxxy/frontmatter
voxxy persona show ID [--raw]
voxxy persona path
voxxy persona generate brian-griffin -c "Brian Griffin from Family Guy" -v brian   # LLM-authored, see above
voxxy persona rewrite peter-griffin "The deploy finished in 42 seconds."
echo "disk is 91% full" | voxxy persona rewrite --voice carlin
voxxy persona rewrite peter-griffin "text" --json   # {original, rewrite, accepted, reason, persona, model, prompt_version, elapsed_s}
```

`persona rewrite` is a dry run (no audio). It exits non-zero and prints the
reason when the plugin's validation rejects the rewrite (too long/short, dropped
numbers) or the LLM call fails.

### `voxxy speak --persona`

```bash
voxxy speak -v peter --persona auto "deploy finished"        # the voice's effective persona
voxxy speak --persona george-carlin "deploy finished"        # an explicit persona
VOX_PERSONA=auto voxxy speak -v peter "deploy finished"
```

Off by default: `speak` is used by scripts and never gains LLM latency silently
(`VOX_PERSONA=off` / `none` disables it again). It is fail-open: on any problem
(no persona for the voice, LLM error, rejected rewrite) the original text is
spoken and a dim warning goes to stderr. The rewrite happens locally before
synthesis, so it works with play, `--raw`, `--out`, sinks and `--via` (the remote
receives the rewritten text).

### Persona LLM configuration

The CLI calls an OpenAI-compatible `POST {base}/chat/completions`.

| Setting | Env (wins) | `~/.config/voxxy/config.toml` | Default |
| --- | --- | --- | --- |
| base URL | `VOXXY_PERSONA_BASE_URL` | `persona_base_url` | `https://api.automaticai.io/v1` |
| model | `VOXXY_PERSONA_MODEL` | `persona_model` | `automaticai/auto/fast` (router alias, never a named model) |
| API key | `VOXXY_PERSONA_API_KEY` | `persona_api_key_ref` (an `op://` reference, resolved with `op read` at call time) | none: errors with instructions |
| timeout (s) | `VOXXY_PERSONA_TIMEOUT` | - | `40` |

Raw keys are never stored in config. Example:

```toml
persona_api_key_ref = "op://DeLoSecrets/<item-uuid>/credential"
```
