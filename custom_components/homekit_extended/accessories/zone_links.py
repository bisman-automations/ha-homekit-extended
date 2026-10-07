"""Find a controller's own run-time and time-remaining entities for a valve.

Some integrations put a run-time `number` and an end-time timestamp `sensor`
on the same device as each zone's valve. Home Assistant's HomeKit Bridge calls
these "linked valve duration" and "linked valve end time"; Rain Bird Extended
creates them for every Rain Bird zone. When they exist, the controller owns the
run: HomeKit's run time reads and writes the number, and the countdown comes
from the sensor instead of a timer kept here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.number import NumberDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfTime
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .base import IGNORED_STATES, device_class_of

DURATION_DOMAINS = ("number", "input_number")
SECONDS_PER_UNIT = {
    UnitOfTime.SECONDS: 1,
    UnitOfTime.MINUTES: 60,
    UnitOfTime.HOURS: 3600,
    UnitOfTime.DAYS: 86400,
}


@dataclass(frozen=True, slots=True)
class ZoneLinks:
    """A valve's controller-side run time and end time, if any."""

    duration: str | None = None
    end_time: str | None = None

    def __bool__(self) -> bool:
        """Return true if anything is linked."""
        return bool(self.duration or self.end_time)


def find_zone_links(hass: HomeAssistant, valve_entity_id: str) -> ZoneLinks:
    """Look on the valve's device for a run-time number and an end-time sensor."""
    registry = er.async_get(hass)
    valve = registry.async_get(valve_entity_id)
    if valve is None or valve.device_id is None:
        return ZoneLinks()
    duration = end_time = None
    for entry in er.async_entries_for_device(registry, valve.device_id):
        if entry.disabled_by is not None or entry.entity_id == valve_entity_id:
            continue
        device_class = device_class_of(entry)
        if (
            duration is None
            and entry.domain in DURATION_DOMAINS
            and (
                device_class == NumberDeviceClass.DURATION
                or seconds_per_unit(hass.states.get(entry.entity_id)) is not None
            )
        ):
            duration = entry.entity_id
        elif (
            end_time is None
            and entry.domain == "sensor"
            and device_class == SensorDeviceClass.TIMESTAMP
        ):
            end_time = entry.entity_id
    return ZoneLinks(duration, end_time)


def seconds_per_unit(state: State | None) -> int | None:
    """Seconds per unit of a duration entity, or None if it isn't a time."""
    if state is None:
        return None
    return SECONDS_PER_UNIT.get(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))


def duration_seconds(state: State | None) -> int | None:
    """A duration entity's value in seconds."""
    if state is None or state.state in IGNORED_STATES:
        return None
    try:
        return round(float(state.state) * (seconds_per_unit(state) or 1))
    except ValueError:
        return None


def duration_limits(state: State | None) -> tuple[int, int, int] | None:
    """(min, max, step) in seconds from a number entity's attributes."""
    if state is None:
        return None
    factor = seconds_per_unit(state) or 1
    try:
        return (
            round(float(state.attributes.get("min", 0)) * factor),
            round(float(state.attributes.get("max", 3600)) * factor),
            max(1, round(float(state.attributes.get("step", 1)) * factor)),
        )
    except (TypeError, ValueError):
        return None


def seconds_until(state: State | None) -> int:
    """Seconds until an end-time sensor's timestamp (0 if idle or past)."""
    if state is None or state.state in IGNORED_STATES:
        return 0
    end: datetime | None = dt_util.parse_datetime(state.state)
    if end is None:
        return 0
    return max(0, round((end - dt_util.utcnow()).total_seconds()))


# Controller-level entities, matched by the key their integration gives them
# (Rain Bird Extended's names; other integrations can use the same keys).
RUN_ALL_KEYS = frozenset({"run_all_zones"})
STOP_KEYS = frozenset({"stop_irrigation"})
RESUME_KEYS = frozenset({"resume"})
RUNNING_KEYS = frozenset({"irrigating"})


@dataclass(frozen=True, slots=True)
class ControllerLinks:
    """A controller's own "run all zones", "stop" and "irrigating" entities.

    When they exist, the Irrigation System's switch runs and stops the
    controller's own program (with its zone order, cycle and soak) instead
    of a sequence kept here, and shows whatever the controller is running.
    """

    run_all: str | None = None
    stop: str | None = None
    running: str | None = None
    resume: str | None = None

    def __bool__(self) -> bool:
        """Return true if anything is linked."""
        return bool(self.run_all or self.stop or self.running)


def find_controller_links(
    hass: HomeAssistant, valve_entity_ids: list[str]
) -> ControllerLinks:
    """Look for controller entities on the device all the valves belong to.

    That's the valves' own device, or the device they're connected through
    (each Rain Bird zone is a device under its controller).
    """
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    controllers: set[str] = set()
    for entity_id in valve_entity_ids:
        entry = registry.async_get(entity_id)
        if entry is None or entry.device_id is None:
            return ControllerLinks()
        device = devices.async_get(entry.device_id)
        if device is None:
            return ControllerLinks()
        controllers.add(device.via_device_id or device.id)
    if len(controllers) != 1:
        return ControllerLinks()
    found: dict[str, str] = {}
    for entry in er.async_entries_for_device(registry, controllers.pop()):
        if entry.disabled_by is not None:
            continue
        key = entry.translation_key
        if entry.domain == "button" and key in RUN_ALL_KEYS:
            found.setdefault("run_all", entry.entity_id)
        elif entry.domain == "button" and key in STOP_KEYS:
            found.setdefault("stop", entry.entity_id)
        elif entry.domain == "button" and key in RESUME_KEYS:
            found.setdefault("resume", entry.entity_id)
        elif entry.domain == "binary_sensor" and key in RUNNING_KEYS:
            found.setdefault("running", entry.entity_id)
    return ControllerLinks(**found)


def seconds_until_attribute(state: State | None, attribute: str) -> int:
    """Seconds until a timestamp held in a state attribute (0 if none or past)."""
    if state is None or not (raw := state.attributes.get(attribute)):
        return 0
    end: datetime | None = dt_util.parse_datetime(str(raw))
    if end is None:
        return 0
    return max(0, round((end - dt_util.utcnow()).total_seconds()))
