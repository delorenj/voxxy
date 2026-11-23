# Story: Voxxy web app v1

Status: deployed to https://vox.delo.sh/ (2026-11-23, commit 3e94180)
Pillar: `belsky-doctrine`
Decision source: `_bmad-output/planning-artifacts/voxxy-heyma-integration-scope.md` (Q2 both surfaces, Q3 product from day one, Q4 strict hosted)

## Level up

Asked for: a polished web surface for vox.delo.sh.
Built: one static app that serves any Voxxy deployment, hosted or self-hosted, switched by the `VOX_EDITION` flag. vox.delo.sh is the first user; every later self-hosted install gets the same app with upload turned on. Brand is "Voxxy", part of HeyMa ("HeyMa hears you. Voxxy answers in a voice."). Footer reads "Voxxy · part of HeyMa".

## Acceptance criteria

1. `GET /` serves the app shell. Static assets live under `/ui/` and are public (GET and HEAD only). No npm, build step or CDN; source is in `app/web/`.
2. `GET /app-config` is public and returns `{edition, auth_required, public_base_url, features}`. It never returns the key.
3. `VOX_EDITION` defaults to `hosted`; `self` is the other value; anything else falls back to hosted. It is set in `compose.yml` with a comment.
4. Key gate: when `auth_required`, the app asks for a key, checks it against `GET /voices`, stores it in this browser only, and returns to the gate if the server later answers 401.
5. Studio: type text, pick a voice, press Speak, hear the result. Optional "Speak on this desk" delivery to a sink.
6. Voices library: grouped (own, designed, house, other), searchable, preview, "Say with it", confirmed delete.
7. Make a voice:
   - Record: a consent challenge (sentence plus 6-digit code), a recorder with an 8 s minimum and level meter, playback review, save.
   - Describe: a text description, optional sample line, preview, save.
   - Upload: offered in the self edition only, with the copy "Use recordings you have the rights to."
8. Speakers: how to turn on Speak on this desk (`voxxy setup --url <base>`, `voxxy sink on`), a desk check and a test line.
9. Responsive down to 375 px, light and dark, keyboard accessible, reduced motion respected.
10. Hosted edition refuses descriptions of real people with 422 and logs the refusal. Voices made this way are tagged `designed`.
11. `POST /voices/consent/challenge` and `POST /voices/own`: challenge is single-use, valid 10 minutes, held in memory. Voices saved this way are tagged `own` and `unverified-consent`.
12. Copy never says "clone anyone" or "no restrictions".
13. Tests cover every new route and the public-path rules.

## What was built

Server (`app/`):

- `edition.py`: edition flag and the real-person description check (comparison phrases such as "sounds like", "voice of", "impression of"; runs of capitalised words that read as a name, with voice vocabulary, nationalities and languages excluded).
- `consent.py`: in-memory `ChallengeStore` (issue, check, redeem; TTL 600 s; cap 2000 open).
- `audio.py`: `to_wav_mono` (ffmpeg fallback for browser containers).
- `auth.py`: `/ui/` prefix and `/app-config` are public.
- `main.py`: `GET /`, `GET /app-config`, `/ui` static mount (no-cache), shared helpers for the WAV trim and voice upsert (the old `POST /voices` now uses them), plus the routes below. `DELETE /voices/{name}` also removes the consent sidecar.

Routes added:

| Route | Auth | Notes |
| --- | --- | --- |
| `GET /` | public | app shell |
| `GET /ui/*` | public | static assets |
| `GET /app-config` | public | edition, auth_required, public_base_url, features |
| `POST /voices/design/preview` | key | audio only, nothing saved |
| `POST /voices/design` | key | 422 on real-person description in hosted; tag `designed` |
| `POST /voices/consent/challenge` | key | returns challenge id, sentence, code, expiry, record limits |
| `POST /voices/own` | key | 404 unknown, 410 expired or used; tags `own`, `unverified-consent`; writes `<name>.consent.json` sidecar |

Web (`app/web/`): `index.html` and `ui/` ES modules (`app`, `api`, `dom`, `state`, `player`, `picker`, `studio`, `voices`, `make`, `recorder`, `speakers`), `app.css`, `voxxy-mark.svg`. Hash router, system fonts, oklch tokens, key stored in localStorage with an in-memory fallback.

Tests: `tests/test_web_app.py`.

## Verified

- `uv run pytest tests -q`, full output in `~/.local/state/voxxy/test-runs/webapp-build.log`.
- Browser run (Playwright chromium, fake microphone) against a fake-repo harness: gate, Studio, desk panel, library, record flow, describe flow including the hosted refusal, delete. Desktop 1280 and mobile 375, light and dark, no horizontal overflow.
- Verified live 2026-11-23: `/`, `/ui/*`, `/app-config` 200; Say synthesized and played in Chrome (Kapture); `/voices/design/preview` 200; a real-name description refused 422; consent challenge issued.

## Deferred

- G5, consent verification: ASR transcript match of the sentence and code, and a speaker-embedding match against the reference. Today the challenge only proves the recording was requested; voices are marked `unverified-consent` and the sidecar records `asr_transcript: not_run`, `speaker_match: not_run`. The UI says so.
- Per-customer key namespaces: voices and keys are still one shared set. Server-side upload and design policy keyed to a customer key comes with them.
- Accounts, billing, pricing tiers and the store listing: not decided, nothing built.
- API key enforcement in production (`VOX_API_KEY` in `.env.template`): parked for Jarad. Sink listening stays public so Mac listeners need no key.
- Mac listener (V4, HeyMa story S12).
- Deploy: the `vox` core must be recreated to serve the app (Dockerfile already copies `app/`). Not done here.
