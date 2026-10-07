"""Building blocks shared by every HomeKit Extended accessory type."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import logging
import re
from typing import Any

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.const import STANDALONE_AID
from pyhap.service import Service
import voluptuous as vol

from homeassistant.components.homekit.accessories import HomeIIDManager
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector
from homeassistant.helpers.event import async_track_state_change_event

from ..const import (
    CONF_DEVICE,
    CONF_FIRMWARE,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_SERIAL,
    MANUFACTURER,
    VERSION,
)
from ..helpers import to_float

_LOGGER = logging.getLogger(__name__)

IGNORED_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN}

CHAR_CONFIGURED_NAME = "ConfiguredName"
CHAR_NAME = "Name"
CHAR_SERVICE_LABEL_INDEX = "ServiceLabelIndex"
CHAR_SERVICE_LABEL_NAMESPACE = "ServiceLabelNamespace"
SERV_SERVICE_LABEL = "ServiceLabel"
LABEL_NAMESPACE_ARABIC_NUMERALS = 1

AccessoryFactory = Callable[..., Accessory]
Detector = Callable[[HomeAssistant, list[er.RegistryEntry]], dict[str, Any]]
Normalizer = Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, str]]]


@dataclass(frozen=True, slots=True)
class AccessoryType:
    """Everything the config flow and server need to know about a type."""

    key: str
    model: str
    default_name: str
    # Domains a source device must expose to be offered in the device picker.
    device_domains: tuple[str, ...]
    # Entity fields shown in the "entities" step and in options.
    schema: Callable[[], dict[vol.Marker, Any]]
    # Pre-fills entity fields from a chosen device's entities.
    detect: Detector
    # Cleans submitted entity fields and returns (data, errors).
    normalize: Normalizer
    factory: AccessoryFactory
    # Config keys holding entity ids (single id or list), for bookkeeping.
    entity_keys: tuple[str, ...] = field(default=())
    # Config key of the valves that get their own run times, if any.
    zones_key: str | None = None


INFO_KEYS = (CONF_MANUFACTURER, CONF_MODEL, CONF_SERIAL, CONF_FIRMWARE)
INFO_CHARS = {
    CONF_MANUFACTURER: "Manufacturer",
    CONF_MODEL: "Model",
    CONF_SERIAL: "SerialNumber",
    CONF_FIRMWARE: "FirmwareRevision",
}
# Options a running accessory applies without re-publishing (they only change
# characteristic values, never the accessory's structure).
IN_PLACE_KEYS = frozenset({*INFO_KEYS, "run_times", "disabled_zones"})
# HomeKit requires a numeric firmware version: 1, 1.2 or 1.2.3.
FIRMWARE_RE = re.compile(r"^\d+(\.\d+){0,2}$")
MAX_INFO_LENGTH = 64


def firmware_version(raw: Any) -> str | None:
    """Return the leading HomeKit-valid version in a string, if any.

    "1.2.3-beta" -> "1.2.3", "v2.0" -> "2.0", "2026.10.1.4" -> "2026.10.1".
    """
    match = re.search(r"\d+(\.\d+){0,2}", str(raw or ""))
    return match.group(0) if match else None


ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


def configured_entity_ids(data: dict[str, Any]) -> list[str]:
    """Entity ids in an entry's config, in order (lists and legacy dicts too)."""
    found: list[str] = []
    for value in data.values():
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                item = item.get("entity_id")
            if isinstance(item, str) and ENTITY_ID_RE.match(item):
                found.append(item)
    return found


