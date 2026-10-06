"""HomeKit Irrigation System accessory grouping several valves as zones."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_SPRINKLER

from homeassistant.components.valve import DOMAIN as VALVE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_CLOSE_VALVE, SERVICE_OPEN_VALVE
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    ACCESSORY_IRRIGATION,
    CONF_DEFAULT_DURATION,
    CONF_VALVES,
    DEFAULT_DURATION,
    MANUFACTURER,
    MODELS,
    VALVE_OPEN_STATES,
    VERSION,
)
from .helpers import friendly_name

_LOGGER = logging.getLogger(__name__)

CHAR_ACTIVE = "Active"
CHAR_CONFIGURED_NAME = "ConfiguredName"
CHAR_IN_USE = "InUse"
CHAR_NAME = "Name"
CHAR_PROGRAM_MODE = "ProgramMode"
CHAR_REMAINING_DURATION = "RemainingDuration"
CHAR_SET_DURATION = "SetDuration"
CHAR_VALVE_TYPE = "ValveType"

SERV_IRRIGATION_SYSTEM = "IrrigationSystem"
SERV_VALVE = "Valve"

MAX_DURATION = 3600
DURATION_PROPERTIES = {"minValue": 0, "maxValue": MAX_DURATION, "minStep": 1}

HK_ACTIVE = 1
HK_INACTIVE = 0
HK_IN_USE = 1
HK_NOT_IN_USE = 0
HK_NO_PROGRAM_SCHEDULED = 0
HK_VALVE_TYPE_IRRIGATION = 1


def valve_entity_ids(raw: Any) -> list[str]:
    """Normalize stored valves to entity ids.

    Accepts plain entity ids and the {"entity_id": ..., "name": ...} mappings
    stored by the standalone homekit-irrigation integration.
    """
    entity_ids: list[str] = []
    for item in raw or []:
        entity_id = item["entity_id"] if isinstance(item, dict) else str(item)
        if entity_id not in entity_ids:
            entity_ids.append(entity_id)
    return entity_ids


@dataclass(slots=True)
class IrrigationConfig:
    """Valves an irrigation accessory groups."""

    name: str
    valves: list[str]
    default_duration: int

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> IrrigationConfig:
        """Build config from entry data merged with options."""
        data = {**entry.data, **entry.options}
        return cls(
            name=entry.title,
            valves=valve_entity_ids(data.get(CONF_VALVES)),
            default_duration=int(data.get(CONF_DEFAULT_DURATION, DEFAULT_DURATION)),
        )


@dataclass(slots=True)
class Zone:
    """HomeKit characteristics and run state for one valve."""

    entity_id: str
    active: Characteristic
    in_use: Characteristic
    set_duration: Characteristic
    remaining: Characteristic
    duration: int
    ends_at: float | None = None
    close_handle: asyncio.TimerHandle | None = field(default=None, repr=False)


def create_irrigation(
    hass: HomeAssistant, driver: AccessoryDriver, entry: ConfigEntry
) -> IrrigationAccessory:
    """Accessory factory for the shared server."""
    return IrrigationAccessory(
        hass, driver, IrrigationConfig.from_entry(entry), entry.entry_id
    )


class IrrigationAccessory(Accessory):
    """One HomeKit Irrigation System with a linked Valve service per zone."""

    category = CATEGORY_SPRINKLER

    def __init__(
        self,
        hass: HomeAssistant,
        driver: AccessoryDriver,
        config: IrrigationConfig,
        serial: str,
    ) -> None:
        """Initialize the accessory."""
        super().__init__(driver=driver, display_name=config.name)
        self.hass = hass
        self.config = config
        self._subscriptions: list[CALLBACK_TYPE] = []
        self._zones: dict[str, Zone] = {}

        self.set_info_service(
            manufacturer=MANUFACTURER,
            model=MODELS[ACCESSORY_IRRIGATION],
            serial_number=serial,
            firmware_revision=VERSION,
        )

        system = self.add_preload_service(
            SERV_IRRIGATION_SYSTEM, [CHAR_NAME, CHAR_REMAINING_DURATION]
        )
        system.is_primary_service = True
        system.configure_char(CHAR_NAME, value=config.name)
        self._system_active = system.configure_char(
            CHAR_ACTIVE, value=HK_INACTIVE, setter_callback=self._set_system_active
        )
        self._system_in_use = system.configure_char(CHAR_IN_USE, value=HK_NOT_IN_USE)
        system.configure_char(CHAR_PROGRAM_MODE, value=HK_NO_PROGRAM_SCHEDULED)
        self._system_remaining = system.configure_char(
            CHAR_REMAINING_DURATION,
            value=0,
            properties=DURATION_PROPERTIES,
            getter_callback=self._system_remaining_seconds,
        )

        for entity_id in config.valves:
            system.add_linked_service(self._add_zone(entity_id))

        for entity_id in config.valves:
            if (state := hass.states.get(entity_id)) is not None:
                self._update_zone(state)
        self._update_system()

        self._subscriptions.append(
            async_track_state_change_event(
                hass, config.valves, self._async_state_changed
            )
        )

    def _add_zone(self, entity_id: str) -> Any:
        """Add one Valve service mirroring a Home Assistant valve."""
        name = friendly_name(self.hass, entity_id)
        service = self.add_preload_service(
            SERV_VALVE,
            [
                CHAR_NAME,
                CHAR_CONFIGURED_NAME,
                CHAR_SET_DURATION,
                CHAR_REMAINING_DURATION,
            ],
            unique_id=entity_id,
        )
        service.configure_char(CHAR_NAME, value=name)
        service.configure_char(CHAR_CONFIGURED_NAME, value=name)
        service.configure_char(CHAR_VALVE_TYPE, value=HK_VALVE_TYPE_IRRIGATION)
        duration = min(self.config.default_duration, MAX_DURATION)
        zone = Zone(
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
                properties=DURATION_PROPERTIES,
                setter_callback=lambda value: self._set_zone_duration(entity_id, value),
            ),
            remaining=service.configure_char(
                CHAR_REMAINING_DURATION,
                value=0,
                properties=DURATION_PROPERTIES,
                getter_callback=lambda: self._zone_remaining_seconds(entity_id),
            ),
            duration=duration,
        )
        self._zones[entity_id] = zone
        return service

    async def async_stop(self) -> None:
        """Release listeners and pending auto-close timers."""
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()
        for zone in self._zones.values():
            self._cancel_run(zone)

    # HomeKit -> Home Assistant

    def _set_system_active(self, value: int) -> None:
        """Turning the system off stops every zone."""
        if value == HK_INACTIVE:
            for entity_id in self._zones:
                self._set_zone_active(entity_id, HK_INACTIVE)
        self._update_system()

    def _set_zone_active(self, entity_id: str, value: int) -> None:
        """Start or stop a zone from HomeKit."""
        zone = self._zones[entity_id]
        if value == HK_ACTIVE:
            _LOGGER.debug("HomeKit started %s for %ss", entity_id, zone.duration)
            zone.active.set_value(HK_ACTIVE)
            zone.in_use.set_value(HK_IN_USE)
            self._call_valve_service(entity_id, SERVICE_OPEN_VALVE)
            self._schedule_close(zone, zone.duration)
        else:
            _LOGGER.debug("HomeKit stopped %s", entity_id)
            zone.active.set_value(HK_INACTIVE)
            zone.in_use.set_value(HK_NOT_IN_USE)
            self._call_valve_service(entity_id, SERVICE_CLOSE_VALVE)
            self._cancel_run(zone)
        self._update_system()

    def _set_zone_duration(self, entity_id: str, value: int) -> None:
        """Store a new run length; restart the countdown if the zone is running."""
        zone = self._zones[entity_id]
        zone.duration = max(0, min(int(value), MAX_DURATION))
        if zone.ends_at is not None:
            self._schedule_close(zone, zone.duration)
            self._update_system()

    def _call_valve_service(self, entity_id: str, service: str) -> None:
        """Call a valve service without blocking the HAP request."""
        self.hass.async_create_task(
            self.hass.services.async_call(
                VALVE_DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=False
            )
        )

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
        """Forget a zone's countdown."""
        if zone.close_handle is not None:
            zone.close_handle.cancel()
        zone.close_handle = None
        zone.ends_at = None
        if publish:
            zone.remaining.set_value(0)

    @callback
    def _async_run_finished(self, entity_id: str) -> None:
        """Close a zone whose HomeKit run time has elapsed."""
        if (zone := self._zones.get(entity_id)) is None:
            return
        _LOGGER.debug("Run time elapsed for %s, closing", entity_id)
        zone.close_handle = None
        self._set_zone_active(entity_id, HK_INACTIVE)

    def _zone_remaining_seconds(self, entity_id: str) -> int:
        """Seconds left in a zone's run, computed when HomeKit reads it."""
        zone = self._zones[entity_id]
        if zone.ends_at is None:
            return 0
        return max(0, min(MAX_DURATION, round(zone.ends_at - self.hass.loop.time())))

    def _system_remaining_seconds(self) -> int:
        """Longest remaining run across zones."""
        return max(
            (self._zone_remaining_seconds(entity_id) for entity_id in self._zones),
            default=0,
        )

    # Home Assistant -> HomeKit

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle a valve changing in Home Assistant."""
        if (new_state := event.data["new_state"]) is None:
            return
        self._update_zone(new_state)
        self._update_system()

    @callback
    def _update_zone(self, state: State) -> None:
        """Mirror one valve's state onto its zone."""
        if (zone := self._zones.get(state.entity_id)) is None:
            return
        is_open = state.state in VALVE_OPEN_STATES
        zone.active.set_value(HK_ACTIVE if is_open else HK_INACTIVE)
        zone.in_use.set_value(HK_IN_USE if is_open else HK_NOT_IN_USE)
        if not is_open and zone.ends_at is not None:
            self._cancel_run(zone)

    def _update_system(self) -> None:
        """Mirror aggregate zone state onto the Irrigation System service."""
        running = any(zone.in_use.value == HK_IN_USE for zone in self._zones.values())
        self._system_active.set_value(HK_ACTIVE if running else HK_INACTIVE)
        self._system_in_use.set_value(HK_IN_USE if running else HK_NOT_IN_USE)
        self._system_remaining.set_value(self._system_remaining_seconds())
