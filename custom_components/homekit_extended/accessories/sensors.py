"""Sensor services and air quality math shared by several accessory types."""

from __future__ import annotations

from collections.abc import Callable

from pyhap.service import Service

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_ON, UnitOfTemperature
from homeassistant.core import State
from homeassistant.util.unit_conversion import TemperatureConverter

from .base import IGNORED_STATES, HomeAccessory, numeric_state, set_clamped

LOW_BATTERY_THRESHOLD = 20
CO2_ABNORMAL_PPM = 1000

Handler = Callable[[State], None]
ServiceBuilder = Callable[[HomeAccessory, str], tuple[Service, Handler]]


def add_temperature_service(acc: HomeAccessory, name: str) -> tuple[Service, Handler]:
    """Temperature sensor, converted to °C."""
    service = acc.add_named_service("TemperatureSensor", name, ["CurrentTemperature"])
    char = service.configure_char(
        "CurrentTemperature", value=0, properties={"minValue": -273, "maxValue": 999}
    )

    def update(state: State) -> None:
        if (value := numeric_state(state)) is None:
            return
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature.CELSIUS)
        if unit in (UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN):
            value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        set_clamped(char, round(value, 1))

    return service, update


def _numeric(service_type: str, char_name: str, **props: float) -> ServiceBuilder:
    def build(acc: HomeAccessory, name: str) -> tuple[Service, Handler]:
        service = acc.add_named_service(service_type, name, [char_name])
        char = service.configure_char(
            char_name, value=props.get("minValue", 0), properties=props or None
        )

        def update(state: State) -> None:
            if (value := numeric_state(state)) is not None:
                set_clamped(char, value)

        return service, update

    return build


def _binary(service_type: str, char_name: str, on: int, off: int) -> ServiceBuilder:
    def build(acc: HomeAccessory, name: str) -> tuple[Service, Handler]:
        service = acc.add_named_service(service_type, name, [char_name])
        char = service.configure_char(char_name, value=off)

        def update(state: State) -> None:
            if state.state not in IGNORED_STATES:
                char.set_value(on if state.state == STATE_ON else off)

        return service, update

    return build


add_humidity_service = _numeric("HumiditySensor", "CurrentRelativeHumidity")
add_illuminance_service = _numeric(
    "LightSensor", "CurrentAmbientLightLevel", minValue=0.0001, maxValue=100000
)
add_motion_service = _binary("MotionSensor", "MotionDetected", True, False)
add_occupancy_service = _binary("OccupancySensor", "OccupancyDetected", 1, 0)
# binary_sensor "on" means open, which HomeKit calls "contact not detected" (1).
add_contact_service = _binary("ContactSensor", "ContactSensorState", 1, 0)
add_leak_service = _binary("LeakSensor", "LeakDetected", 1, 0)


def add_battery_service(acc: HomeAccessory, name: str) -> tuple[Service, Handler]:
    """Battery level with a low-battery flag."""
    service = acc.add_named_service(
        "BatteryService", name, ["BatteryLevel", "ChargingState", "StatusLowBattery"]
    )
    level = service.configure_char("BatteryLevel", value=100)
    service.configure_char("ChargingState", value=2)  # not chargeable
    low = service.configure_char("StatusLowBattery", value=0)

    def update(state: State) -> None:
        if (value := numeric_state(state)) is None:
            return
        set_clamped(level, round(value))
        low.set_value(1 if value <= LOW_BATTERY_THRESHOLD else 0)

    return service, update


def add_co2_service(acc: HomeAccessory, name: str) -> tuple[Service, Handler]:
    """CO₂ sensor that flags levels at or above 1000 ppm as abnormal."""
    service = acc.add_named_service(
        "CarbonDioxideSensor", name, ["CarbonDioxideDetected", "CarbonDioxideLevel"]
    )
    detected = service.configure_char("CarbonDioxideDetected", value=0)
    level = service.configure_char("CarbonDioxideLevel", value=0)

    def update(state: State) -> None:
        if (value := numeric_state(state)) is None:
            return
        set_clamped(level, value)
        detected.set_value(1 if value >= CO2_ABNORMAL_PPM else 0)

    return service, update


# Air quality: HomeKit uses 0 unknown, 1 excellent ... 5 poor.

THRESHOLDS: dict[str, tuple[float, float, float, float]] = {
    # µg/m³ unless noted; upper bounds for excellent, good, fair, inferior.
    "pm25": (12, 35, 55, 150),
    "pm10": (54, 154, 254, 354),
    "voc": (250, 500, 1000, 3000),
    "no2": (50, 100, 200, 400),
    "ozone": (60, 120, 180, 240),
    "so2": (50, 100, 350, 500),
    "co2": (800, 1000, 1500, 2000),  # ppm
    "aqi": (50, 100, 150, 200),  # US AQI
}


def level_for(pollutant: str, value: float) -> int:
    """Map a pollutant reading to HomeKit's 1-5 air quality."""
    for quality, limit in enumerate(THRESHOLDS[pollutant], start=1):
        if value <= limit:
            return quality
    return 5


def homekit_air_quality(value: float) -> int:
    """Map a 1-5 HomeKit value or an AQI-style value to HomeKit air quality."""
    if 1 <= value <= 5 and value == int(value):
        return int(value)
    return level_for("aqi", value)


def pm25_air_quality(value: float) -> int:
    """Map PM2.5 density (µg/m³) to HomeKit air quality."""
    return level_for("pm25", value)
