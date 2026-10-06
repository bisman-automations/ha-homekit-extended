"""Registry of HomeKit Extended accessory types, in menu order."""

from __future__ import annotations

from .air_purifier import AIR_PURIFIER
from .air_quality import AIR_QUALITY
from .base import AccessoryType
from .buttons import BUTTONS
from .ceiling_fan import CEILING_FAN
from .multi_sensor import MULTI_SENSOR
from .power_strip import POWER_STRIP
from .valves import FAUCET, IRRIGATION

ACCESSORY_TYPES: dict[str, AccessoryType] = {
    accessory_type.key: accessory_type
    for accessory_type in (
        IRRIGATION,
        CEILING_FAN,
        BUTTONS,
        MULTI_SENSOR,
        AIR_QUALITY,
        AIR_PURIFIER,
        POWER_STRIP,
        FAUCET,
    )
}

__all__ = ["ACCESSORY_TYPES", "AccessoryType"]
