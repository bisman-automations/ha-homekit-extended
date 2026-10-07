"""Print the pip requirements of core integrations HomeKit Extended builds on.

The tests set up Home Assistant's own HomeKit Bridge, whose requirements
change between releases, so CI installs them from whichever Home Assistant
version is installed: pip install $(python scripts/core_requirements.py)
"""

import json
from pathlib import Path

import homeassistant.components

COMPONENTS = Path(homeassistant.components.__path__[0])

# homekit_controller's aiohomekit is the HAP client in test_end_to_end.py.
for domain in ("homekit", "camera", "ffmpeg", "homekit_controller"):
    manifest = json.loads((COMPONENTS / domain / "manifest.json").read_text())
    for requirement in manifest.get("requirements", []):
        print(requirement)
