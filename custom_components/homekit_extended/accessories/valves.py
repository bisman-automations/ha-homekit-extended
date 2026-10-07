"""Valve groups: an Irrigation System with zones, or a Faucet/Shower with outlets.

Everything here maps onto native HomeKit behavior:

- Each zone has its own run time (SetDuration) and closes itself when it ends.
- Turning the Irrigation System on runs every enabled zone in turn; zones a
  user turns off in Apple Home (IsConfigured) are skipped.
- "One zone at a time" closes the running zone when another one starts.
- A valve that is unavailable shows as a fault (StatusFault).
- An optional pump or master valve runs while any zone is open. It is driven
  from Home Assistant and never exposed to HomeKit.
- An irrigation system's zones can be laid out three ways: as zones of an
  Irrigation System (the default); as plain, numbered Valve services on one
  accessory, which Apple Home can show as separate tiles; or as one accessory
  per zone, published as a HomeKit bridge (or as separate accessories in a
  core HomeKit Bridge). In every layout this class coordinates the zones, so
  one zone at a time, the pump and controller countdowns work the same.
- When a valve's device also has a run-time number and an end-time sensor
  (Rain Bird Extended, for example), the controller owns the run: run times
  read and write that number, the countdown comes from that sensor, and the
  controller closes the valve itself.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_FAUCET, CATEGORY_SPRINKLER
import voluptuous as vol

from homeassistant.components.homekit.accessories import HomeIIDManager
from homeassistant.components.valve import DOMAIN as VALVE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SERVICE_CLOSE_VALVE,
    SERVICE_OPEN_VALVE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er, selector

from ..const import (
    CONF_DEFAULT_DURATION,
    CONF_DISABLED_ZONES,
    CONF_MASTER,
    CONF_ONE_AT_A_TIME,
    CONF_RUN_TIMES,
    CONF_SEPARATE_ZONES,
    CONF_USE_CONTROLLER,
    CONF_VALVE_TYPE,
    CONF_VALVES,
    CONF_ZONE_LAYOUT,
    DEFAULT_DURATION,
    VALVE_OPEN_STATES,
)
from ..helpers import friendly_name
from .base import (
    IGNORED_STATES,
    STANDALONE_AID,
    AccessoryType,
    HomeAccessory,
    accessory_info,
    add_named_service,
    entity_field,
    entity_list,
    find_entities,
    set_accessory_info,
    source_device_info,
)
from .zone_links import (
    ZoneLinks,
    duration_limits,
    duration_seconds,
    find_zone_links,
    seconds_per_unit,
    seconds_until,
)

CHAR_ACTIVE = "Active"
CHAR_IN_USE = "InUse"
CHAR_IS_CONFIGURED = "IsConfigured"
CHAR_PROGRAM_MODE = "ProgramMode"
CHAR_REMAINING_DURATION = "RemainingDuration"
CHAR_SET_DURATION = "SetDuration"
CHAR_STATUS_FAULT = "StatusFault"
CHAR_VALVE_TYPE = "ValveType"

MAX_DURATION = 3600
DURATION_PROPERTIES = {"minValue": 0, "maxValue": MAX_DURATION, "minStep": 1}
# Matches core HomeKit: pyhap clamps RemainingDuration to maxValue, so allow
# long controller-side runs to count down correctly.
LINKED_REMAINING_MAX = 48 * 3600
# Keep the pump running briefly so it doesn't cycle between zones.
PUMP_OFF_DELAY = 5

HK_ACTIVE = 1
HK_INACTIVE = 0
HK_IN_USE = 1
HK_NOT_IN_USE = 0
HK_NO_PROGRAM_SCHEDULED = 0
HK_CONFIGURED = 1
HK_NOT_CONFIGURED = 0
HK_NO_FAULT = 0
HK_FAULT = 1
HK_VALVE_TYPES = {"irrigation": 1, "shower_head": 2, "faucet": 3}
FAUCET_VALVE_TYPES = ("shower_head", "faucet")
MASTER_DOMAINS = ["switch", "input_boolean", "valve"]

# How an irrigation system's zones appear in HomeKit.
LAYOUT_SYSTEM = "system"
LAYOUT_VALVES = "valves"
LAYOUT_ACCESSORIES = "accessories"
ZONE_LAYOUTS = (LAYOUT_SYSTEM, LAYOUT_VALVES, LAYOUT_ACCESSORIES)


def zone_layout(data: dict[str, Any]) -> str:
    """The configured layout; 2.1.0's "separate zones" switch means valves."""
    if (layout := data.get(CONF_ZONE_LAYOUT)) in ZONE_LAYOUTS:
        return layout
    return LAYOUT_VALVES if data.get(CONF_SEPARATE_ZONES) else LAYOUT_SYSTEM


