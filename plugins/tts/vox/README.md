# Vox Hermes TTS provider

A formal Hermes directory plugin for the self-hosted **Vox** HTTP service. It
registers the provider name **`vox`**, matching the fleet's existing
`tts.provider: vox` configuration, so Hermes resolves it through the TTS
registry rather than falling through to Edge.

## Behavior

- reads `tts.vox.base_url` (default `https://vox.delo.sh`)
- defaults to voice `rick` (`tts.voice` is passed by Hermes; `tts.vox.voice`
  and then `rick` are provider fallbacks)
- lists Vox voices from `GET /voices`
- checks usable engine health via `GET /healthz`, without raising on failure
- calls `POST /synthesize-url` for `ogg`/`opus`, downloads the returned
  OGG/Opus URL to a **local** `.ogg` file, and advertises
  `voice_compatible=True`
- calls `POST /synthesize` for WAV and uses `ffmpeg` only for explicit MP3 or
  FLAC requests; errors are surfaced as clean provider exceptions

`/synthesize-url` is intentionally used for voice delivery because Vox owns
its Telegram-compatible codec settings (48 kHz mono Opus in OGG). Hermes then
keeps the local OGG artifact as a native voice bubble.

## Install

```bash
mkdir -p ~/.hermes/plugins/tts
ln -s /absolute/path/to/voxxy/plugins/tts/vox ~/.hermes/plugins/tts/vox
hermes plugins enable tts/vox
```

Use this configuration (also in `templates/config.example.yaml`):

```yaml
plugins:
  enabled: [tts/vox]
tts:
  provider: vox
  voice: rick
  output_format: ogg
  vox:
    base_url: https://vox.delo.sh
    voice: rick
```

For a protected deployment, put the secret in `VOX_API_KEY` (or
`tts.vox.api_key`); it is sent as Bearer and `X-API-Key` authentication.

## Canonical-name migration

The initial untracked skeleton used `plugins/tts/voxxy`, provider `voxxy`, and
`VOX_URL`. It was never a shipped provider and does **not** match the existing
fleet configuration. This implementation deliberately makes both the plugin
key and provider id canonical **`vox`**:

- install path/key: `plugins/tts/vox` / `tts/vox`
- configured provider: `tts.provider: vox`
- service config: `tts.vox.*`

The implementation temporarily reads `tts.voxxy.*` only as a runtime bridge
for a local user who copied the early skeleton. It does not register `voxxy`;
change that configuration to `vox` and enable `tts/vox`. This avoids a second
registry name that could silently fall through to Edge.

Hermes supports a two-segment category layout for user plugins. Install this
plugin at `~/.hermes/plugins/tts/vox`, which registers the `tts/vox` key.

Do not define `tts.providers.vox` as a command provider: Hermes intentionally
makes a same-name command provider override the plugin.

## Isolated smoke check

```bash
HOME_DIR=$(mktemp -d)
mkdir -p "$HOME_DIR/plugins/tts"
ln -s /absolute/path/to/voxxy/plugins/tts/vox "$HOME_DIR/plugins/tts/vox"
printf '%s\n' 'plugins:' '  enabled: [tts/vox]' 'tts:' '  provider: vox' '  voice: rick' '  output_format: ogg' '  vox:' '    base_url: https://vox.delo.sh' > "$HOME_DIR/config.yaml"
HERMES_HOME="$HOME_DIR" HERMES_PLUGINS_DEBUG=1 hermes plugins list
```

Then run `text_to_speech` through Hermes or the isolated test in this plugin.
A healthy live synthesis creates a local file beginning with `OggS` and
containing `OpusHead`.
