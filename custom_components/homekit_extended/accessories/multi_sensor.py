"""Multi-sensor: several sensors of one device as one HomeKit accessory."""

from __future__ import annotations

from typing import Any

from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_SENSOR
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..const import (
    CONF_BATTERY_SENSOR,
    CONF_CONTACT_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_ILLUMINANCE_SENSOR,
    CONF_LEAK_SENSOR,
    CONF_MOTION_SENSOR,
    CONF_OCCUPANCY_SENSOR,
    CONF_TEMPERATURE_SENSOR,
)
from .base import (
    AccessoryType,
    HomeAccessory,
    drop_empty,
    entity_field,
    find_entity,
    optional_entities,
)
from .sensors import (
    ServiceBuilder,
    add_battery_service,
    add_contact_service,
    add_humidity_service,
    add_illuminance_service,
    add_leak_service,
    add_motion_service,
    add_occupancy_service,
    add_temperature_service,
)

# key: (picker domain, HomeKit service builder, service name suffix)
SENSORS: dict[str, tuple[str, ServiceBuilder, str]] = {
    CONF_MOTION_SENSOR: ("binary_sensor", add_motion_service, "Motion"),
    CONF_OCCUPANCY_SENSOR: ("binary_sensor", add_occupancy_service, "Occupancy"),
    CONF_CONTACT_SENSOR: ("binary_sensor", add_contact_service, "Contact"),
    CONF_LEAK_SENSOR: ("binary_sensor", add_leak_service, "Leak"),
    CONF_TEMPERATURE_SENSOR: ("sensor", add_temperature_service, "Temperature"),
    CONF_HUMIDITY_SENSOR: ("sensor", add_humidity_service, "Humidity"),
    CONF_ILLUMINANCE_SENSOR: ("sensor", add_illuminance_service, "Light Level"),
    CONF_BATTERY_SENSOR: ("sensor", add_battery_service, "Battery"),
}


def _schema() -> dict[vol.Marker, Any]:
    return {
        vol.Optional(key): entity_field(domain)
        for key, (domain, _, _) in SENSORS.items()
    }


def _detect(hass: HomeAssistant, entries: list[er.RegistryEntry]) -> dict[str, Any]:
    return drop_empty(
        {
            CONF_MOTION_SENSOR: find_entity(entries, "binary_sensor", "motion"),
            CONF_OCCUPANCY_SENSOR: find_entity(
                entries, "binary_sensor", {"occupancy", "presence"}
            ),
            CONF_CONTACT_SENSOR: find_entity(
                entries, "binary_sensor", {"door", "window", "opening", "garage_door"}
            ),
            CONF_LEAK_SENSOR: find_entity(entries, "binary_sensor", "moisture"),
            CONF_TEMPERATURE_SENSOR: find_entity(entries, "sensor", "temperature"),
            CONF_HUMIDITY_SENSOR: find_entity(entries, "sensor", "humidity"),
            CONF_ILLUMINANCE_SENSOR: find_entity(entries, "sensor", "illuminance"),
            CONF_BATTERY_SENSOR: find_entity(entries, "sensor", "battery"),
        }
    )


def _normalize(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    optional_entities(data, SENSORS)
    has_sensor = any(data[key] for key in SENSORS if key != CONF_BATTERY_SENSOR)
    return data, {} if has_sensor else {"base": "no_sensors_selected"}


class MultiSensorAccessory(HomeAccessory):
    """One service per configured sensor; the first one is primary."""

    category = CATEGORY_SENSOR

    def __init__(
        self, hass: HomeAssistant, driver: AccessoryDriver, entry: ConfigEntry
    ) -> None:
        """Initialize the accessory."""
        super().__init__(hass, driver, entry, MULTI_SENSOR.model)
        handlers = {}
        primary = None
        for key, (_, build, suffix) in SENSORS.items():
            if not (entity_id := self.data.get(key)):
                continue
            service, handler = build(self, f"{self.name} {suffix}")
            if primary is None and key != CONF_BATTERY_SENSOR:
                service.is_primary_service = True
                primary = service
            elif primary is not None:
                primary.add_linked_service(service)
            handlers[entity_id] = handler
        self.track(handlers)


MULTI_SENSOR = AccessoryType(
    key="multi_sensor",
    model="HomeKit Extended Multi-Sensor",
    default_name="Sensor",
    device_domains=("sensor", "binary_sensor"),
    schema=_schema,
    detect=_detect,
    normalize=_normalize,
    factory=MultiSensorAccessory,
    entity_keys=tuple(SENSORS),
)