def valve_entity_ids(raw: Any) -> list[str]:
    """Normalize stored valves (plain ids or legacy {"entity_id": ...} mappings)."""
    return entity_list(raw)


def _duration_field() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=MAX_DURATION,
            step=1,
            unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _irrigation_schema() -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_VALVES): entity_field("valve", multiple=True),
        vol.Required(
            CONF_DEFAULT_DURATION, default=DEFAULT_DURATION
        ): _duration_field(),
        vol.Required(CONF_ONE_AT_A_TIME, default=True): selector.BooleanSelector(),
        vol.Optional(CONF_MASTER): entity_field(MASTER_DOMAINS),
        vol.Required(CONF_USE_CONTROLLER, default=True): selector.BooleanSelector(),
        vol.Required(CONF_ZONE_LAYOUT, default=LAYOUT_SYSTEM): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(ZONE_LAYOUTS),
                translation_key=CONF_ZONE_LAYOUT,
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
    }


def _faucet_schema() -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_VALVES): entity_field("valve", multiple=True),
        vol.Required(CONF_VALVE_TYPE, default="shower_head"): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(FAUCET_VALVE_TYPES),
                translation_key=CONF_VALVE_TYPE,
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
        vol.Required(CONF_DEFAULT_DURATION, default=0): _duration_field(),
        vol.Required(CONF_USE_CONTROLLER, default=True): selector.BooleanSelector(),
    }


def _detect(hass: HomeAssistant, entries: list[er.RegistryEntry]) -> dict[str, Any]:
    valves = find_entities(entries, "valve", {"water", None}) or find_entities(
        entries, "valve"
    )
    return {CONF_VALVES: valves} if valves else {}


