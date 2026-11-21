"""Diagnostics for HomeKit Extended."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from . import HomeKitExtendedConfigEntry
from .const import CONF_DEVICE, CONF_PIN, CONF_SERIAL

TO_REDACT = {CONF_PIN, CONF_SERIAL, CONF_DEVICE}

# HAP's SerialNumber characteristic, as pyhap writes its type (short form for
# Apple's base UUID) and the full UUID, in case a version writes that instead.
SERIAL_NUMBER_TYPES = {"30", "00000030-0000-1000-8000-0026BB765291"}


def _redact_hap(value: Any) -> Any:
    """Replace every SerialNumber value in a HAP dump (often the MAC address)."""
    if isinstance(value, list):
        return [_redact_hap(item) for item in value]
    if not isinstance(value, dict):
        return value
    redacted = {key: _redact_hap(item) for key, item in value.items()}
    if (
        str(redacted.get("type", "")).upper() in SERIAL_NUMBER_TYPES
        and "value" in redacted
    ):
        redacted["value"] = REDACTED
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> dict[str, Any]:
    """Return the entry config and the accessory as HomeKit sees it."""
    entry_info = {
        "title": entry.title,
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
    }
    server = getattr(entry, "runtime_data", None)
    if server is None:
        # Not loaded (setup failed or a leftover bridge-mode entry).
        return {"entry": entry_info, "loaded": False}
    driver = server.driver
    published = server.published
    # The bridge HomeKit Extended publishes for separate zone accessories,
    # or the accessory itself. This is our driver, not core HomeKit's.
    accessory = published if published is not None else server.accessory
    return {
        "entry": entry_info,
        "port": server.port,
        "paired": server.paired,
        "paired_controllers": len(driver.state.paired_clients) if driver else 0,
        "accessory": _redact_hap(accessory.to_HAP()) if accessory else None,
    }
