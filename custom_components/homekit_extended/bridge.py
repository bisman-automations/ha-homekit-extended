"""Publish a HomeKit Extended accessory inside a core HomeKit Bridge.

Core HomeKit builds its bridge's accessories in one step when the bridge
starts. This hooks that step so accessories assigned to the bridge are added
before it is announced, and adds or removes them on a running bridge when a
HomeKit Extended entry loads or unloads. Accessory IDs come from the bridge's
own storage, so Apple Home keeps rooms, scenes and automations.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Any

from pyhap.accessory import Accessory

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .accessories import ACCESSORY_TYPES
from .accessories.base import HomeAccessory, configured_entity_ids
from .const import CONF_ACCESSORY_TYPE, CONF_BRIDGE, DOMAIN, HOMEKIT_DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.homekit import HomeKit

_LOGGER = logging.getLogger(__name__)

DATA_BRIDGED = f"{DOMAIN}_bridged"
HOOK_MARKER = "_homekit_extended_hook"
# Apple allows 150 accessories per bridge, counting the bridge itself.
MAX_BRIDGED = 150


def aid_key(entry_id: str) -> str:
    """Key under which core's AID storage remembers our accessory."""
    return f"{DOMAIN}.{entry_id}"


def homekit_bridges(hass: HomeAssistant) -> dict[str, str]:
    """Core HomeKit entries running in bridge mode: entry id -> title."""
    from homeassistant.components.homekit.const import (
        CONF_HOMEKIT_MODE,
        DEFAULT_HOMEKIT_MODE,
        HOMEKIT_MODE_BRIDGE,
    )

    return {
        entry.entry_id: entry.title
        for entry in hass.config_entries.async_entries(HOMEKIT_DOMAIN)
        if {**entry.data, **entry.options}.get(CONF_HOMEKIT_MODE, DEFAULT_HOMEKIT_MODE)
        == HOMEKIT_MODE_BRIDGE
    }


def running_homekit(hass: HomeAssistant, entry_id: str) -> HomeKit | None:
    """The core HomeKit instance of a loaded entry."""
    entry = hass.config_entries.async_get_entry(entry_id)
    data = getattr(entry, "runtime_data", None) if entry else None
    return getattr(data, "homekit", None)


def bridged_entity_ids(hass: HomeAssistant, entry_id: str) -> set[str]:
    """Entities a running core bridge publishes as its own accessories."""
    homekit = running_homekit(hass, entry_id)
    bridge = getattr(homekit, "bridge", None)
    if bridge is None:
        return set()
    return {
        entity_id
        for accessory in bridge.accessories.values()
        if not isinstance(accessory, HomeAccessory)
        and (entity_id := getattr(accessory, "entity_id", None))
    }


def _registry(hass: HomeAssistant) -> dict[str, dict[str, BridgedAccessoryServer]]:
    return hass.data.setdefault(DATA_BRIDGED, {})


def async_install_hook(hass: HomeAssistant) -> bool:
    """Wrap core HomeKit's bridge creation once; False if core has changed."""
    from homeassistant.components.homekit import HomeKit

    original: Callable[..., Any] | None = getattr(
        HomeKit, "_async_create_bridge_accessory", None
    )
    if original is None:
        return False
    if getattr(original, HOOK_MARKER, False):
        return True

    async def _async_create_bridge_accessory(
        self: HomeKit, *args: Any, **kwargs: Any
    ) -> Accessory:
        bridge = await original(self, *args, **kwargs)
        attached = _registry(self.hass).get(self._entry_id, {})  # noqa: SLF001
        for server in list(attached.values()):
            server.async_attach(self)
        return bridge

    setattr(_async_create_bridge_accessory, HOOK_MARKER, True)
    HomeKit._async_create_bridge_accessory = _async_create_bridge_accessory  # type: ignore[method-assign]  # noqa: SLF001
    return True


@callback
def async_follow_bridges(hass: HomeAssistant) -> Callable[[], None]:
    """Reload our accessories when the core bridge they live in is deleted."""

    @callback
    def _changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if entry.domain != HOMEKIT_DOMAIN or change is not ConfigEntryChange.REMOVED:
            return
        for server in list(_registry(hass).get(entry.entry_id, {}).values()):
            hass.config_entries.async_schedule_reload(server.entry.entry_id)

    return async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _changed)


def _update_accessories_hash(homekit: HomeKit) -> None:
    """Tell paired controllers the bridge's accessories changed."""
    from homeassistant.components.homekit import STATUS_RUNNING

    driver = homekit.driver
    if driver is None or homekit.status != STATUS_RUNNING:
        return
    if driver.state.set_accessories_hash(driver.accessories_hash):
        driver.async_persist()
        driver.async_update_advertisement()


