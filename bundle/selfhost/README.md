# Voxxy, self-hosted edition

Voxxy turns text into speech in a voice you choose. This edition runs on your own Linux
machine with an NVIDIA graphics card: your text, voices and audio stay on it, and nothing
is metered.

## What you need

- Linux with an NVIDIA graphics card with at least 8 GB of memory (12 GB or more is comfortable)
- Docker with the Compose plugin and the NVIDIA container toolkit
- About 20 GB of free disk for the images and the model weights
- `uv` or `pipx`, if you want the `voxxy` command (recommended)

Check a machine without changing anything:

```bash
./install.sh --check
```

## Install

```bash
./install.sh
```

It checks the machine, writes `.env` with a new database password and API key, builds and
starts three containers (database, Voxxy, the VoxCPM2 speech engine), adds the four house
voices and installs the `voxxy` command. The first start downloads the VoxCPM2 weights
(about 4.6 GB) from Hugging Face. Run `./install.sh` again at any time: it keeps your
settings and voices.

When it finishes:

- **Web app:** open http://127.0.0.1:8000/ and sign in with the `VOX_API_KEY` value from `.env`.
- **Command line:** `voxxy speak -v house-f1 "Hello from my own machine."`
- **Your own code:** send the key as `X-API-Key` (or `Authorization: Bearer`). The OpenAI-style
  speech endpoint is `POST /v1/audio/speech`; the full API is described at `/docs`.
- **AI agents:** the MCP endpoint is `/mcp/`.

## Voices

Four house voices ship with Voxxy: `house-f1`, `house-f2`, `house-m1`, `house-m2`. They are
synthetic and belong to nobody. You can also:

- describe a voice in words and have one made (the web app's *Make a voice*);
- make a voice from a clip: `voxxy voice add clip.wav --name my-voice --auto` picks the
  cleanest 10-15 seconds and transcribes them (`voxxy voice candidates clip.wav` shows the choices).

**Only make voices you have the right to use.** That is your responsibility on this
edition. Using someone's voice without their permission may be against the law where you live.

## Everyday commands

From this folder:

| To | Run |
|---|---|
| Stop Voxxy | `docker compose down` |
| Start it again | `docker compose up -d` |
| See what it is doing | `docker compose logs -f vox` (or `engine-voxcpm`) |
| Check its health | `voxxy health` or `curl http://127.0.0.1:8000/healthz` |
| Change a setting | edit `.env`, then `docker compose up -d` |

Your voices live in `data/voices/` and in the database volume. `docker compose down` keeps
both; `docker compose down -v` deletes the database, so do not use `-v` unless you mean it.

## Opening it to your network

By default Voxxy listens on 127.0.0.1 only. To reach it from other machines, set
`VOXXY_BIND=0.0.0.0` and `VOX_PUBLIC_BASE_URL=http://<this machine>:8000` in `.env`, then
`docker compose up -d`. Every request still needs your API key.

## Help

Email support@automaticai.io with the output of `./install.sh --check` and
`docker compose logs --tail 200`. Include the version from the `VERSION` file.

The speech engine is VoxCPM2 by OpenBMB, licensed Apache-2.0.
