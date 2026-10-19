"""Run the tests against an upcoming Home Assistant (beta or dev).

pytest-homeassistant-custom-component is released for each Home Assistant
release, so against a newer beta or dev build it can lag behind. This bridges
the known gaps before the harness loads, then runs pytest with any arguments
given:

    python scripts/run_upcoming_tests.py -q

- Home Assistant must be imported before the harness, which imports httpx
  first (newer Home Assistant aliases httpx when it loads).
- Home Assistant 2026.11 moved the logger's private test helper
  `_clear_logger_overwrites` into `logger.helpers`; the harness still imports
  it from `homeassistant.components.logger`.

Each shim only applies when the harness needs it, so this also runs fine
against the current release.
"""

import sys

import pytest

import homeassistant  # noqa: F401  # must load before the harness
from homeassistant.components import logger


def _bridge_logger_helper() -> None:
    if hasattr(logger, "_clear_logger_overwrites"):
        return
    try:
        from homeassistant.components.logger import helpers
    except ImportError:
        return
    if hasattr(helpers, "_clear_logger_overwrites"):
        logger._clear_logger_overwrites = helpers._clear_logger_overwrites  # type: ignore[attr-defined]


_bridge_logger_helper()
sys.exit(pytest.main(sys.argv[1:]))
