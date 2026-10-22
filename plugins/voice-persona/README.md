# voice-persona (Hermes plugin)

An optional enrichment pass between "the response text" and "the TTS audio". When enabled, the
text that is about to be spoken is rewritten **once, as a whole,** as a character would say it
(Peter Griffin, George Carlin, Dr. Steve Brule, or any persona file you write), without losing a
single fact. It is provider-agnostic: it works with `vox`, `elevenlabs`, `edge`, anything.

It is side-effect free, fail-open, grounded (no invented facts; numbers must survive) and versioned
(`PROMPT_VERSION`, logged on every call). On any problem the original text is spoken.

## Where it sits

```
response -> prepare_spoken_text -> transform_tts_text [voice-persona] -> prepare_spoken_text -> chunk -> provider
```

The hook `transform_tts_text` fires in Hermes `tools/tts_tool.py::text_to_speech_tool`, after the
text is normalized and before it is split into chunks, so the rewrite sees the whole response once.
Core re-normalizes an accepted replacement. Callback kwargs: `text`, `provider`, `voice`, `platform`.
Core treats a non-string, blank, or exception as "no change".

## Install

```bash
ln -s /home/delorenj/code/voxxy/plugins/voice-persona ~/.hermes/plugins/voice-persona
```

```yaml
# ~/.hermes/config.yaml (or the profile's config.yaml)
plugins:
  enabled: [voice-persona]
```

## Configuration

Read at call time from the active profile's config (`hermes_cli.config.load_config`), so the
multiplexed gateway serves every profile its own persona.

```yaml
tts:
  persona:
    enabled: true            # master switch; default false => the plugin is a no-op
    name: peter-griffin      # bundled persona id (personas/<name>.md), optional
    file: PERSONA.md         # OR a path; relative paths resolve against the profile HERMES_HOME
    auto_from_voice: true    # default true: with no name/file, use the bundled persona whose
                             # frontmatter `voices:` contains the configured TTS voice
    timeout_seconds: 25      # hard bound on the rewrite; on expiry the original text is spoken
    max_ratio: 1.8           # reject rewrites longer than input*max_ratio (+40 chars) -> original
    min_ratio: 0.5           # reject rewrites shorter than input*min_ratio -> original
```

Resolution order: **file > name > auto_from_voice**. A missing persona is a no-op (logged once),
never an error.

### auto_from_voice mapping

| `voice` kwarg (case-insensitive) | persona |
| --- | --- |
| `peter` | `peter-griffin` |
| `carlin` | `george-carlin` |
| `brule` | `steve-brule` |

So an agent whose Vox voice is `peter` talks like Peter with only `tts.persona.enabled: true`.
Any other voice (e.g. `rick`) resolves to nothing and is spoken as-is.

### Profile-local override

Drop a `PERSONA.md` in a profile's `HERMES_HOME` and set `file: PERSONA.md`. It wins over `name` and
the voice mapping, and applies only to that profile.

## Persona file format

Markdown with optional YAML frontmatter:

```markdown
---
name: Peter Griffin        # display name used in the prompt (default: file stem)
voices: [peter]            # TTS voice ids that auto-select this persona
temperature: 0.8           # optional sampling temperature (default 0.7)
description: one line
---
# Character bible (free-form)
Idiosyncrasies, catchphrases, humor, worldview, speech tics, pronunciation/grammar rules.

## Rules for rewriting
What to inject, and what to NEVER drop or alter.

## Examples
Before/after pairs whose "after" keeps every fact of the "before".
```

The body goes to the model verbatim as the character bible. To write a new persona: copy a bundled
one, change the bible, keep a "Rules for rewriting" section that names the facts that must survive
(numbers, names, paths, commands, warnings, action items), and add 2-3 honest before/after examples.
Bundled personas live in `personas/`; for a private one use `file:`. George Carlin has a profanity
dial documented in his file (`mild-to-moderate` by default, since the audio goes to Telegram).

## How a rewrite is validated

One auxiliary LLM call (`agent.auxiliary_client.call_llm(task="tts_persona", ...)`). Output is
cleaned of reasoning blocks, "Sure, here's..." preambles and wrapping quotes, then rejected (original
text spoken) if it is empty, longer than `max_ratio`, shorter than `min_ratio`, or drops more than 15%
of the digit runs in the source (so one missing number out of a handful is already a rejection).
Nothing is cached to disk; parsed personas are cached in memory by (path, mtime).

## Limits

- **Whole-file TTS only.** Gateway auto-TTS, voice replies, CLI voice, the web audio router and the
  `text_to_speech` tool converge on `text_to_speech_tool`. The sentence-level streaming TTS path
  (`gateway/run_turn.py`, `tools/tts_streaming.py`) cannot rewrite a whole response and bypasses personas.
- **Adds one auxiliary LLM call of latency** (typically a few seconds, bounded by
  `timeout_seconds`; core also bounds the hook with `tts.transform_timeout_seconds`, default 45).
- Digits are kept as digits; the guard cannot verify spelled-out numbers or names.

## Choosing the model for the pass

`tts_persona` is a normal auxiliary task. With nothing configured it follows `provider: auto`, the
main route. Point it at something cheaper and faster:

```yaml
auxiliary:
  tts_persona:
    provider: automaticai
    model: automaticai/personal/glm-5.3-flash   # any fast, non-reasoning model
    timeout: 25
```

Use the configured gateway or provider; never hardcode keys in this plugin.

## Tests

```bash
cd /home/delorenj/code/voxxy && mise x -- uv run pytest plugins/voice-persona/tests -q
```
