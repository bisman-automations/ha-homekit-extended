"""Building blocks shared by every HomeKit Extended accessory type."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import logging
from typing import Any

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.service import Service
import voluptuous as vol

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

from ..const import MANUFACTURER, VERSION
from ..helpers import to_float

_LOGGER = logging.getLogger(__name__)

IGNORED_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN}

CHAR_CONFIGURED_NAME = "ConfiguredName"
CHAR_NAME = "Name"
CHAR_SERVICE_LABEL_INDEX = "ServiceLabelIndex"
CHAR_SERVICE_LABEL_NAMESPACE = "ServiceLabelNamespace"
SERV_SERVICE_LABEL = "ServiceLabel"
LABEL_NAMESPACE_ARABIC_NUMERALS = 1

AccessoryFactory = Callable[[HomeAssistant, AccessoryDriver, ConfigEntry], Accessory]
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
    """User-facing, enabled entities of a device, in registry order."""
    registry = er.async_get(hass)
    return [
        entry
        for entry in er.async_entries_for_device(registry, device_id)
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
    """Accessory that mirrors Home Assistant entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        driver: AccessoryDriver,
        entry: ConfigEntry,
        model: str,
    ) -> None:
        """Initialize the accessory and its information service."""
        super().__init__(driver=driver, display_name=entry.title)
        self.hass = hass
        self.entry = entry
        self.data: dict[str, Any] = {**entry.data, **entry.options}
        self._subscriptions: list[CALLBACK_TYPE] = []
        self.set_info_service(
            manufacturer=MANUFACTURER,
            model=model,
            serial_number=entry.entry_id,
            firmware_revision=VERSION,
        )

    @property
    def name(self) -> str:
        """Return the accessory name."""
        return self.display_name

    def add_named_service(
        self,
        service_type: str,
        name: str,
        chars: list[str] | None = None,
        *,
        label_index: int | None = None,
        unique_id: str | None = None,
    ) -> Service:
        """Add a service carrying Name and ConfiguredName."""
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
