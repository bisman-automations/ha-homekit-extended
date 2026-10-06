"""Shared helpers for HomeKit Extended."""

from __future__ import annotations

import re
import secrets
from typing import Any

from homeassistant.core import HomeAssistant

PIN_RE = re.compile(r"^\d{3}-\d{2}-\d{3}$")

# Setup codes the HomeKit Accessory Protocol spec forbids.
INVALID_PINS = frozenset(
    {
        *(f"{d * 3}-{d * 2}-{d * 3}" for d in "0123456789"),
        "123-45-678",
        "876-54-321",
    }
)


def validate_pin(pin: str) -> bool:
    """Return true if the pin is a well-formed, HAP-permitted setup code."""
    return bool(PIN_RE.match(pin)) and pin not in INVALID_PINS


def generate_pin() -> str:
    """Return a random, valid HomeKit setup code."""
    while True:
        digits = f"{secrets.randbelow(10**8):08d}"
        pin = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
        if validate_pin(pin):
            return pin


def to_float(value: Any) -> float | None:
    """Convert a Home Assistant state value to a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    """Return the entity's friendly name, or a readable fallback."""
    state = hass.states.get(entity_id)
    if state is not None and state.name:
        return state.name
    return entity_id.split(".", 1)[-1].replace("_", " ").title()