def source_device_info(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Manufacturer, model, serial and firmware of the device being represented.

    Uses the device picked during setup, or else the device of the first
    configured entity. Missing details are taken from the device it's
    connected through (a Rain Bird zone's controller, for example). A device
    without a serial number falls back to its MAC address.
    """
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    device = devices.async_get(data[CONF_DEVICE]) if data.get(CONF_DEVICE) else None
    if device is None:
        for entity_id in configured_entity_ids(data):
            entry = registry.async_get(entity_id)
            if entry and entry.device_id:
                device = devices.async_get(entry.device_id)
                if device is not None:
                    break
    info: dict[str, str] = {}
    seen: set[str] = set()
    while device is not None and device.id not in seen:
        seen.add(device.id)
        mac = next(
            (
                value.upper()
                for kind, value in device.connections
                if kind == dr.CONNECTION_NETWORK_MAC
            ),
            None,
        )
        for key, value in (
            (CONF_MANUFACTURER, device.manufacturer),
            (CONF_MODEL, device.model),
            (CONF_SERIAL, device.serial_number or mac),
            (CONF_FIRMWARE, firmware_version(device.sw_version)),
        ):
            if value:
                info.setdefault(key, str(value))
        device = (
            devices.async_get(device.via_device_id) if device.via_device_id else None
        )
    return info


def accessory_info(
    data: dict[str, Any],
    model: str,
    serial: str,
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    """Accessory information to publish.

    Each field uses, in order: what the user set, the represented device's
    value, then HomeKit Extended's default.
    """
    defaults = {
        CONF_MANUFACTURER: MANUFACTURER,
        CONF_MODEL: model,
        CONF_SERIAL: serial,
        CONF_FIRMWARE: VERSION,
    }
    source = source or {}
    info = {
        key: str(data.get(key) or source.get(key) or default)[:MAX_INFO_LENGTH]
        for key, default in defaults.items()
    }
    if len(info[CONF_SERIAL]) < 2:
        info[CONF_SERIAL] = serial
    return info


# Selector helpers


def entity_field(
    domain: str | list[str],
    *,
    device_class: str | list[str] | None = None,
    multiple: bool = False,
) -> selector.EntitySelector:
    """Entity picker limited to a domain and optional device classes."""
    entity_filter = selector.EntityFilterSelectorConfig(domain=domain)
    if device_class is not None:
        entity_filter["device_class"] = device_class
    return selector.EntitySelector(
        selector.EntitySelectorConfig(filter=entity_filter, multiple=multiple)
    )


def optional_entities(data: dict[str, Any], keys: Iterable[str]) -> None:
    """Normalize cleared optional pickers to None in place."""
    for key in keys:
        data[key] = data.get(key) or None


def entity_list(raw: Any) -> list[str]:
    """Normalize stored entity lists, accepting legacy {"entity_id": ...} items."""
    entity_ids: list[str] = []
    for item in raw or []:
        entity_id = item["entity_id"] if isinstance(item, dict) else str(item)
        if entity_id not in entity_ids:
            entity_ids.append(entity_id)
    return entity_ids


# Device auto-detection


def device_entities(hass: HomeAssistant, device_id: str) -> list[er.RegistryEntry]:
    """User-facing, enabled entities of a device and the devices under it.

    Controllers often put each zone, outlet or button on its own device linked
    to the controller (Rain Bird zones, for example), so picking the controller
    should find them too.
    """
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    device_ids = [device_id] + [
        device.id
        for device in devices.devices.values()
        if device.via_device_id == device_id
    ]
    return [
        entry
        for each_id in device_ids
        for entry in er.async_entries_for_device(registry, each_id)
        if entry.disabled_by is None and entry.entity_category is None
    ]


def device_name(hass: HomeAssistant, device_id: str) -> str | None:
    """Return the name a user sees for a device."""
    if (device := dr.async_get(hass).async_get(device_id)) is None:
        return None
    return device.name_by_user or device.name


def device_class_of(entry: er.RegistryEntry) -> str | None:
    """User override first, then the integration's device class."""
    return entry.device_class or entry.original_device_class


def find_entity(
    entries: list[er.RegistryEntry],
    domain: str,
    device_classes: str | Iterable[str] | None = None,
) -> str | None:
    """First entity of a domain, optionally with one of the device classes."""
    found = find_entities(entries, domain, device_classes)
    return found[0] if found else None


def find_entities(
    entries: list[er.RegistryEntry],
    domain: str,
    device_classes: str | Iterable[str] | None = None,
) -> list[str]:
    """All entities of a domain, optionally with one of the device classes."""
    if isinstance(device_classes, str):
        device_classes = {device_classes}
    elif device_classes is not None:
        device_classes = set(device_classes)
    return [
        entry.entity_id
        for entry in entries
        if entry.domain == domain
        and (device_classes is None or device_class_of(entry) in device_classes)
    ]


def drop_empty(values: dict[str, Any]) -> dict[str, Any]:
    """Keep only detected values."""
    return {key: value for key, value in values.items() if value}


# Accessory base


class HomeAccessory(Accessory):
    """Accessory that mirrors Home Assistant entities.

    Instance IDs come from Home Assistant's HomeKit IID storage, the same as
    core HomeKit Bridge accessories, so a service keeps its ID when zones,
    outlets or buttons are added or removed and Apple Home keeps its settings.
    """

    # Read by core HomeKit's diagnostics when the accessory is in its bridge.
    entity_id: str | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        driver: AccessoryDriver,
        entry: ConfigEntry,
        model: str,
        aid: int = STANDALONE_AID,
    ) -> None:
        """Initialize the accessory and its information service."""
        iid_storage = getattr(driver, "iid_storage", None)
        super().__init__(
            driver=driver,
            display_name=entry.title,
            aid=aid,
            iid_manager=HomeIIDManager(iid_storage) if iid_storage else None,
        )
        self.hass = hass
        self.entry = entry
        self.data: dict[str, Any] = {**entry.data, **entry.options}
        # Read by core HomeKit's diagnostics, which don't redact it.
        self.config = {k: v for k, v in self.data.items() if k != "pin"}
        self._subscriptions: list[CALLBACK_TYPE] = []
        self._tracked: list[str] = []
        self._default_model = model
        info = accessory_info(
            self.data, model, entry.entry_id, source_device_info(hass, self.data)
        )
        self.set_info_service(
            manufacturer=info[CONF_MANUFACTURER],
            model=info[CONF_MODEL],
            serial_number=info[CONF_SERIAL],
            firmware_revision=info[CONF_FIRMWARE],
        )

    def apply_in_place(self, config: dict[str, Any]) -> None:
        """Update accessory information without re-publishing."""
        info = accessory_info(
            config,
            self._default_model,
            self.entry.entry_id,
            source_device_info(self.hass, config),
        )
        service = self.get_service("AccessoryInformation")
        for key, char_name in INFO_CHARS.items():
            service.get_characteristic(char_name).set_value(info[key])

    def add_protocol_version_service(self) -> None:
        """Leave out HAP's protocol information service.

        pyhap adds it when an accessory is created with the standalone AID.
        Versions before 2.0.0 created accessories without an AID, so they
        never had it; adding it now would shift every instance ID after the
        accessory information service and break existing pairings.
        """

    @property
    def name(self) -> str:
        """Return the accessory name."""
        return self.display_name

    @property
    def available(self) -> bool:
        """Show "No Response" in Apple Home when every entity is unavailable."""
        if not self._tracked:
            return True
        return any(
            (state := self.hass.states.get(entity_id)) is not None
            and state.state != STATE_UNAVAILABLE
            for entity_id in self._tracked
        )

    def add_named_service(
        self,
        service_type: str,
        name: str,
        chars: list[str] | None = None,
        *,
        label_index: int | None = None,
        unique_id: str | None = None,
    ) -> Service:
        """Add a service carrying Name and ConfiguredName.

        Repeated service types need a unique_id so each keeps its own stored
        instance IDs; the name stands in when the caller didn't give one.
        """
        if unique_id is None and any(
            existing.display_name == service_type for existing in self.services
        ):
            unique_id = name
        extra = [CHAR_SERVICE_LABEL_INDEX] if label_index is not None else []
        service = self.add_preload_service(
            service_type,
            [CHAR_NAME, CHAR_CONFIGURED_NAME, *extra, *(chars or [])],
            unique_id=unique_id,
        )
        service.configure_char(CHAR_NAME, value=name)
        service.configure_char(CHAR_CONFIGURED_NAME, value=name)
        if label_index is not None:
            service.configure_char(CHAR_SERVICE_LABEL_INDEX, value=label_index)
        return service

    def add_service_label(self) -> Service:
        """Tell HomeKit that services are numbered (buttons, outlets)."""
        service = self.add_preload_service(
            SERV_SERVICE_LABEL, [CHAR_SERVICE_LABEL_NAMESPACE]
        )
        service.configure_char(
            CHAR_SERVICE_LABEL_NAMESPACE, value=LABEL_NAMESPACE_ARABIC_NUMERALS
        )
        return service

    def track(
        self,
        handlers: dict[str, Callable[[State], None]],
    ) -> None:
        """Mirror the current state of each entity now and on every change."""
        handlers = {entity_id: h for entity_id, h in handlers.items() if entity_id}
        if not handlers:
            return
        self._tracked.extend(e for e in handlers if e not in self._tracked)
        for entity_id, handler in handlers.items():
            if (state := self.hass.states.get(entity_id)) is not None:
                handler(state)

        @callback
        def _changed(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            if new_state is not None and (handler := handlers.get(new_state.entity_id)):
                handler(new_state)

        self._subscriptions.append(
            async_track_state_change_event(self.hass, list(handlers), _changed)
        )

    def call_service(
        self, domain: str, service: str, entity_id: str, **data: Any
    ) -> None:
        """Call a Home Assistant service without blocking the HAP request."""
        _LOGGER.debug("%s: %s.%s %s %s", self.name, domain, service, entity_id, data)
        self.hass.async_create_task(
            self.hass.services.async_call(
                domain, service, {ATTR_ENTITY_ID: entity_id, **data}, blocking=False
            )
        )

    async def stop(self) -> None:
        """Called by the driver (or core's bridge) when it stops."""
        await self.async_stop()

    async def async_stop(self) -> None:
        """Release Home Assistant listeners."""
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()


def numeric_state(state: State) -> float | None:
    """Return a usable numeric state, or None."""
    if state.state in IGNORED_STATES:
        return None
    return to_float(state.state)


def set_clamped(char: Characteristic, value: float) -> None:
    """Set a numeric characteristic, clamped to its HAP range."""
    props = char.properties
    low = props.get("minValue", value)
    high = props.get("maxValue", value)
    char.set_value(max(low, min(high, value)))
