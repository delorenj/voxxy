# Voxxy in HeyMa: integration scope

Status: in build, 2026-11-23. Jarad answered section 6 the same day ("I want to
start immediately"); see section 7 for what is built.
Asked for: Jarad (2026-11-23) wants to package Voxxy once it has a polished
interface and expects it to fit the HeyMa ecosystem. Crank asked for this scope
first, with interface-direction choices parked for Jarad.

Level (pillar `belsky-doctrine`): the request names Voxxy. The plan covers **any
product joining the HeyMa umbrella** (one contract, section 4), with Voxxy as the
first product to follow it after the original four.

## 1. Where things stand (verified 2026-11-23)

### Voxxy (speech out: text to a speaker)

| Fact | Where |
|---|---|
| FastAPI + FastMCP core on CPU, engines in their own GPU containers (`voxcpm`, `vibevoice`), ElevenLabs as remote fallback, Postgres voice profiles, OGG/Opus URL cache | `README.md`, `docs/specs/engine-decoupling.md` |
| Live at `https://vox.delo.sh`; `/healthz` answers `{"status":"ok","engines":[voxcpm ready, elevenlabs ready]}` | probed 2026-11-23 |
| HTTP: `/synthesize` (WAV), `/synthesize-url` (OGG URL), OpenAI-compatible `/v1/audio/speech` and `/v1/models`, `/voices` CRUD | `app/main.py` |
| **Sink relay**: `GET /sink/{key}/events` (SSE), `POST /sink/{key}/play`, `POST /sink/{key}/say`. `voxxy listen --key <host>` on a desk machine plays whatever any caller sends to that key | `app/sink.py`, `cli/voxxy/commands/listen.py` |
| `voxxy` CLI (Typer, `uv tool install`): `daemon`, `engine`, `voice`, `voice_clone`, `persona`, `speak`, `listen`, `health --json` (exit 0 ok, 2 degraded, 3 unreachable) | `cli/`, `docs/specs/voxxy-cli.md` |
| Optional API-key guard exists (`VOX_API_KEY`, `app/auth.py`) but is **not enforced in production**: `GET /voices` answers 200 without a key | `docs/plans/voxxy-api-key-and-wrapper.md`, probed 2026-11-23 |
| Consumers today: the Hermes fleet (`tts.provider: vox`, plugin `plugins/tts/vox`), Holocene's inbox-watcher replies (`MEMO_VOX_URL`), MCP clients. Node-RED is retired (package removed 2026-11-23) | `docs/plans/hermes-voxxy-tts-plugin.md`, `holocene/services/inbox-watcher/memo_common.py` |
| No web UI or TUI ("deferred" in the CLI spec). The only polished surface is the CLI | `docs/specs/voxxy-cli.md` section 1 |
| `docs/monetization-strategy.md` (2026-04-26) proposes Free/Hobbyist/Pro/Enterprise tiers and a voice marketplace. It is aspirational and predates the current fleet | the file |
| In flight: the character-sounds spec (intro/outro clips, interjections per character) | `_bmad-output/specs/spec-character-sounds/` |
| No `.launch.yaml`; Products lists voxxy as "missing" | Holocene `/products` |

### HeyMa (speech in: microphone to text)

| Fact | Where |
|---|---|
| HeyMa is the umbrella over four products: Wax (record, archive, transcribe, enrich), Vinyl (dictation), Telephony (shadow mode), Phonograph (hosted translation) | `~/HeyMa/docs/heyma-shell.md` |
| One client per host: `heyma status --json` is a registry of **adapters**. Each one calls only its product's CLI or endpoint and returns `{id, installed, running, health, summary, actions}` | `components/heyma-shell/heyma_shell/adapters.py`, `registry.py` |
| One Linux tray (`heyma-tray.service`): red while a microphone is hot, yellow when an installed section is degraded (debounced), green otherwise. One section per installed product | `model.py`, `tray_ui.py` |
| `HeyMa.app` on the Mac: native Swift, Developer ID signed, same section model, **no Python in the shell** | `components/heyma-mac/` |
| Planned: one settings window (S3), `heyma setup` per-product install (S4, S7), `~/.config/heyma/host.toml` records what each host runs | `docs/heyma-shell.md`, "Stories" |
| HeyMa has no reference to Voxxy or TTS today. The empty `tts/` directory at the repo root has no tracked content | `grep`, `ls` |
| Name clash: Wax already has a **voice registry** (`~/d/Voices/`, `wax voices`) for speaker *identification*. Voxxy "voices" are synthesis *profiles* | `~/HeyMa/AGENTS.md` |

## 2. Why it fits

HeyMa turns sound into text. Voxxy turns text into sound. Today HeyMa's tray can
tell you whether the microphone path is healthy but not whether the
voice that answers you is. The answering half already runs on Voxxy: the Hermes
agents speak through it, and Holocene memo replies come back through it. Folding
Voxxy in as HeyMa's fifth product gives one place
that shows both directions for every host, without moving any of Voxxy's code
into HeyMa. That follows HeyMa's co-location rule: the shell calls a product's
CLI and never imports it.

## 3. Integration seams

Ordered by how much they depend on the parked choices in section 6.

| # | Seam | Owner repo | Needs a Jarad decision? |
|---|---|---|---|
| V1 | **Registry adapter `voxxy`** in heyma-shell. `installed` if `voxxy` resolves on the host or `host.toml` names a Voxxy URL. `health` from `voxxy health --json`: exit 0 ok, 2 warn, 3 fail. `summary` names the ready engines. `actions`: "Say a test line", "Open voice list". Read-only, so it never changes the icon's red (mic-hot) rule | HeyMa | No (only the section label, Q1) |
| V2 | **"Speak on this desk" toggle**: a `voxxy-listen.service` user unit running `voxxy listen --key <host>`, so agent and memo replies can play on the machine you are sitting at. Tray row toggles the unit; the row shows listener state from `GET /sink/{key}` | Voxxy (unit + stable status), HeyMa (row) | Q5, which hosts listen by default |
| V3 | **`heyma setup` step "Install Voxxy?"**: client-only by default (`uv tool install` the CLI, write `vox_url`, sink key and an `op://` API-key reference into `host.toml`). Server role (`voxxy daemon install`, GPU engines) only on a host that has a GPU | HeyMa (S4/S7 already planned) | Q3, because self-host packaging depends on it |
| V4 | **Mac section**: HeyMa.app is Python-free, so the Mac needs a native sink listener (Swift SSE plus AVPlayer) instead of `voxxy listen`. Needs a Mac build | HeyMa (`components/heyma-mac`) | Q2, Q5 |
| V5 | **Packaging hygiene in Voxxy itself**: turn the API-key guard on in production once every first-party caller sends the key (Hermes plugin, inbox-watcher, Node-RED, CLI); keep `/healthz` public so the adapter needs no key; give the repo a `.launch.yaml` (`mode: steady` plus a `showcase:` block with a demo video once one exists) so it joins Holocene's `/live` "On deck" shelf | Voxxy | Q3 decides when the key becomes mandatory |
| V6 | **Wax to Voxxy data seams** (later): read a transcript aloud; seed a Voxxy voice from a Wax voice-registry clip; spoken greetings for Telephony. Each one crosses a consent or product line | both | Q4 (cloning), Telephony stays shadow |

Not in scope: moving Voxxy into the HeyMa repo (it is a separate registered
project; Vinyl is a submodule only for co-location), any change to how Wax,
Vinyl or Telephony work, and any billing or account system.

## 4. The joining-HeyMa contract (the level up)

What any product needs before it gets a HeyMa section. Voxxy is the first product
to follow it. Wax, Vinyl, Telephony and Phonograph already meet it:

1. A CLI or endpoint that reports status as JSON **with a stable exit code** (0 nominal, 2 degraded, 3 or more unreachable or failed), with no credentials needed to read health.
2. One adapter in `heyma_shell/adapters.py` that calls only that CLI or endpoint and returns the shared row shape; an adapter that raises becomes a failed section, never a failed registry (`registry._safe`).
3. Actions are calls to the product's own CLI, never HeyMa code doing the product's work.
4. A `heyma setup` step that calls the product's own installer and records the host's role in `host.toml`.
5. A Mac story when the product has anything to do on a Mac (native, no Python).
6. The product's own repo owns its stories; HeyMa owns the adapter, tray row, settings page and setup step.

Write this list into `~/HeyMa/docs/heyma-shell.md` when V1 is accepted, so the
next product (for example Earshot or the parked Share-menu read-aloud) follows it
without a new scope.

## 5. Proposed order (after section 6 is answered)

1. V1 adapter and `heyma status` row (HeyMa). Small, read-only, no interface choices beyond the label.
2. V5 key hygiene in Voxxy, caller by caller, then enforce.
3. V2 listen unit and tray toggle (Linux).
4. V3 setup step (rides the planned HeyMa S4).
5. The polished interface itself (Q2), then a demo video and the `showcase:` block, which puts Voxxy on the `/live` On deck shelf.
6. V4 Mac section; V6 seams one at a time, each with its own scope.

Stories: Voxxy-side work (V2 unit, V5) goes in Voxxy's BMAD; shell work (V1, V2
row, V3, V4) goes in HeyMa's BMAD as stories under the HeyMa shell epic. Neither
is written yet.

