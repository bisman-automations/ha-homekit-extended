"""Buttons: Home Assistant event entities as HomeKit programmable switches.

Each event entity is usually one physical button. Some integrations instead
put a whole remote on one entity, with event types like ``button_1_single``
and ``button_2_long``; those are split into one HomeKit button per physical
button, worked out from the event types the entity reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
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
    STANDALONE_AID,
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

# Words that describe the press rather than which button was pressed.
PRESS_WORDS = {
    "single",
    "double",
    "triple",
    "quadruple",
    "long",
    "hold",
    "press",
    "pressed",
    "release",
    "released",
    "short",
    "initial",
    "click",
    "clicked",
    "repeat",
    "start",
}
PRESS_PATTERNS = re.compile(r"multi_press_\d+|press_\d+|\b\d+x\b")
WORD_SPLIT = re.compile(r"[_\s\-.]+")


def _tokens(event_type: str) -> list[str]:
    return [t for t in WORD_SPLIT.split(event_type.lower()) if t]


def press_map(event_types: list[str] | None) -> dict[str, int] | None:
    """Map one button's event types to HomeKit presses.

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
        tokens = _tokens(event_type)
        if any(hint in name for hint in LONG_HINTS):
            if "release" in name or name.endswith("_up"):
                long_releases.append(event_type)
            else:
                mapping[event_type] = LONG_PRESS
        elif any(hint in name for hint in DOUBLE_HINTS):
            mapping[event_type] = DOUBLE_PRESS
        elif name in SINGLE_NAMES or "single" in tokens or "click" in tokens:
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


def button_key(event_type: str) -> str:
    """Which physical button an event type belongs to ("" if it doesn't say).

    "button_1_single" -> "button_1", "single_left" -> "left",
    "short_release" -> "", "on" -> "on".
    """
    stripped = PRESS_PATTERNS.sub(" ", event_type.lower())
    return "_".join(t for t in _tokens(stripped) if t not in PRESS_WORDS)


def split_buttons(event_types: list[str] | None) -> dict[str, list[str]]:
    """Group one entity's event types by physical button.

    Returns {"": event_types} for an ordinary single-button entity.
    """
    if not event_types:
        return {"": []}
    groups: dict[str, list[str]] = {}
    for event_type in event_types:
        groups.setdefault(button_key(event_type), []).append(event_type)
    if len([key for key in groups if key]) < 2:
        return {"": list(event_types)}
    return groups


def button_label(key: str) -> str:
    """Readable name for a button key: "button_1" -> "Button 1"."""
    return key.replace("_", " ").title()


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


@dataclass(slots=True)
class Button:
    """One programmable switch service."""

    char: Characteristic
    mapping: dict[str, int] | None


@dataclass(slots=True)
class EventSource:
    """An event entity feeding one or more buttons."""

    last_seen: str | None
    buttons: dict[str, Button] = field(default_factory=dict)

    def press_for(self, state: State) -> tuple[Button, int] | None:
        """Return the button and press for a new event, or None to ignore it."""
        if state.state in IGNORED_STATES or state.state == self.last_seen:
            return None
        self.last_seen = state.state
        event_type = state.attributes.get(ATTR_EVENT_TYPE)
        if "" in self.buttons:
            button = self.buttons[""]
        elif (
            event_type is None
            or (button := self.buttons.get(button_key(event_type))) is None
        ):
            return None
        if button.mapping is None:
            return button, SINGLE_PRESS
        if (press := button.mapping.get(event_type)) is None:
            return None
        return button, press


class ButtonsAccessory(HomeAccessory):
    """One Stateless Programmable Switch per physical button."""

    category = CATEGORY_PROGRAMMABLE_SWITCH

    def __init__(
        self,
        hass: HomeAssistant,
        driver: AccessoryDriver,
        entry: ConfigEntry,
        aid: int = STANDALONE_AID,
    ) -> None:
        """Initialize the accessory."""
        super().__init__(hass, driver, entry, BUTTONS.model, aid)
        self.events = entity_list(self.data.get(CONF_EVENTS))
        self._sources: dict[str, EventSource] = {}

        self.add_service_label()
        layout = []
        for entity_id in self.events:
            state = hass.states.get(entity_id)
            types = state.attributes.get(ATTR_EVENT_TYPES) if state else None
            layout.extend(
                (entity_id, key, group) for key, group in split_buttons(types).items()
            )
            self._sources[entity_id] = EventSource(state.state if state else None)

        for index, (entity_id, key, group) in enumerate(layout, start=1):
            if len(layout) == 1:
                name = self.name
            elif key:
                name = f"{friendly_name(hass, entity_id)} {button_label(key)}"
            else:
                name = friendly_name(hass, entity_id)
            service = self.add_named_service(
                "StatelessProgrammableSwitch",
                name,
                ["ProgrammableSwitchEvent"],
                label_index=index,
                unique_id=f"{entity_id}#{key}" if key else entity_id,
            )
            if index == 1:
                service.is_primary_service = True
            self._sources[entity_id].buttons[key] = Button(
                service.configure_char("ProgrammableSwitchEvent"),
                press_map(group),
            )

        # Current states were captured above; only react to new events.
        @callback
        def _changed(event: Any) -> None:
            if (state := event.data["new_state"]) is not None:
                self._on_event(state)

        if self.events:
            self._subscriptions.append(
                async_track_state_change_event(hass, self.events, _changed)
            )

    @callback
    def _on_event(self, state: State) -> None:
        source = self._sources.get(state.entity_id)
        if source is None or (result := source.press_for(state)) is None:
            return
        button, press = result
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
