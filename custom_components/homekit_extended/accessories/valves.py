"""Valve groups: an Irrigation System with zones, or a Faucet/Shower with outlets.

Everything here maps onto native HomeKit behavior:

- Each zone has its own run time (SetDuration) and closes itself when it ends.
- Turning the Irrigation System on runs every enabled zone in turn; zones a
  user turns off in Apple Home (IsConfigured) are skipped.
- "One zone at a time" closes the running zone when another one starts.
- A valve that is unavailable shows as a fault (StatusFault).
- An optional pump or master valve runs while any zone is open. It is driven
  from Home Assistant and never exposed to HomeKit.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_FAUCET, CATEGORY_SPRINKLER
import voluptuous as vol

from homeassistant.components.valve import DOMAIN as VALVE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SERVICE_CLOSE_VALVE,
    SERVICE_OPEN_VALVE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er, selector

from ..const import (
    CONF_DEFAULT_DURATION,
    CONF_DISABLED_ZONES,
    CONF_MASTER,
    CONF_ONE_AT_A_TIME,
    CONF_RUN_TIMES,
    CONF_VALVE_TYPE,
    CONF_VALVES,
    DEFAULT_DURATION,
    VALVE_OPEN_STATES,
)
from ..helpers import friendly_name
from .base import (
    IGNORED_STATES,
    AccessoryType,
    HomeAccessory,
    entity_field,
    entity_list,
    find_entities,
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
    }


def _detect(hass: HomeAssistant, entries: list[er.RegistryEntry]) -> dict[str, Any]:
    valves = find_entities(entries, "valve", {"water", None}) or find_entities(
        entries, "valve"
    )
    return {CONF_VALVES: valves} if valves else {}


def _normalize(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    data[CONF_VALVES] = valve_entity_ids(data.get(CONF_VALVES))
    data[CONF_DEFAULT_DURATION] = int(data.get(CONF_DEFAULT_DURATION) or 0)
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
    hass: HomeAssistant, valves: list[str], run_times: dict[str, int]
) -> tuple[dict[vol.Marker, Any], dict[str, str], dict[str, int]]:
    """One run-time field per valve, labeled with the valve's name.

    Returns (schema, field label -> entity id, suggested values).
    """
    labels: dict[str, str] = {}
    for entity_id in valves:
        label = friendly_name(hass, entity_id)
        if label in labels:
            label = f"{label} ({entity_id})"
        labels[label] = entity_id
    schema = {vol.Required(label): _duration_field() for label in labels}
    suggested = {label: run_times[entity_id] for label, entity_id in labels.items()}
    return schema, labels, suggested


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


class ValveGroupAccessory(HomeAccessory):
    """A parent service with one linked Valve service per Home Assistant valve."""

    MODEL: ClassVar[str]
    SYSTEM_SERVICE: ClassVar[str]

    def __init__(
        self, hass: HomeAssistant, driver: AccessoryDriver, entry: ConfigEntry
    ) -> None:
        """Initialize the accessory."""
        super().__init__(hass, driver, entry, self.MODEL)
        self.valves = valve_entity_ids(self.data.get(CONF_VALVES))
        self.run_times = zone_run_times(self.data, self.valves)
        self.disabled_zones: set[str] = set(self.data.get(CONF_DISABLED_ZONES) or [])
        self.one_at_a_time = self.default_one_at_a_time()
        self.master: str | None = self.data.get(CONF_MASTER)
        self._zones: dict[str, Zone] = {}
        self._sequence: deque[str] = deque()
        self._sequence_current: str | None = None
        self._pump_on = False
        self._pump_off_handle: asyncio.TimerHandle | None = None
        self._building = True

        system = self.add_named_service(
            self.SYSTEM_SERVICE, self.name, [CHAR_STATUS_FAULT, *self.system_chars()]
        )
        system.is_primary_service = True
        self._system_active = system.configure_char(
            CHAR_ACTIVE, value=HK_INACTIVE, setter_callback=self._set_system_active
        )
        self._system_fault = system.configure_char(CHAR_STATUS_FAULT, value=HK_NO_FAULT)
        self._system_in_use: Characteristic | None = None
        self._system_remaining: Characteristic | None = None
        self.configure_system(system)

        for entity_id in self.valves:
            system.add_linked_service(self._add_zone(entity_id))

        if self.master and (state := hass.states.get(self.master)) is not None:
            self._pump_on = state.state in ("on", *VALVE_OPEN_STATES)
        self.track({entity_id: self._update_zone for entity_id in self.valves})
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

    def _add_zone(self, entity_id: str) -> Any:
        service = self.add_named_service(
            "Valve",
            friendly_name(self.hass, entity_id),
            [
                CHAR_SET_DURATION,
                CHAR_REMAINING_DURATION,
                CHAR_IS_CONFIGURED,
                CHAR_STATUS_FAULT,
            ],
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
                value=self.run_times[entity_id],
                properties=DURATION_PROPERTIES,
                setter_callback=lambda value: self._set_zone_duration(entity_id, value),
            ),
            remaining=service.configure_char(
                CHAR_REMAINING_DURATION,
                value=0,
                properties=DURATION_PROPERTIES,
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
            duration=self.run_times[entity_id],
        )
        return service

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
        run_times = zone_run_times(config, self.valves)
        self.disabled_zones = set(config.get(CONF_DISABLED_ZONES) or [])
        for entity_id, zone in self._zones.items():
            if run_times[entity_id] != zone.duration:
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
        if zone.ends_at is None:
            return 0
        return max(0, min(MAX_DURATION, round(zone.ends_at - self.hass.loop.time())))

    def _system_remaining_seconds(self) -> int:
        if self._sequence_current is not None:
            total = self._zone_remaining_seconds(self._sequence_current) + sum(
                self._zones[entity_id].duration for entity_id in self._sequence
            )
            return min(total, MAX_DURATION)
        return max(
            (self._zone_remaining_seconds(entity_id) for entity_id in self._zones),
            default=0,
        )

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
        self._system_active.set_value(
            HK_ACTIVE if running or self._sequence_current else HK_INACTIVE
        )
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
            properties=DURATION_PROPERTIES,
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
