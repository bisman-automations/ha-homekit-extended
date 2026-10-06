"""Constants for HomeKit Extended."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "homekit_extended"
VERSION: Final = "1.0.0"

CONF_ACCESSORY_TYPE: Final = "accessory_type"
CONF_PIN: Final = "pin"
CONF_PORT: Final = "port"

ACCESSORY_AIR_PURIFIER: Final = "air_purifier"
ACCESSORY_IRRIGATION: Final = "irrigation"
ACCESSORY_TYPES: Final = (ACCESSORY_AIR_PURIFIER, ACCESSORY_IRRIGATION)

# Air purifier
CONF_AIR_QUALITY_SENSOR: Final = "air_quality_sensor"
CONF_FAN: Final = "fan"
CONF_FILTER_LIFE_SENSOR: Final = "filter_life_sensor"
CONF_HUMIDITY_SENSOR: Final = "humidity_sensor"
CONF_PM25_SENSOR: Final = "pm25_sensor"
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"

AIR_PURIFIER_SENSOR_KEYS: Final = (
    CONF_AIR_QUALITY_SENSOR,
    CONF_PM25_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_FILTER_LIFE_SENSOR,
)

# Irrigation
CONF_DEFAULT_DURATION: Final = "default_duration"
CONF_VALVES: Final = "valves"

DEFAULT_DURATION: Final = 900
DEFAULT_PIN: Final = "123-45-678"

# Core HomeKit Bridge starts at 21063; stay well clear of it.
DEFAULT_PORTS: Final = {
    ACCESSORY_IRRIGATION: 51828,
    ACCESSORY_AIR_PURIFIER: 51829,
}

FILTER_CHANGE_THRESHOLD: Final = 10

MANUFACTURER: Final = "Home Assistant"
MODELS: Final = {
    ACCESSORY_AIR_PURIFIER: "HomeKit Extended Air Purifier",
    ACCESSORY_IRRIGATION: "HomeKit Extended Irrigation",
}

VALVE_OPEN_STATES: Final = {"open", "opening"}
