"""Diagnostics for HomeKit Extended."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import HomeKitExtendedConfigEntry
from .const import CONF_PIN

TO_REDACT = {CONF_PIN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> dict[str, Any]:
    """Return the entry config and the accessory as HomeKit sees it."""
    server = entry.runtime_data
    driver = server.driver
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        **(
            {"bridge": server.bridge_name, "attached": server.attached}
            if getattr(server, "bridged", False)
            else {"port": server.port}
        ),
        "paired": server.paired,
        "paired_controllers": (
            server.paired_controllers
            if getattr(server, "bridged", False)
            else len(driver.state.paired_clients)
            if driver
            else 0
        ),
        "accessory": (
            server.published.to_HAP()
            if getattr(server, "published", None) is not None
            else server.accessory.to_HAP()
            if server.accessory
            else None
        ),
    }
