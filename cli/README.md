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
```

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
| model | `VOXXY_PERSONA_MODEL` | `persona_model` | `automaticai/personal/glm-5.3-flash` |
| API key | `VOXXY_PERSONA_API_KEY` | `persona_api_key_ref` (an `op://` reference, resolved with `op read` at call time) | none: errors with instructions |
| timeout (s) | `VOXXY_PERSONA_TIMEOUT` | - | `40` |

Raw keys are never stored in config. Example:

```toml
persona_api_key_ref = "op://DeLoSecrets/<item-uuid>/credential"
```