## 6. Decisions (Jarad, 2026-11-23, dictated)

| # | Question | Decision |
|---|---|---|
| Q1 | Name in HeyMa | **Voxxy**. Never "Voices" |
| Q2 | Where the polished interface lives | **Both**: a section in the HeyMa tray and a Voxxy web app |
| Q3 | Who it is for | **A product from day one**: pricing tiers, a self-hosted edition, per-customer keys. Pricing and positioning: `voxxy-product-and-pricing.md` |
| Q4 | Voice cloning | Cloning works from your own recordings, with voice fingerprinting. The **hosted web app is strict**: your own voice, or describe a voice; never someone else's. The **self-hosted edition** (flat fee, about $50) carries no such rule and the operator's own responsibility; that is implied, never advertised. Subscribers own the rights to voice files they download |
| Q5 | Which hosts speak by default | Not answered; default stands (the desk you use most, others opt in through `heyma setup`) |
| Q6 | Mac priority | **After the first three Linux pieces work** (V1 adapter, key, speak-here toggle) |
| — | Node-RED | **Retired**. Package and docs removed from Voxxy |
| — | Where it sells | **store.automaticai.io**, the second product after Earshot, under the HeyMa family (listing name: N1 in the pricing doc) |
| — | API key | **Required** |

The original questions and recommendations, kept for the record:

