"""HomeKit Extended: accessory types Home Assistant's HomeKit Bridge lacks.

Each config entry publishes one standalone HomeKit accessory on its own port
with its own pairing, alongside (not inside) the core HomeKit Bridge.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .air_purifier import create_air_purifier
from .const import (
    ACCESSORY_AIR_PURIFIER,
    ACCESSORY_IRRIGATION,
    CONF_ACCESSORY_TYPE,
    CONF_PORT,
)
from .driver import AccessoryFactory, HomeKitAccessoryServer, persist_path
from .irrigation import create_irrigation

_LOGGER = logging.getLogger(__name__)

FACTORIES: dict[str, AccessoryFactory] = {
    ACCESSORY_AIR_PURIFIER: create_air_purifier,
    ACCESSORY_IRRIGATION: create_irrigation,
}

type HomeKitExtendedConfigEntry = ConfigEntry[HomeKitAccessoryServer]


async def async_setup_entry(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> bool:
    """Start publishing the entry's accessory."""
    server = HomeKitAccessoryServer(
        hass, entry, FACTORIES[entry.data[CONF_ACCESSORY_TYPE]]
    )
    try:
        await server.async_start()
    except OSError as err:
        port = {**entry.data, **entry.options}[CONF_PORT]
        raise ConfigEntryNotReady(f"Unable to listen on port {port}: {err}") from err

    entry.runtime_data = server
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> bool:
    """Stop publishing the entry's accessory."""
    await entry.runtime_data.async_stop()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the pairing so a re-added accessory starts fresh."""
    path = persist_path(hass, entry)
    await hass.async_add_executor_job(path.unlink, True)


async def _async_update_listener(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> None:
    """Re-publish the accessory with new options."""
    await hass.config_entries.async_reload(entry.entry_id)
