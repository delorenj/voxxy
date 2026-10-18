# Voxxy branding

`voxxy-mark.svg` is the canonical Voxxy mark: a V whose arms frame three broadcast arcs radiating from the point,
one voice going out. Mint `#c7e9b4` (the colour the Node-RED node already ships) for the V, white for the waves,
on deep green `#13201b`.

It is the Telegram avatar of @voxxy_pm_bot, set with the `hermes-project-avatar` skill:

```bash
python3 ~/.agents/skills/hermes-project-avatar/scripts/telegram_avatar.py convert \
  --svg branding/voxxy-mark.svg --out /tmp/voxxy-pm.jpg --background '#13201b' --pad 5
```

Change the mark here and re-run the skill; the Holocene Agent Pad picks the new avatar up from Telegram by itself.
