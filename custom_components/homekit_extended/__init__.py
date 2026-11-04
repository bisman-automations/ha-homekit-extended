"""HomeKit Extended: accessory types Home Assistant's HomeKit Bridge lacks.

Each config entry publishes one HomeKit accessory on its own port, with its
own pairing. Entries saved by the removed "add to a HomeKit Bridge" mode are
not converted and are not loaded: setup raises, a repair explains how to
recreate the accessory, and core HomeKit's storage is left untouched.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import CONF_BRIDGE, CONF_PORT, DOMAIN, SIGNAL_PAIRING_CHANGED
from .driver import HomeKitAccessoryServer, persist_path
from .id_storage import (
    AID_STORAGE_VERSION,
    IID_STORAGE_VERSION,
    aid_storage_key,
    iid_storage_key,
)
from .pairing import (
    DATA_QR_TOKENS,
    async_dismiss_pairing,
    async_register_view,
    async_show_pairing,
)

PLATFORMS = [Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type HomeKitExtendedConfigEntry = ConfigEntry[HomeKitAccessoryServer]

# Repairs from 2.x bridge mode. Cleared so an upgrade doesn't leave them
# behind next to the single "bridge mode was removed" repair.
_LEGACY_BRIDGE_ISSUES = (
    "bridge_missing",
    "bridge_unsupported",
    "bridge_full",
    "duplicate_entities",
)
BRIDGE_REMOVED = "bridge_removed"


def entry_config(entry: ConfigEntry) -> dict[str, Any]:
    """Entry data with options applied over it, matching the rest of the integration."""
    return {**entry.data, **entry.options}


def is_bridge_mode(entry: ConfigEntry) -> bool:
    """Return true when the entry was configured to join a core HomeKit Bridge.

    Options win over data, which is how 2.x stored a move into or out of a
    bridge. An entry that was already moved to standalone (``bridge`` set to
    null in options) keeps working. Anything still pointing at a bridge is
    left as it is: there is no automatic migration.
    """
    return bool(entry_config(entry).get(CONF_BRIDGE))


def _issue_id(entry_id: str, kind: str) -> str:
    return f"{kind}_{entry_id}"


@callback
def _async_clear_bridge_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Drop bridge-mode repairs for an entry that is gone or running standalone."""
    for kind in (*_LEGACY_BRIDGE_ISSUES, BRIDGE_REMOVED):
        ir.async_delete_issue(hass, DOMAIN, _issue_id(entry_id, kind))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the pairing QR code view.

    Nothing here reaches into core HomeKit. Accessories are published by our
    own HAP server when each entry is set up.
    """
    async_register_view(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: HomeKitExtendedConfigEntry
) -> bool:
    """Start publishing the entry's accessory."""
    if is_bridge_mode(entry):
        # Refuse, and do not read or write core HomeKit. The repair tells the
        # user to delete this entry and add the accessory again standalone.
        for kind in _LEGACY_BRIDGE_ISSUES:
            ir.async_delete_issue(hass, DOMAIN, _issue_id(entry.entry_id, kind))
        ir.async_create_issue(
            hass,
            DOMAIN,
            _issue_id(entry.entry_id, BRIDGE_REMOVED),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=BRIDGE_REMOVED,
            translation_placeholders={"name": entry.title},
        )
        raise ConfigEntryError(
            f"{entry.title} was added to a HomeKit Bridge. That mode was "
            "removed, and this entry was not converted. Delete it and add "
            "the accessory again with its own pairing. See Repairs for the steps.",
            translation_domain=DOMAIN,
            translation_key=BRIDGE_REMOVED,
            translation_placeholders={"name": entry.title},
        )
    _async_clear_bridge_issues(hass, entry.entry_id)

    @callback
    def _pairing_changed() -> None:
        async_show_pairing(hass, server)
        async_dispatcher_send(hass, SIGNAL_PAIRING_CHANGED.format(entry.entry_id))

    server = HomeKitAccessoryServer(hass, entry, _pairing_changed)
    try:
        await server.async_start()
    except OSError as err:
        port = entry_config(entry)[CONF_PORT]
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
    """Forget our pairing and stored IDs so a re-added accessory starts fresh.

    This deletes only HomeKit Extended's files (pairing state, instance IDs,
    accessory IDs). It does not open core HomeKit's AID storage or bridge.
    An accessory that 2.x injected into a HomeKit Bridge can remain listed
    there until that HomeKit Bridge entry is reloaded or the HomeKit Bridge
    integration is reset.
    """
    await async_reset_pairing_state(hass, entry)
    await Store(
        hass, IID_STORAGE_VERSION, iid_storage_key(entry.entry_id)
    ).async_remove()
    _async_clear_bridge_issues(hass, entry.entry_id)
    await Store(
        hass, AID_STORAGE_VERSION, aid_storage_key(entry.entry_id)
    ).async_remove()
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
