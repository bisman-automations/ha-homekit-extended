"""Air quality monitor: every pollutant reading on one Air Quality sensor."""

from __future__ import annotations

from typing import Any

from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_SENSOR
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er

from ..const import (
    CONF_AQI_SENSOR,
    CONF_CO2_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_NO2_SENSOR,
    CONF_OZONE_SENSOR,
    CONF_PM10_SENSOR,
    CONF_PM25_SENSOR,
    CONF_SO2_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_VOC_SENSOR,
)
from .base import (
    STANDALONE_AID,
    AccessoryType,
    HomeAccessory,
    drop_empty,
    entity_field,
    find_entity,
    numeric_state,
    optional_entities,
    set_clamped,
)
from .sensors import (
    add_co2_service,
    add_humidity_service,
    add_temperature_service,
    homekit_air_quality,
    level_for,
)

# key: (pollutant for thresholds, HomeKit density characteristic or None)
POLLUTANTS: dict[str, tuple[str, str | None]] = {
    CONF_PM25_SENSOR: ("pm25", "PM2.5Density"),
    CONF_PM10_SENSOR: ("pm10", "PM10Density"),
    CONF_VOC_SENSOR: ("voc", "VOCDensity"),
    CONF_NO2_SENSOR: ("no2", "NitrogenDioxideDensity"),
    CONF_OZONE_SENSOR: ("ozone", "OzoneDensity"),
    CONF_SO2_SENSOR: ("so2", "SulphurDioxideDensity"),
    CONF_CO2_SENSOR: ("co2", None),  # its own Carbon Dioxide service
}
EXTRA_KEYS = (CONF_TEMPERATURE_SENSOR, CONF_HUMIDITY_SENSOR)
ALL_KEYS = (CONF_AQI_SENSOR, *POLLUTANTS, *EXTRA_KEYS)

DETECT_CLASSES = {
    CONF_AQI_SENSOR: "aqi",
    CONF_PM25_SENSOR: "pm25",
    CONF_PM10_SENSOR: "pm10",
    CONF_VOC_SENSOR: {"volatile_organic_compounds", "volatile_organic_compounds_parts"},
    CONF_NO2_SENSOR: "nitrogen_dioxide",
    CONF_OZONE_SENSOR: "ozone",
    CONF_SO2_SENSOR: "sulphur_dioxide",
    CONF_CO2_SENSOR: "carbon_dioxide",
    CONF_TEMPERATURE_SENSOR: "temperature",
    CONF_HUMIDITY_SENSOR: "humidity",
}


def _schema() -> dict[vol.Marker, Any]:
    return {vol.Optional(key): entity_field("sensor") for key in ALL_KEYS}


def _detect(hass: HomeAssistant, entries: list[er.RegistryEntry]) -> dict[str, Any]:
    return drop_empty(
        {
            key: find_entity(entries, "sensor", classes)
            for key, classes in DETECT_CLASSES.items()
        }
    )


def _normalize(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    optional_entities(data, ALL_KEYS)
    has_reading = data[CONF_AQI_SENSOR] or any(data[key] for key in POLLUTANTS)
    return data, {} if has_reading else {"base": "no_pollutants_selected"}


class AirQualityAccessory(HomeAccessory):
    """Air Quality service; overall quality is the worst pollutant (or an AQI sensor)."""

    category = CATEGORY_SENSOR

    def __init__(
        self,
        hass: HomeAssistant,
        driver: AccessoryDriver,
        entry: ConfigEntry,
        aid: int = STANDALONE_AID,
    ) -> None:
        """Initialize the accessory."""
        super().__init__(hass, driver, entry, AIR_QUALITY.model, aid)
        self.aqi_sensor: str | None = self.data.get(CONF_AQI_SENSOR)
        configured = {
            key: spec for key, spec in POLLUTANTS.items() if self.data.get(key)
        }
        density_chars = [char for _, char in configured.values() if char]

        air = self.add_named_service(
            "AirQualitySensor", self.name, ["AirQuality", *density_chars]
        )
        air.is_primary_service = True
        self._quality = air.configure_char("AirQuality", value=0)
        self._densities: dict[str, Characteristic] = {
            key: air.configure_char(char, value=0)
            for key, (_, char) in configured.items()
            if char
        }
        self._levels: dict[str, int] = {}

        handlers: dict[str | None, Any] = {self.aqi_sensor: self._update_aqi}
        for key in configured:
            handlers[self.data[key]] = self._pollutant_handler(key)
        if co2 := self.data.get(CONF_CO2_SENSOR):
            service, co2_handler = add_co2_service(self, f"{self.name} CO₂")
            air.add_linked_service(service)
            pollutant_handler = handlers[co2]

            def both(state: State) -> None:
                co2_handler(state)
                pollutant_handler(state)

            handlers[co2] = both
        for key, add in (
            (CONF_TEMPERATURE_SENSOR, add_temperature_service),
            (CONF_HUMIDITY_SENSOR, add_humidity_service),
        ):
            if entity_id := self.data.get(key):
                service, handler = add(self, f"{self.name} {key.split('_')[0].title()}")
                air.add_linked_service(service)
                handlers[entity_id] = handler
        self.track(handlers)

    def _pollutant_handler(self, key: str):
        pollutant = POLLUTANTS[key][0]

        @callback
        def update(state: State) -> None:
            value = numeric_state(state)
            if value is None:
                self._levels.pop(key, None)
            else:
                if (char := self._densities.get(key)) is not None:
                    set_clamped(char, value)
                self._levels[key] = level_for(pollutant, value)
            self._update_quality()

        return update

    @callback
    def _update_aqi(self, state: State) -> None:
        if (value := numeric_state(state)) is not None:
            self._levels["aqi"] = homekit_air_quality(value)
            self._update_quality()

    def _update_quality(self) -> None:
        if "aqi" in self._levels:
            self._quality.set_value(self._levels["aqi"])
        else:
            self._quality.set_value(max(self._levels.values(), default=0))


AIR_QUALITY = AccessoryType(
    key="air_quality",
    model="HomeKit Extended Air Quality Monitor",
    default_name="Air Quality",
    device_domains=("sensor",),
    schema=_schema,
    detect=_detect,
    normalize=_normalize,
    factory=AirQualityAccessory,
    entity_keys=ALL_KEYS,
)
