"""Constants for HomeKit Extended."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "homekit_extended"
VERSION: Final = "2.1.0"
MANUFACTURER: Final = "HomeKit Extended"

CONF_ACCESSORY_TYPE: Final = "accessory_type"
CONF_BRIDGE: Final = "bridge"
CONF_CONNECTION: Final = "connection"
CONF_INFO: Final = "accessory_info"
CONF_DEVICE: Final = "device"
CONF_PIN: Final = "pin"
CONF_PLAIN_NAME: Final = "plain_name"
CONF_PORT: Final = "port"

# Entity fields
CONF_AIR_QUALITY_SENSOR: Final = "air_quality_sensor"
CONF_AQI_SENSOR: Final = "aqi_sensor"
CONF_BATTERY_SENSOR: Final = "battery_sensor"
CONF_CO2_SENSOR: Final = "co2_sensor"
CONF_CONTACT_SENSOR: Final = "contact_sensor"
CONF_DEFAULT_DURATION: Final = "default_duration"
CONF_DISABLED_ZONES: Final = "disabled_zones"
CONF_EVENTS: Final = "events"
CONF_FAN: Final = "fan"
CONF_FILTER_LIFE_SENSOR: Final = "filter_life_sensor"
CONF_FIRMWARE: Final = "firmware"
CONF_HUMIDITY_SENSOR: Final = "humidity_sensor"
CONF_ILLUMINANCE_SENSOR: Final = "illuminance_sensor"
CONF_LEAK_SENSOR: Final = "leak_sensor"
CONF_LIGHT: Final = "light"
CONF_MANUFACTURER: Final = "manufacturer"
CONF_MASTER: Final = "master"
CONF_MODEL: Final = "model"
CONF_MOTION_SENSOR: Final = "motion_sensor"
CONF_NO2_SENSOR: Final = "no2_sensor"
CONF_OCCUPANCY_SENSOR: Final = "occupancy_sensor"
CONF_ONE_AT_A_TIME: Final = "one_at_a_time"
CONF_OUTLETS: Final = "outlets"
CONF_OZONE_SENSOR: Final = "ozone_sensor"
CONF_PM10_SENSOR: Final = "pm10_sensor"
CONF_PM25_SENSOR: Final = "pm25_sensor"
CONF_RUN_TIMES: Final = "run_times"
CONF_SEPARATE_ZONES: Final = "separate_zones"
CONF_SERIAL: Final = "serial_number"
CONF_SO2_SENSOR: Final = "so2_sensor"
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"
CONF_USE_CONTROLLER: Final = "use_controller_timers"
CONF_VALVE_TYPE: Final = "valve_type"
CONF_VALVES: Final = "valves"
CONF_VOC_SENSOR: Final = "voc_sensor"

# Choice in the "publish as" picker for an accessory with its own pairing.
STANDALONE: Final = "standalone"
HOMEKIT_DOMAIN: Final = "homekit"

DEFAULT_DURATION: Final = 900
# Core HomeKit Bridge starts at 21063; stay well clear of it.
DEFAULT_PORT: Final = 51828

FILTER_CHANGE_THRESHOLD: Final = 10
VALVE_OPEN_STATES: Final = {"open", "opening"}

SIGNAL_PAIRING_CHANGED: Final = f"{DOMAIN}_pairing_changed_{{}}"
