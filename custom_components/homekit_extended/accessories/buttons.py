"""Buttons: Home Assistant event entities as HomeKit programmable switches."""

from __future__ import annotations

from typing import Any

from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_PROGRAMMABLE_SWITCH
import voluptuous as vol

from homeassistant.components.event import ATTR_EVENT_TYPE, ATTR_EVENT_TYPES
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from ..const import CONF_EVENTS
from ..helpers import friendly_name
from .base import (
    IGNORED_STATES,
    AccessoryType,
    HomeAccessory,
    entity_field,
    entity_list,
    find_entities,
)

SINGLE_PRESS = 0
DOUBLE_PRESS = 1
LONG_PRESS = 2

SINGLE_NAMES = {
    "single",
    "single_press",
    "short_press",
    "short_release",
    "press",
    "pressed",
    "click",
    "clicked",
    "multi_press_1",
    "1x",
}
# Fired at the start of every press; only used when nothing better exists.
FALLBACK_SINGLE_NAMES = {"initial_press", "press_start", "down"}
DOUBLE_HINTS = ("double", "multi_press_2", "press_2", "2x")
LONG_HINTS = ("long", "hold")


def press_map(event_types: list[str] | None) -> dict[str, int] | None:
    """Map a device's event types to HomeKit presses.

    Returns None when the entity doesn't list its types, meaning every event
    counts as a single press.
    """
    if not event_types:
        return None
    mapping: dict[str, int] = {}
    long_releases: list[str] = []
    fallbacks: list[str] = []
    for event_type in event_types:
        name = event_type.lower()
        if any(hint in name for hint in LONG_HINTS):
            if "release" in name or name.endswith("_up"):
                long_releases.append(event_type)
            else:
                mapping[event_type] = LONG_PRESS
        elif any(hint in name for hint in DOUBLE_HINTS):
            mapping[event_type] = DOUBLE_PRESS
        elif name in SINGLE_NAMES or name.endswith("_single"):
            mapping[event_type] = SINGLE_PRESS
        elif name in FALLBACK_SINGLE_NAMES:
            fallbacks.append(event_type)
    if SINGLE_PRESS not in mapping.values():
        mapping.update(dict.fromkeys(fallbacks, SINGLE_PRESS))
    if LONG_PRESS not in mapping.values():
        mapping.update(dict.fromkeys(long_releases, LONG_PRESS))
    if not mapping:
        # Unrecognized vocabulary: treat the first type as a single press.
        mapping[event_types[0]] = SINGLE_PRESS
    return mapping


def _schema() -> dict[vol.Marker, Any]:
    return {vol.Required(CONF_EVENTS): entity_field("event", multiple=True)}


def _detect(hass: HomeAssistant, entries: list[er.RegistryEntry]) -> dict[str, Any]:
    events = find_entities(
        entries, "event", {"button", "doorbell", None}
    ) or find_entities(entries, "event")
    return {CONF_EVENTS: events} if events else {}


def _normalize(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    data[CONF_EVENTS] = entity_list(data.get(CONF_EVENTS))
    return data, {} if data[CONF_EVENTS] else {CONF_EVENTS: "no_buttons_selected"}


class Button:
    """One programmable switch service mirroring an event entity."""

    def __init__(self, char: Characteristic, state: State | None) -> None:
        """Initialize from the entity's current state."""
        self.char = char
        self.last_seen = state.state if state else None
        self.mapping = (
            press_map(state.attributes.get(ATTR_EVENT_TYPES)) if state else None
        )

    def press_for(self, state: State) -> int | None:
        """Return the HomeKit press for a new event, or None to ignore it."""
        if state.state in IGNORED_STATES or state.state == self.last_seen:
            return None
        self.last_seen = state.state
        if state.attributes.get(ATTR_EVENT_TYPES):
            self.mapping = press_map(state.attributes[ATTR_EVENT_TYPES])
        if self.mapping is None:
            return SINGLE_PRESS
        return self.mapping.get(state.attributes.get(ATTR_EVENT_TYPE))


class ButtonsAccessory(HomeAccessory):
    """One Stateless Programmable Switch per event entity."""

    category = CATEGORY_PROGRAMMABLE_SWITCH

    def __init__(
        self, hass: HomeAssistant, driver: AccessoryDriver, entry: ConfigEntry
    ) -> None:
        """Initialize the accessory."""
        super().__init__(hass, driver, entry, BUTTONS.model)
        self.events = entity_list(self.data.get(CONF_EVENTS))
        self._buttons: dict[str, Button] = {}

        self.add_service_label()
        for index, entity_id in enumerate(self.events, start=1):
            name = friendly_name(hass, entity_id) if len(self.events) > 1 else self.name
            service = self.add_named_service(
                "StatelessProgrammableSwitch",
                name,
                ["ProgrammableSwitchEvent"],
                label_index=index,
                unique_id=entity_id,
            )
            if index == 1:
                service.is_primary_service = True
            char = service.configure_char("ProgrammableSwitchEvent")
            self._buttons[entity_id] = Button(char, hass.states.get(entity_id))

        # Current states were captured above; only react to new events.
        self.track_events()

    def track_events(self) -> None:
        """Subscribe to event entities without replaying their last event."""

        @callback
        def _changed(event: Any) -> None:
            if (state := event.data["new_state"]) is not None:
                self._on_event(state)

        if self.events:
            self._subscriptions.append(
                async_track_state_change_event(self.hass, self.events, _changed)
            )

    @callback
    def _on_event(self, state: State) -> None:
        button = self._buttons.get(state.entity_id)
        if button is None or (press := button.press_for(state)) is None:
            return
        # pyhap reports this characteristic as null between events, so every
        # set_value is a change and notifies, including repeated presses.
        button.char.set_value(press)


BUTTONS = AccessoryType(
    key="buttons",
    model="HomeKit Extended Buttons",
    default_name="Remote",
    device_domains=("event",),
    schema=_schema,
    detect=_detect,
    normalize=_normalize,
    factory=ButtonsAccessory,
    entity_keys=(CONF_EVENTS,),
)
