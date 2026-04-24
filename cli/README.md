# voxxy CLI

Unified command-line interface for the vox-tts service.

See [docs/specs/voxxy-cli.md](../docs/specs/voxxy-cli.md) for the full spec and
[docs/specs/voxxy-cli.plan.md](../docs/specs/voxxy-cli.plan.md) for the task plan.

## Install (dev)

```bash
cd cli
uv sync
uv run voxxy --help
```

## Install (global)

```bash
uv tool install ./cli
voxxy --help
```
