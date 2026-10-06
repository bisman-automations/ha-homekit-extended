"""HomeKit Extended: accessory types Home Assistant's HomeKit Bridge lacks.

Each config entry publishes one standalone HomeKit accessory on its own port
with its own pairing, alongside (not inside) the core HomeKit Bridge.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import CONF_PORT, DOMAIN, SIGNAL_PAIRING_CHANGED
from .driver import HomeKitAccessoryServer, persist_path
from .pairing import (
    DATA_QR_TOKENS,
    async_dismiss_pairing,
    async_register_view,
    async_show_pairing,
)

PLATFORMS = [Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type HomeKitExtendedConfigEntry = ConfigEntry[HomeKitAccessoryServer]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the pairing QR code view."""
    async_register_view(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> bool:
    """Start publishing the entry's accessory."""

    @callback
    def _pairing_changed() -> None:
        async_show_pairing(hass, server)
        async_dispatcher_send(hass, SIGNAL_PAIRING_CHANGED.format(entry.entry_id))

    server = HomeKitAccessoryServer(hass, entry, _pairing_changed)
    try:
        await server.async_start()
    except OSError as err:
        port = {**entry.data, **entry.options}[CONF_PORT]
        raise ConfigEntryNotReady(f"Unable to listen on port {port}: {err}") from err

    entry.runtime_data = server
    async_show_pairing(hass, server)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> bool:
    """Stop publishing the entry's accessory."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_stop()
    async_dismiss_pairing(hass, entry.entry_id)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the pairing so a re-added accessory starts fresh."""
    await async_reset_pairing_state(hass, entry)
    hass.data.get(DATA_QR_TOKENS, {}).pop(entry.entry_id, None)


async def async_reset_pairing_state(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the stored pairing; the entry must be unloaded first."""
    await hass.async_add_executor_job(persist_path(hass, entry).unlink, True)


async def _async_update_listener(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> None:
    """Re-publish the accessory with new options, unless they apply in place."""
    if not entry.runtime_data.async_apply_in_place():
        await hass.config_entries.async_reload(entry.entry_id)
