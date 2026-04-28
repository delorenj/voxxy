# node-red-contrib-vox

TTS node for the voxxy service (multi-engine TTS: VoxCPM2, VibeVoice, ElevenLabs fallback).

## Install (local dev)

From your Node-RED user directory (`~/.node-red` typically):

```bash
  npm install /home/delorenj/code/voxxy/node-red-contrib-vox
node-red-restart
```

## Use

Drop the **vox tts** node into a flow. Feed it `msg.payload` as a string.
Output is `msg.payload` containing WAV bytes (Buffer).

- Service URL defaults to `http://vox:8000` (expecting the `vox` container on the same docker network or set via env).
- Override `voice` per message with `msg.voice = "rick"`.
- Override `cfg` / `steps` with `msg.cfg` / `msg.steps`.

## Future

Swap the HTTP POST for a Bloodbank (RabbitMQ) publish so synthesis becomes
event-driven with at-least-once delivery. Same payload shape; different transport.
