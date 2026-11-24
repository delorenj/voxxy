# Story: G4 self-hosted bundle

Status: built and checked 2026-11-23 (voxxy `87de72b`, `3fc48da`, `925dad9`). Not sold yet: the bundle is
a local build artifact (`dist/bundle/`, gitignored), the license signing key does not exist, and the
self-hosted gate (G9) stays off. The live hosted service at vox.delo.sh runs the same code (license
`null`, `/license` 404 there).
Pillar: `belsky-doctrine`
Decision source: `_bmad-output/planning-artifacts/voxxy-product-and-pricing.md` (section 3.3, N3, N11; gap G4)

## Level up

Asked for: a Voxxy self-hosted bundle.
Built: a product-agnostic, manifest-driven bundle build and check (`scripts/bundle/build.py`,
`check.py`, `smoke.sh`). Voxxy is the first manifest (`bundle/selfhost/manifest.toml`). Any
self-hosted AutomaticAI product can add its own manifest and get the same copy/rewrite/prune build,
forbidden-content check, reproducible tarball and install smoke. The VS1 license uses Earshot's ES1
shape, so one issuer (G10) can sign both.

## Acceptance criteria

1. `mise run bundle:build` writes `dist/bundle/voxxy-selfhost-<version>/`, a reproducible
   `.tar.gz` (sorted entries, fixed owner, commit-time mtime) and its `.sha256`, then runs the check.
   Exit 0 clean, 2 warnings (for example a dirty source tree, scoped to the bundle's own source
   paths), 3 forbidden or broken.
2. Contents follow the pricing doc: VoxCPM2 engine only, the four `house-*` voices (sha256 checked
   against `house-voices/voices.toml`), no fleet, celebrity or character voices, no personas, and no
   web-search/YouTube clone flow (`voxxy/clone.py`, `commands/voice_clone.py`, `commands/persona.py`
   pruned from the wheel; yt-dlp dropped from its dependencies).
3. The CLI wheel carries a rewritten `voxxy/_bundle.py` (`EDITION = 'self'`, local default URL,
   `house-f1` default voice). The fleet CLI is unchanged (`clone`, `persona` still there).
4. The check fails on: an index mismatch, a forbidden path, any audio not in the recipe, fleet
   voice or persona names (derived live from `voices/` and `plugins/voice-persona/personas/`),
   host names, home paths, vault references, secret-shaped strings; a CLI that offers a pruned
   command or lacks a required one (fresh venv); a compose file Docker rejects; a shell script that
   fails `bash -n` or shellcheck.
5. `install.sh` (buyer-facing): preflight (Linux, Docker, Compose, NVIDIA runtime, 8 GB VRAM, 20 GB
   disk), `.env` with random secrets at mode 600, `compose up --build`, health wait, house voices
   registered from `voices/voices.tsv`, optional `--license KEY|FILE`, the CLI installed with
   `voxxy setup --key-ref file:<bundle>/.env#VOX_API_KEY`, engine check. `--check`, `--no-cli`,
   `--no-start`.
6. VS1 license, `VS1.<b64url payload>.<b64url Ed25519 sig over "voxxy-license-v1\n" + payload>`,
   verified offline (`app/license.py`). Soft enforcement: Voxxy always starts and speaks. States:
   `valid`, `missing`, `invalid`, `updates_ended` (the bundle's `core/RELEASE.json` released after
   the buyer's `updates_until`). Shown in `/app-config` (state, detail, date only), `GET /license`
   (API key), `voxxy license show` (exit 0 / 2 / 3 / 4 hosted / 5 refused) and logged at start.
   `POST /license` / `voxxy license import KEY|FILE|-` store the key on the data volume only if it
   verifies; an invalid key answers 422 `X-Vox-Error: license_<state>`. Hosted: `license: null`,
   routes 404.
7. Tests: `tests/test_bundle.py` (11), `tests/test_license.py` (32), `cli/tests/test_license_cmd.py`
   (6). Suites 2026-11-23: server 437 passed / 7 skipped, CLI 401 passed.
8. `mise run bundle:smoke` installs the built tarball for real as a buyer would (scratch Compose
   project `voxxy-bundle-smoke`, port 18766, the contract's fake engine standing in for the GPU) and
   checks: `.env` mode, the four house voices with tags and transcript, 401 without the key, WAV
   synthesis, edition `self`, license `missing`, 422 on a bad key, the release stamp, CLI setup with
   the `file:` reference, `voxxy voice list`, `voxxy license show` exit 2, then tears the stack down.
   Passed 2026-11-23 on tarball sha256 `385cf4a5…67ba69`.

## Parked for Jarad

- The Voxxy license signing keypair and its vault item; then its public key goes into
  `app/license.py: TRUSTED_PUBLIC_KEYS` (until then every license reads `invalid`).
- N11: moving the persona files and the `voice_clone` flow out of the public `delorenj/voxxy`
  repo into a private fleet-only repo (a public-repo removal).
- A real GPU install on a buyer-like machine (the smoke uses the fake engine).

## Next

- G10: generalise the Earshot issuer to a per-product catalog in the store repo, so it mints VS1
  keys too (Earshot's behaviour unchanged).
- Wire the release step: bump `[bundle] version`, build, publish the tarball to the delivery
  location the store's thanks page points at.