| # | Question | Recommendation | Default |
|---|---|---|---|
| Q1 | **Name in HeyMa**: does it appear as "Voxxy" (its own brand, like Vinyl) or as a generic "Voices" section? | "Voxxy", so the brand carries to a sellable product; "Voices" also collides with Wax's voice registry | Voxxy |
| Q2 | **Where does the polished interface live?** (a) only inside HeyMa (tray section, settings page, Mac section); (b) a standalone Voxxy web app (voice library, try-a-line, clone wizard, character sounds); (c) both, with HeyMa linking to the web app | (c): HeyMa shows health and the speak-here toggle; the web app is the polished surface you would demo and later sell | Not started until you choose |
| Q3 | **Who is it for?** Personal and HeyMa-internal only, or a product for other people (the April monetization tiers, self-host install, API keys per customer)? | Internal first, with the API key enforced so selling later needs no rework | Internal |
| Q4 | **Voice cloning from Wax recordings**: allowed, and for whose voices? | Only your own voice and voices you name explicitly; never automatic from the registry | Not built |
| Q5 | **Which hosts speak by default?** (big-chungus desktop, MacBook, Mac Studio, laptop) | The desk machine you use most; others opt in through `heyma setup` | None until set up |
| Q6 | **Mac priority**: is a native Mac listener worth a Mac build now? | After V1 to V3 on Linux | Later |

No spending is implied by any seam above. The Developer ID HeyMa.app already
uses would cover a Mac build.

## 7. Built (2026-11-23)

