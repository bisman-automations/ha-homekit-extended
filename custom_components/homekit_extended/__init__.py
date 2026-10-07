"""HomeKit Extended: accessory types Home Assistant's HomeKit Bridge lacks.

Each config entry publishes one HomeKit accessory, either standalone on its
own port with its own pairing, or inside one of core HomeKit's bridges.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .bridge import (
    BridgedAccessoryServer,
    async_follow_bridges,
    async_install_hook,
    homekit_bridges,
)
from .const import CONF_BRIDGE, CONF_PORT, DOMAIN, SIGNAL_PAIRING_CHANGED
from .driver import (
    IID_MANAGER_STORAGE_VERSION,
    HomeKitAccessoryServer,
    IIDStorage,
    iid_storage_key,
    persist_path,
)
from .pairing import (
    DATA_QR_TOKENS,
    async_dismiss_pairing,
    async_register_view,
    async_show_pairing,
)

PLATFORMS = [Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type HomeKitExtendedConfigEntry = ConfigEntry[
    HomeKitAccessoryServer | BridgedAccessoryServer
]
DATA_HOOKED = f"{DOMAIN}_hooked"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the pairing QR code view and hook into core HomeKit bridges."""
    async_register_view(hass)
    hass.data[DATA_HOOKED] = async_install_hook(hass)
    async_follow_bridges(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> bool:
    """Start publishing the entry's accessory."""
    if {**entry.data, **entry.options}.get(CONF_BRIDGE):
        return await _async_setup_bridged(hass, entry)

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


async def _async_setup_bridged(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> bool:
    """Add the entry's accessory to a core HomeKit Bridge."""
    bridge_id = {**entry.data, **entry.options}[CONF_BRIDGE]
    for kind, failed in (
        ("bridge_unsupported", not hass.data.get(DATA_HOOKED)),
        ("bridge_missing", bridge_id not in homekit_bridges(hass)),
    ):
        issue_id = f"{kind}_{entry.entry_id}"
        if not failed:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
            continue
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=kind,
            translation_placeholders={"name": entry.title},
        )
        raise ConfigEntryError(
            f"{entry.title} can't be added to its HomeKit Bridge; see Repairs"
        )

    server = BridgedAccessoryServer(hass, entry)
    await server.async_start()
    entry.runtime_data = server
    async_dismiss_pairing(hass, entry.entry_id)
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
    """Forget the pairing and stored IDs so a re-added accessory starts fresh."""
    await async_reset_pairing_state(hass, entry)
    await IIDStorage(
        hass, IID_MANAGER_STORAGE_VERSION, iid_storage_key(entry.entry_id)
    ).async_remove()
    for kind in ("bridge_missing", "bridge_unsupported", "bridge_full"):
        ir.async_delete_issue(hass, DOMAIN, f"{kind}_{entry.entry_id}")
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