class BridgedAccessoryServer:
    """An accessory published inside a core HomeKit Bridge."""

    bridged = True
    driver = None
    iid_storage = None

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.accessory_type = ACCESSORY_TYPES[entry.data[CONF_ACCESSORY_TYPE]]
        self.bridge_entry_id: str = self._config[CONF_BRIDGE]
        self.homekit: HomeKit | None = None
        self.accessory: Accessory | None = None
        self._published = (entry.title, self._config)

    @property
    def _config(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    @property
    def bridge_name(self) -> str:
        """Title of the core HomeKit entry."""
        entry = self.hass.config_entries.async_get_entry(self.bridge_entry_id)
        return entry.title if entry else self.bridge_entry_id

    @property
    def paired(self) -> bool:
        """Return true if the bridge is paired with Apple Home."""
        driver = getattr(self.homekit, "driver", None)
        return bool(driver and driver.state.paired)

    @property
    def attached(self) -> bool:
        """Return true while the accessory is in a running bridge."""
        bridge = getattr(self.homekit, "bridge", None)
        return (
            self.accessory is not None
            and bridge is not None
            and bridge.accessories.get(self.accessory.aid) is self.accessory
        )

    def async_apply_in_place(self) -> bool:
        """Apply option changes that only update characteristic values."""
        from .accessories.base import IN_PLACE_KEYS

        title, config = self.entry.title, self._config
        old_title, old_config = self._published

        def structural(values: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in values.items() if k not in IN_PLACE_KEYS}

        if title != old_title or structural(config) != structural(old_config):
            return False
        if config != old_config and self.accessory is not None:
            self.accessory.apply_in_place(config)  # type: ignore[attr-defined]
        self._published = (title, config)
        return True

    async def async_start(self) -> None:
        """Join the bridge now if it's built, or when it next starts."""
        _registry(self.hass).setdefault(self.bridge_entry_id, {})[
            self.entry.entry_id
        ] = self
        homekit = running_homekit(self.hass, self.bridge_entry_id)
        if homekit is not None and getattr(homekit, "bridge", None) is not None:
            self.async_attach(homekit)

    @callback
    def async_attach(self, homekit: HomeKit) -> None:
        """Add the accessory to a core bridge that has just been built."""
        bridge = homekit.bridge
        assert bridge is not None and homekit.aid_storage is not None
        if self.homekit is homekit and self.attached:
            return
        if (previous := self._async_discard()) is not None:
            self.hass.async_create_task(previous.async_stop())  # type: ignore[attr-defined]
        if len(bridge.accessories) + 1 >= MAX_BRIDGED:
            _LOGGER.warning(
                "Can't add %s to %s: the bridge already has the most accessories "
                "HomeKit allows",
                self.entry.title,
                self.bridge_name,
            )
            self._async_issue("bridge_full")
            return
        ir.async_delete_issue(self.hass, DOMAIN, f"bridge_full_{self.entry.entry_id}")
        key = aid_key(self.entry.entry_id)
        aid = homekit.aid_storage.get_or_allocate_aid(key, key)
        accessory = self.accessory_type.factory(
            self.hass, homekit.driver, self.entry, aid
        )
        bridge.add_accessory(accessory)
        self.homekit, self.accessory = homekit, accessory
        _update_accessories_hash(homekit)
        self.async_check_duplicates()
        _LOGGER.info("Added '%s' to %s", self.entry.title, self.bridge_name)

    @callback
    def async_check_duplicates(self) -> None:
        """Raise a repair if the bridge also publishes our entities itself."""
        duplicates = sorted(
            set(configured_entity_ids(self._config))
            & bridged_entity_ids(self.hass, self.bridge_entry_id)
        )
        issue_id = f"duplicate_entities_{self.entry.entry_id}"
        if not duplicates:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="duplicate_entities",
            translation_placeholders={
                "name": self.entry.title,
                "bridge": self.bridge_name,
                "entities": ", ".join(f"`{e}`" for e in duplicates),
            },
        )

    def _async_issue(self, kind: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{kind}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=kind,
            translation_placeholders={
                "name": self.entry.title,
                "bridge": self.bridge_name,
            },
        )

    @callback
    def _async_discard(self) -> Accessory | None:
        """Take the accessory out of its bridge, if it's still there."""
        accessory, homekit = self.accessory, self.homekit
        self.accessory = self.homekit = None
        if accessory is None:
            return None
        bridge = getattr(homekit, "bridge", None)
        if bridge is not None and bridge.accessories.get(accessory.aid) is accessory:
            del bridge.accessories[accessory.aid]
            _update_accessories_hash(homekit)  # type: ignore[arg-type]
        return accessory

    async def async_stop(self) -> None:
        """Leave the bridge."""
        attached = _registry(self.hass).get(self.bridge_entry_id, {})
        attached.pop(self.entry.entry_id, None)
        if (accessory := self._async_discard()) is not None:
            await accessory.async_stop()  # type: ignore[attr-defined]
        ir.async_delete_issue(
            self.hass, DOMAIN, f"duplicate_entities_{self.entry.entry_id}"
        )