| Seam | State | Where |
|---|---|---|
| V1 | Built and live. `heyma status` shows `voxxy ok · up · voxcpm, elevenlabs ready`; tray submenu "Voxxy" with "Say a test line" and "Open Voxxy". Contract written into `docs/heyma-shell.md` (stories S9 to S12) | HeyMa `1f29988` |
| V5 key | Vault item "Voxxy API Key - vox.delo.sh" (`op://DeLoSecrets/mpxfavdfzeqh7ahqc2r3qa6aki/credential`). One convention for every caller: `$VOX_API_KEY`, else `$VOX_API_KEY_REF` (op://), else `api_key_ref` in `~/.config/voxxy/config.toml`. Wired: voxxy CLI, Hermes `tts/vox` and `voice-persona` plugins (gateway drop-in), Holocene inbox-watcher and API, Genesis (`GENESIS_VOX_KEY`), Flume voice checks, the shell, `notify-vox.sh`. Listening on a sink (`GET /sink/{key}`, `/events`) stays public, so Mac listeners need no key | voxxy `e9f89b2`, `a38507d`; holocene `d058ca3`; genesis `2108bf2`; flume |
| V5 enforce | **Enforced 2026-11-23** (Jarad approved). `.env.template` carries `VOX_API_KEY=op://DeLoSecrets/mpxfavdfzeqh7ahqc2r3qa6aki/credential`; `voxxy daemon restart` recreated the core. Verified: `/voices`, `/sink/*/say`, `/mcp` answer 401 without a key and 200 with it (X-API-Key or Bearer); `/healthz`, `/`, `/ui/*`, `/app-config`, sink listening stay public. Callers verified with the key on: voxxy CLI, HeyMa row, Hermes `tts/vox` plugin, Holocene inbox-watcher and hub voices, Genesis. No 401s from real callers in the first minute. Rollback: remove the line, `voxxy daemon restart` | voxxy `.env.template` |
| V2 | Built and verified on big-chungus. `voxxy sink on/off/toggle` writes and drives the `voxxy-listen.service` user unit (key = hostname, baked into ExecStart; never `$VOX_SINK`); `voxxy sink status --json` adds the local unit. Tray item "Speak on this desk" toggles it (✓ when on). Left off on big-chungus | voxxy `b1d97b0`; HeyMa `f3b7b29` |
| V3 | Built and verified. `voxxy setup [--url] [--key-ref op://] [--speak-here/--no-speak-here] --json` (exit 0/2/3); `heyma setup voxxy` calls it and records `[voxxy]` in `~/.config/heyma/host.toml` | voxxy `730603f`; HeyMa `966c8e1` |
| Web app v1 | **Live at https://vox.delo.sh/** (deployed 2026-11-23, hosted edition; key gate active since the key was enforced). One static app (`app/web/`, served at `/`, assets under public `/ui/`) with a key gate, Studio, Voices library, Make a voice (record with a consent challenge, describe; upload in the self edition only) and Speakers. `VOX_EDITION` (hosted by default, `self`) is reported by public `GET /app-config`. New routes: `POST /voices/design`, `/voices/design/preview`, `/voices/consent/challenge`, `/voices/own`. Hosted refuses real-person descriptions. Consent ASR plus speaker match: see G5 below. Story: `_bmad-output/implementation-artifacts/stories/voxxy-web-app-v1.md` | voxxy `3e94180` |
| G5 | **Live at vox.delo.sh** (2026-11-23, `19a1277`; migration 004 applied, models in `./models`). Live check: a wrong-code reading was refused 422 with phrase score 1.00 and a consent record kept; no voiceprint set. Operator key "Voxxy Operator Key" (`op://DeLoSecrets/6wdqvicsykbgjho6do5kegzgxm/credential`) wired 2026-11-23: the review queue answers 401 without a key, 403 to the API key, 200 to the operator key; `voxxy consent reviews` reads it from `$VOX_OPERATOR_KEY_REF`. Calibration slices of private recordings deleted (Jarad's go-ahead). Since `d678325` a hosted account's first voice always goes to review (live probe: a correct-code synthetic first voice was held as pending, refused synthesis 403, set no voiceprint, and was rejected through `voxxy consent reject`). Pricing N1-N16 decided 2026-11-23 (`voxxy-product-and-pricing.md` section 10). One consent gate for any likeness path; Record yours uses it: challenge in postgres (migration `004_consent.sql`), ASR check (faster-whisper base.en, the spike's phrase rule) plus speaker match (TitaNet-large via sherpa-onnx, silence cut first) against an optional reference clip and the account voiceprint. Accepted tags `consent-verified`; review stores `consent-pending` and synthesis answers 403; rejected answers 422 with a plain reason. Operator queue `GET/POST /consent/reviews` plus `voxxy consent ...` behind `VOX_OPERATOR_KEY`. Hosted enforces, self only warns. Thresholds are the calibration spike's (accept 0.70, refuse below 0.10, review between); recalibrate after about 50 real consent pairs. Story: `_bmad-output/implementation-artifacts/stories/voxxy-g5-consent.md` | voxxy `19a1277`, `d98e6ef` |
| Next | Recalibrate on about 50 real consent pairs; per-key voice namespaces (customer keys must not reach plain `POST /voices`); then V4 Mac listener (S12) | |
