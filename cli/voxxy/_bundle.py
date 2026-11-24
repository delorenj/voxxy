"""Edition defaults for this copy of the CLI.

The repository copy is the fleet edition. The self-hosted bundle build
(``scripts/bundle/build.py``) overwrites this file with the self-hosted values and
leaves the fleet-only modules out (``voice clone``, the ``persona`` group), so code
must never import those modules unconditionally; check :data:`FLEET` first.
"""

EDITION = "fleet"
FLEET = EDITION == "fleet"
DEFAULT_URL = "https://vox.delo.sh"
DEFAULT_VOICE = "rick"
# How to get the optional clip-analysis dependencies (`voice add --auto`, `voice candidates`).
CLONE_INSTALL_HINT = 'uv tool install --editable "$HOME/code/voxxy/cli[clone]" --force'
