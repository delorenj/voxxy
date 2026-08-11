# Vox Hermes TTS provider — shipped design

## Decision

The canonical Hermes provider and plugin name is **`vox`**, not `voxxy`.
The fleet already declares `tts.provider: vox`; the exact plugin registry lookup
uses that string, so retaining `voxxy` would reproduce the reported unknown
provider → Edge fallback. The shipped directory is `plugins/tts/vox`, whose
plugin key is `tts/vox` and whose provider `.name` is `vox`.

The original untracked `plugins/tts/voxxy` skeleton was renamed rather than
kept as a second plugin. The implementation reads `tts.voxxy` only as a
narrow runtime migration bridge for its service URL/voice; it never registers
an alias. Operators should migrate to:

```yaml
tts:
  provider: vox
  voice: rick
  output_format: ogg
  vox:
    base_url: https://vox.delo.sh
    voice: rick
plugins:
  enabled: [tts/vox]
```

## Contract

This plugin implements Hermes' actual `agent.tts_provider.TTSProvider` ABC and
registers through `PluginContext.register_tts_provider()`. Hermes dispatches
unknown, non-command providers via `agent.tts_registry`; provider exceptions
reach the normal JSON error envelope. The plugin cannot shadow built-ins and a
same-name command provider has intentional precedence.

For `ogg`/`opus`, the provider posts to Vox `POST /synthesize-url`, downloads
the returned cache URL, validates OGG/Opus bytes, and returns a local `.ogg`
path. `voice_compatible=True` tells Hermes to deliver it as a voice bubble.
For WAV it calls `POST /synthesize`; MP3/FLAC use local ffmpeg conversion or
return a correctly suffixed WAV fallback when ffmpeg is unavailable.

## Verification scope

Tests cover manifest discovery through a real isolated `HERMES_HOME`, registry
registration/resolution for `tts.provider: vox`, config-derived `rick`, voice
listing, native local OGG/Opus synthesis, and clean upstream failure behavior.
The test suite includes an opt-in live check against `https://vox.delo.sh`;
when network is not explicitly enabled it reports the exact skip reason rather
than claiming a mocked request is live proof.