def _normalize(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    data[CONF_VALVES] = valve_entity_ids(data.get(CONF_VALVES))
    data[CONF_DEFAULT_DURATION] = int(data.get(CONF_DEFAULT_DURATION) or 0)
    if CONF_ZONE_LAYOUT in data:
        # Replaces 2.1.0's switch; clear it so it can't override the layout.
        data[CONF_SEPARATE_ZONES] = False
    if CONF_ONE_AT_A_TIME in data or CONF_MASTER in data:
        data[CONF_MASTER] = data.get(CONF_MASTER) or None
    errors: dict[str, str] = {}
    if not data[CONF_VALVES]:
        errors[CONF_VALVES] = "no_valves_selected"
    elif data.get(CONF_MASTER) in data[CONF_VALVES]:
        errors[CONF_MASTER] = "master_is_zone"
    return data, errors


def zone_run_times(data: dict[str, Any], valves: list[str]) -> dict[str, int]:
    """Each zone's run time, falling back to the default for new zones."""
    default = min(int(data.get(CONF_DEFAULT_DURATION, DEFAULT_DURATION)), MAX_DURATION)
    stored = data.get(CONF_RUN_TIMES) or {}
    return {
        entity_id: max(0, min(int(stored.get(entity_id, default)), MAX_DURATION))
        for entity_id in valves
    }


def run_times_form(
    hass: HomeAssistant,
    valves: list[str],
    run_times: dict[str, int],
    use_controller: bool = True,
) -> tuple[dict[vol.Marker, Any], dict[str, str], dict[str, int]]:
    """One run-time field per valve, labeled with the valve's name.

    Zones whose controller has its own run-time entity show and edit that
    value, within the controller's limits.

    Returns (schema, field label -> entity id, suggested values).
    """
    labels: dict[str, str] = {}
    schema: dict[vol.Marker, Any] = {}
    suggested: dict[str, int] = {}
    for entity_id in valves:
        label = friendly_name(hass, entity_id)
        if label in labels:
            label = f"{label} ({entity_id})"
        labels[label] = entity_id
        field_selector = _duration_field()
        value = run_times[entity_id]
        links = find_zone_links(hass, entity_id) if use_controller else ZoneLinks()
        if links.duration:
            state = hass.states.get(links.duration)
            if (limits := duration_limits(state)) is not None:
                low, high, step = limits
                field_selector = selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=low,
                        max=high,
                        step=step,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            if (linked := duration_seconds(state)) is not None:
                value = linked
        schema[vol.Required(label)] = field_selector
        suggested[label] = value
    return schema, labels, suggested


def _human_duration(seconds: int) -> str:
    """1 → "1 second", 60 → "1 minute", 86400 → "24 hours"."""
    for size, unit in ((3600, "hour"), (60, "minute"), (1, "second")):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{seconds} seconds"


def run_times_details(
    hass: HomeAssistant, valves: list[str], use_controller: bool = True
) -> str:
    """Explain which zones save to the controller and which are stored here."""
    controller: dict[tuple[int, int, int] | None, list[str]] = {}
    local: list[str] = []
    for entity_id in valves:
        links = find_zone_links(hass, entity_id) if use_controller else ZoneLinks()
        name = friendly_name(hass, entity_id)
        if links.duration:
            limits = duration_limits(hass.states.get(links.duration))
            controller.setdefault(limits, []).append(name)
        else:
            local.append(name)
    parts: list[str] = []
    for limits, names in controller.items():
        text = f"**Set on the controller:** {', '.join(names)}."
        if limits is not None:
            low, high, step = limits
            text += (
                f" From {_human_duration(max(low, step))} to {_human_duration(high)}"
                f" in {_human_duration(step)} steps."
            )
        parts.append(text)
    if local:
        parts.append(
            f"**Stored by HomeKit Extended:** {', '.join(local)}."
            f" Up to {_human_duration(MAX_DURATION)}; 0 runs until stopped."
        )
    return "\n\n".join(parts)


async def async_save_run_times(
    hass: HomeAssistant,
    user_input: dict[str, Any],
    labels: dict[str, str],
    use_controller: bool = True,
) -> dict[str, int]:
    """Write controller-owned run times to their entities; return the rest."""
    stored: dict[str, int] = {}
    for label, entity_id in labels.items():
        if user_input.get(label) is None:
            continue
        seconds = int(user_input[label])
        links = find_zone_links(hass, entity_id) if use_controller else ZoneLinks()
        if links.duration:
            factor = seconds_per_unit(hass.states.get(links.duration)) or 1
            await hass.services.async_call(
                links.duration.split(".", 1)[0],
                "set_value",
                {"entity_id": links.duration, "value": round(seconds / factor, 3)},
                blocking=True,
            )
        else:
            stored[entity_id] = seconds
    return stored


@dataclass(slots=True)
class Zone:
    """HomeKit characteristics and run state for one valve."""

    entity_id: str
    active: Characteristic
    in_use: Characteristic
    set_duration: Characteristic
    remaining: Characteristic
    configured: Characteristic
    fault: Characteristic
    duration: int
    links: ZoneLinks = ZoneLinks()
    ends_at: float | None = None
    close_handle: asyncio.TimerHandle | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        """Return true while water is flowing (as far as HomeKit is told)."""
        return self.in_use.value == HK_IN_USE

    @property
    def enabled(self) -> bool:
        """Return true unless the zone was turned off in Apple Home."""
        return self.configured.value == HK_CONFIGURED


class ZoneAccessory(Accessory):
    """One irrigation zone as an accessory of its own.

    It only holds the zone's Valve service; the irrigation system that
    created it handles everything the valve does.
    """

    category = CATEGORY_SPRINKLER
    homekit_extended = True

    def __init__(
        self, group: ValveGroupAccessory, entity_id: str, aid: int, name: str
    ) -> None:
        """Initialize the zone accessory and its information service."""
        storage = getattr(group.driver, "iid_storage", None)
        super().__init__(
            driver=group.driver,
            display_name=name,
            aid=aid,
            iid_manager=HomeIIDManager(storage) if storage else None,
        )
        self.hass = group.hass
        self.entity_id = entity_id
        self.config = group.config
        set_accessory_info(self, group.zone_info(entity_id, group.data))

    def add_protocol_version_service(self) -> None:
        """Zone accessories are always bridged; they don't carry this service."""

    @property
    def available(self) -> bool:
        """Show "No Response" when the zone's valve is unavailable."""
        state = self.hass.states.get(self.entity_id)
        return state is not None and state.state != STATE_UNAVAILABLE


class ValveGroupAccessory(HomeAccessory):
    """A parent service with one linked Valve service per Home Assistant valve."""

    MODEL: ClassVar[str]
    SYSTEM_SERVICE: ClassVar[str]

    def __init__(
        self,
        hass: HomeAssistant,
        driver: AccessoryDriver,
        entry: ConfigEntry,
        aid: int = STANDALONE_AID,
        aid_for: Callable[[str], int] | None = None,
    ) -> None:
        """Initialize the accessory.

        aid_for gives each zone accessory its AID; it is needed for the
        "separate accessories" layout and ignored otherwise.
        """
        self.layout = self.zone_layout({**entry.data, **entry.options})
        if self.layout == LAYOUT_ACCESSORIES and aid_for is None:
            self.layout = LAYOUT_VALVES
        super().__init__(
            hass,
            driver,
            entry,
            self.MODEL,
            aid,
            published=self.layout != LAYOUT_ACCESSORIES,
        )
        self.zone_accessories: dict[str, ZoneAccessory] = {}
        self.valves = valve_entity_ids(self.data.get(CONF_VALVES))
        self.run_times = zone_run_times(self.data, self.valves)
        self.disabled_zones: set[str] = set(self.data.get(CONF_DISABLED_ZONES) or [])
        self.one_at_a_time = self.default_one_at_a_time()
        self.master: str | None = self.data.get(CONF_MASTER)
        self.links: dict[str, ZoneLinks] = (
            {entity_id: find_zone_links(hass, entity_id) for entity_id in self.valves}
            if self.data.get(CONF_USE_CONTROLLER, True)
            else {}
        )
        self._zones: dict[str, Zone] = {}
        self._sequence: deque[str] = deque()
        self._sequence_current: str | None = None
        self._pump_on = False
        self._pump_off_handle: asyncio.TimerHandle | None = None
        self._building = True
        self._system_active: Characteristic | None = None
        self._system_fault: Characteristic | None = None
        self._system_in_use: Characteristic | None = None
        self._system_remaining: Characteristic | None = None

        if self.layout == LAYOUT_ACCESSORIES:
            assert aid_for is not None
            for entity_id in self.valves:
                zone = ZoneAccessory(
                    self,
                    entity_id,
                    aid_for(entity_id),
                    friendly_name(hass, entity_id),
                )
                self.zone_accessories[entity_id] = zone
                self._add_zone(entity_id, owner=zone).is_primary_service = True
        elif self.layout == LAYOUT_VALVES:
            # Numbered valves with no parent service.
            self.add_service_label()
            for index, entity_id in enumerate(self.valves, start=1):
                zone = self._add_zone(entity_id, label_index=index)
                zone.is_primary_service = index == 1
        else:
            system = self.add_named_service(
                self.SYSTEM_SERVICE,
                self.name,
                [CHAR_STATUS_FAULT, *self.system_chars()],
            )
            system.is_primary_service = True
            self._system_active = system.configure_char(
                CHAR_ACTIVE, value=HK_INACTIVE, setter_callback=self._set_system_active
            )
            self._system_fault = system.configure_char(
                CHAR_STATUS_FAULT, value=HK_NO_FAULT
            )
            self.configure_system(system)
            for entity_id in self.valves:
                system.add_linked_service(self._add_zone(entity_id))

        if self.master and (state := hass.states.get(self.master)) is not None:
            self._pump_on = state.state in ("on", *VALVE_OPEN_STATES)
        handlers = {entity_id: self._update_zone for entity_id in self.valves}
        for entity_id, links in self.links.items():
            if links.duration:
                handlers[links.duration] = self._linked_duration_handler(entity_id)
            if links.end_time:
                handlers[links.end_time] = self._linked_end_time_handler(entity_id)
        self.track(handlers)
        self._building = False
        self._update_system()

    # Type-specific hooks

    def system_chars(self) -> list[str]:
        """Optional characteristics of the parent service."""
        return []

    def configure_system(self, system: Any) -> None:
        """Configure type-specific parent characteristics."""

    def valve_type(self) -> int:
        """HomeKit ValveType for every zone."""
        raise NotImplementedError

    def default_one_at_a_time(self) -> bool:
        """Whether starting a zone stops the others."""
        return False

    def start_all(self) -> None:
        """Handle the parent service being turned on."""

    def zone_layout(self, data: dict[str, Any]) -> str:
        """How zones appear in HomeKit; only irrigation systems can change it."""
        return LAYOUT_SYSTEM

    def published_accessories(self) -> list[Accessory]:
        """The zone accessories in the "separate accessories" layout."""
        if self.layout == LAYOUT_ACCESSORIES:
            return list(self.zone_accessories.values())
        return [self]

    def zone_info(self, entity_id: str, config: dict[str, Any]) -> dict[str, str]:
        """A zone accessory's information: its own device's, then the system's."""
        return accessory_info(
            config,
            self._default_model,
            entity_id,
            source_device_info(self.hass, {CONF_VALVES: [entity_id]}),
        )

    def _add_zone(
        self,
        entity_id: str,
        label_index: int | None = None,
        owner: Accessory | None = None,
    ) -> Any:
        links = self.links.get(entity_id, ZoneLinks())
        duration = self.run_times[entity_id]
        duration_props = DURATION_PROPERTIES
        remaining_props = DURATION_PROPERTIES
        if links.duration:
            state = self.hass.states.get(links.duration)
            if (linked := duration_seconds(state)) is not None:
                duration = linked
            if (limits := duration_limits(state)) is not None:
                low, high, step = limits
                duration_props = {"minValue": low, "maxValue": high, "minStep": step}
        if links:
            remaining_props = {
                **DURATION_PROPERTIES,
                "maxValue": max(duration_props["maxValue"], LINKED_REMAINING_MAX),
            }
        service = add_named_service(
            owner or self,
            "Valve",
            friendly_name(self.hass, entity_id),
            [
                CHAR_SET_DURATION,
                CHAR_REMAINING_DURATION,
                CHAR_IS_CONFIGURED,
                CHAR_STATUS_FAULT,
            ],
            label_index=label_index,
            unique_id=entity_id,
        )
        service.configure_char(CHAR_VALVE_TYPE, value=self.valve_type())
        self._zones[entity_id] = Zone(
            entity_id=entity_id,
            active=service.configure_char(
                CHAR_ACTIVE,
                value=HK_INACTIVE,
                setter_callback=lambda value: self._set_zone_active(entity_id, value),
            ),
            in_use=service.configure_char(CHAR_IN_USE, value=HK_NOT_IN_USE),
            set_duration=service.configure_char(
                CHAR_SET_DURATION,
                value=duration,
                properties=duration_props,
                setter_callback=lambda value: self._set_zone_duration(entity_id, value),
            ),
            remaining=service.configure_char(
                CHAR_REMAINING_DURATION,
                value=0,
                properties=remaining_props,
                getter_callback=lambda: self._zone_remaining_seconds(entity_id),
            ),
            configured=service.configure_char(
                CHAR_IS_CONFIGURED,
                value=(
                    HK_NOT_CONFIGURED
                    if entity_id in self.disabled_zones
                    else HK_CONFIGURED
                ),
                setter_callback=lambda value: self._set_zone_configured(
                    entity_id, value
                ),
            ),
            fault=service.configure_char(CHAR_STATUS_FAULT, value=HK_NO_FAULT),
            duration=duration,
            links=links,
        )
        return service

    def _linked_duration_handler(self, entity_id: str):
        @callback
        def update(state: State) -> None:
            if (seconds := duration_seconds(state)) is None:
                return
            zone = self._zones[entity_id]
            zone.duration = seconds
            zone.set_duration.set_value(seconds)

        return update

    def _linked_end_time_handler(self, entity_id: str):
        @callback
        def update(state: State) -> None:
            zone = self._zones[entity_id]
            zone.remaining.set_value(self._zone_remaining_seconds(entity_id))
            self._update_system()

        return update

    async def async_stop(self) -> None:
        """Release listeners and pending timers."""
        await super().async_stop()
        self._sequence.clear()
        self._sequence_current = None
        for zone in self._zones.values():
            self._cancel_run(zone)
        if self._pump_off_handle is not None:
            self._pump_off_handle.cancel()
            self._pump_off_handle = None

    # HomeKit -> Home Assistant

    def _set_system_active(self, value: int) -> None:
        if value == HK_INACTIVE:
            self._sequence.clear()
            self._sequence_current = None
            for entity_id, zone in self._zones.items():
                if zone.running:
                    self._close_zone(entity_id)
        else:
            self.start_all()
        self._update_system()

    def _set_zone_active(self, entity_id: str, value: int) -> None:
        if value == HK_ACTIVE:
            # Starting a zone by hand ends a "run all".
            self._sequence.clear()
            self._sequence_current = None
            self._open_zone(entity_id)
        else:
            self._close_zone(entity_id)
            if entity_id == self._sequence_current:
                self._advance_sequence()
        self._update_system()

    def _set_zone_duration(self, entity_id: str, value: int) -> None:
        zone = self._zones[entity_id]
        if zone.links.duration:
            # The controller's number is the source of truth; its state change
            # comes back through the linked duration handler.
            state = self.hass.states.get(zone.links.duration)
            factor = seconds_per_unit(state) or 1
            zone.duration = int(value)
            self.call_service(
                zone.links.duration.split(".", 1)[0],
                "set_value",
                zone.links.duration,
                value=round(int(value) / factor, 3),
            )
            return
        zone.duration = max(0, min(int(value), MAX_DURATION))
        if zone.ends_at is not None:
            self._schedule_close(zone, zone.duration)
            self._update_system()
        if self.run_times.get(entity_id) != zone.duration:
            self.run_times[entity_id] = zone.duration
            self._persist()

    def _set_zone_configured(self, entity_id: str, value: int) -> None:
        if value == HK_CONFIGURED:
            self.disabled_zones.discard(entity_id)
        else:
            self.disabled_zones.add(entity_id)
            self._sequence = deque(z for z in self._sequence if z != entity_id)
        self._persist()

    def _persist(self) -> None:
        """Save changes made in Apple Home so they survive restarts."""
        options = {
            **self.entry.options,
            CONF_RUN_TIMES: dict(self.run_times),
            CONF_DISABLED_ZONES: sorted(self.disabled_zones),
        }

        def _save() -> None:
            self.hass.config_entries.async_update_entry(self.entry, options=options)

        self.hass.loop.call_soon_threadsafe(_save)

    def apply_in_place(self, config: dict[str, Any]) -> None:
        """Apply info, run times and enabled zones without re-publishing."""
        super().apply_in_place(config)
        for entity_id, zone_accessory in self.zone_accessories.items():
            set_accessory_info(zone_accessory, self.zone_info(entity_id, config))
        run_times = zone_run_times(config, self.valves)
        self.disabled_zones = set(config.get(CONF_DISABLED_ZONES) or [])
        for entity_id, zone in self._zones.items():
            if not zone.links.duration and run_times[entity_id] != zone.duration:
                zone.duration = run_times[entity_id]
                zone.set_duration.set_value(zone.duration)
            zone.configured.set_value(
                HK_NOT_CONFIGURED if entity_id in self.disabled_zones else HK_CONFIGURED
            )
        self.run_times = run_times

    # Zones and sequences

    def _open_zone(self, entity_id: str) -> None:
        if self.one_at_a_time:
            for other_id, other in self._zones.items():
                if other_id != entity_id and other.running:
                    self._close_zone(other_id)
        zone = self._zones[entity_id]
        zone.active.set_value(HK_ACTIVE)
        zone.in_use.set_value(HK_IN_USE)
        self.call_service(VALVE_DOMAIN, SERVICE_OPEN_VALVE, entity_id)
        if zone.links.duration:
            # The controller runs the zone for its own run time and closes it.
            self._cancel_run(zone, publish=False)
            if not zone.links.end_time:
                zone.ends_at = self.hass.loop.time() + zone.duration
            zone.remaining.set_value(self._zone_remaining_seconds(entity_id))
        else:
            self._schedule_close(zone, zone.duration)

    def _close_zone(self, entity_id: str) -> None:
        zone = self._zones[entity_id]
        zone.active.set_value(HK_INACTIVE)
        zone.in_use.set_value(HK_NOT_IN_USE)
        self.call_service(VALVE_DOMAIN, SERVICE_CLOSE_VALVE, entity_id)
        self._cancel_run(zone)

    def _start_sequence(self) -> None:
        """Run every enabled zone with a run time, one after another."""
        queue = [
            entity_id
            for entity_id, zone in self._zones.items()
            if zone.enabled and zone.duration > 0
        ]
        for entity_id, zone in self._zones.items():
            if zone.running and entity_id not in queue[:1]:
                self._close_zone(entity_id)
        self._sequence = deque(queue)
        self._advance_sequence()

    def _advance_sequence(self) -> None:
        self._sequence_current = None
        while self._sequence:
            entity_id = self._sequence.popleft()
            if self._zones[entity_id].enabled:
                self._sequence_current = entity_id
                self._open_zone(entity_id)
                return

    # Run timers

    def _schedule_close(self, zone: Zone, duration: int) -> None:
        """Close the valve after duration seconds (0 runs until stopped)."""
        self._cancel_run(zone, publish=False)
        if duration <= 0:
            zone.remaining.set_value(0)
            return
        zone.ends_at = self.hass.loop.time() + duration
        zone.close_handle = self.hass.loop.call_later(
            duration, self._async_run_finished, zone.entity_id
        )
        zone.remaining.set_value(duration)

    def _cancel_run(self, zone: Zone, *, publish: bool = True) -> None:
        if zone.close_handle is not None:
            zone.close_handle.cancel()
        zone.close_handle = None
        zone.ends_at = None
        if publish:
            zone.remaining.set_value(0)

    @callback
    def _async_run_finished(self, entity_id: str) -> None:
        if (zone := self._zones.get(entity_id)) is None:
            return
        zone.close_handle = None
        self._set_zone_active(entity_id, HK_INACTIVE)

    def _zone_remaining_seconds(self, entity_id: str) -> int:
        zone = self._zones[entity_id]
        limit = zone.remaining.properties.get("maxValue", MAX_DURATION)
        if zone.links.end_time:
            if not zone.running:
                return 0
            return min(limit, seconds_until(self.hass.states.get(zone.links.end_time)))
        if zone.ends_at is None:
            return 0
        return max(0, min(limit, round(zone.ends_at - self.hass.loop.time())))

    def _system_remaining_seconds(self) -> int:
        if self._sequence_current is not None:
            total = self._zone_remaining_seconds(self._sequence_current) + sum(
                self._zones[entity_id].duration for entity_id in self._sequence
            )
            return min(total, self._system_remaining_max())
        return max(
            (self._zone_remaining_seconds(entity_id) for entity_id in self._zones),
            default=0,
        )

    def _system_remaining_max(self) -> int:
        return LINKED_REMAINING_MAX if any(self.links.values()) else MAX_DURATION

    # Home Assistant -> HomeKit

    @callback
    def _update_zone(self, state: State) -> None:
        if (zone := self._zones.get(state.entity_id)) is None:
            return
        zone.fault.set_value(HK_FAULT if state.state in IGNORED_STATES else HK_NO_FAULT)
        is_open = state.state in VALVE_OPEN_STATES
        was_running = zone.running
        zone.active.set_value(HK_ACTIVE if is_open else HK_INACTIVE)
        zone.in_use.set_value(HK_IN_USE if is_open else HK_NOT_IN_USE)
        if not is_open and zone.ends_at is not None:
            self._cancel_run(zone)
        if was_running and not is_open and state.entity_id == self._sequence_current:
            # Closed outside HomeKit mid-run: carry on with the next zone.
            self._advance_sequence()
        self._update_system()

    def _update_system(self) -> None:
        if self._building:
            return
        running = any(zone.running for zone in self._zones.values())
        if self._system_active is not None:
            self._system_active.set_value(
                HK_ACTIVE if running or self._sequence_current else HK_INACTIVE
            )
        if self._system_fault is not None:
            self._system_fault.set_value(
                HK_FAULT
                if any(zone.fault.value == HK_FAULT for zone in self._zones.values())
                else HK_NO_FAULT
            )
        if self._system_in_use is not None:
            self._system_in_use.set_value(HK_IN_USE if running else HK_NOT_IN_USE)
        if self._system_remaining is not None:
            self._system_remaining.set_value(self._system_remaining_seconds())
        self._update_pump(running)

    # Pump / master valve

    def _update_pump(self, running: bool) -> None:
        if not self.master:
            return
        if running:
            if self._pump_off_handle is not None:
                self._pump_off_handle.cancel()
                self._pump_off_handle = None
            if not self._pump_on:
                self._switch_pump(True)
        elif self._pump_on and self._pump_off_handle is None:
            self._pump_off_handle = self.hass.loop.call_later(
                PUMP_OFF_DELAY, self._async_pump_off
            )

    @callback
    def _async_pump_off(self) -> None:
        self._pump_off_handle = None
        if not any(zone.running for zone in self._zones.values()):
            self._switch_pump(False)

    def _switch_pump(self, on: bool) -> None:
        assert self.master
        self._pump_on = on
        domain = self.master.split(".", 1)[0]
        if domain == VALVE_DOMAIN:
            service = SERVICE_OPEN_VALVE if on else SERVICE_CLOSE_VALVE
        else:
            service = SERVICE_TURN_ON if on else SERVICE_TURN_OFF
        self.call_service(domain, service, self.master)


class IrrigationAccessory(ValveGroupAccessory):
    """Irrigation System with one zone per valve."""

    category = CATEGORY_SPRINKLER
    MODEL = "HomeKit Extended Irrigation"
    SYSTEM_SERVICE = "IrrigationSystem"

    def system_chars(self) -> list[str]:
        """Irrigation systems report overall remaining time."""
        return [CHAR_REMAINING_DURATION]

    def configure_system(self, system: Any) -> None:
        """Configure InUse, ProgramMode and RemainingDuration."""
        self._system_in_use = system.configure_char(CHAR_IN_USE, value=HK_NOT_IN_USE)
        system.configure_char(CHAR_PROGRAM_MODE, value=HK_NO_PROGRAM_SCHEDULED)
        self._system_remaining = system.configure_char(
            CHAR_REMAINING_DURATION,
            value=0,
            properties={
                **DURATION_PROPERTIES,
                "maxValue": self._system_remaining_max(),
            },
            getter_callback=self._system_remaining_seconds,
        )

    def valve_type(self) -> int:
        """Zones are irrigation valves."""
        return HK_VALVE_TYPES["irrigation"]

    def default_one_at_a_time(self) -> bool:
        """Most systems lack the pressure to water two zones at once."""
        return bool(self.data.get(CONF_ONE_AT_A_TIME, True))

    def start_all(self) -> None:
        """Turning the system on runs every enabled zone in turn."""
        self._start_sequence()

    def zone_layout(self, data: dict[str, Any]) -> str:
        """Irrigation zones can be valves or accessories of their own."""
        return zone_layout(data)


class FaucetAccessory(ValveGroupAccessory):
    """Faucet or shower with one outlet per valve; turning it on opens them all."""

    category = CATEGORY_FAUCET
    MODEL = "HomeKit Extended Faucet"
    SYSTEM_SERVICE = "Faucet"

    def valve_type(self) -> int:
        """Shower heads or faucets, as configured."""
        return HK_VALVE_TYPES[self.data.get(CONF_VALVE_TYPE, "shower_head")]

    def start_all(self) -> None:
        """Turning the faucet on opens every enabled outlet."""
        for entity_id, zone in self._zones.items():
            if zone.enabled and not zone.running:
                self._open_zone(entity_id)


IRRIGATION = AccessoryType(
    key="irrigation",
    model=IrrigationAccessory.MODEL,
    default_name="Irrigation",
    device_domains=("valve",),
    schema=_irrigation_schema,
    detect=_detect,
    normalize=_normalize,
    factory=IrrigationAccessory,
    entity_keys=(CONF_VALVES, CONF_MASTER),
    zones_key=CONF_VALVES,
    zone_accessories=True,
)

FAUCET = AccessoryType(
    key="faucet",
    model=FaucetAccessory.MODEL,
    default_name="Shower",
    device_domains=("valve",),
    schema=_faucet_schema,
    detect=_detect,
    normalize=_normalize,
    factory=FaucetAccessory,
    entity_keys=(CONF_VALVES,),
    zones_key=CONF_VALVES,
)
