"""HomeKit Air Purifier accessory backed by a fan and optional sensors."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_AIR_PURIFIER

from homeassistant.components.fan import (
    ATTR_OSCILLATING,
    ATTR_PERCENTAGE,
    ATTR_PERCENTAGE_STEP,
    ATTR_PRESET_MODE,
    ATTR_PRESET_MODES,
    DOMAIN as FAN_DOMAIN,
    SERVICE_OSCILLATE,
    SERVICE_SET_PERCENTAGE,
    SERVICE_SET_PRESET_MODE,
    FanEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    ATTR_UNIT_OF_MEASUREMENT,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    ACCESSORY_AIR_PURIFIER,
    CONF_AIR_QUALITY_SENSOR,
    CONF_FAN,
    CONF_FILTER_LIFE_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_PM25_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    FILTER_CHANGE_THRESHOLD,
    MANUFACTURER,
    MODELS,
    VERSION,
)
from .helpers import to_float

_LOGGER = logging.getLogger(__name__)

CHAR_ACTIVE = "Active"
CHAR_AIR_QUALITY = "AirQuality"
CHAR_CONFIGURED_NAME = "ConfiguredName"
CHAR_CURRENT_AIR_PURIFIER_STATE = "CurrentAirPurifierState"
CHAR_CURRENT_HUMIDITY = "CurrentRelativeHumidity"
CHAR_CURRENT_TEMPERATURE = "CurrentTemperature"
CHAR_FILTER_CHANGE_INDICATION = "FilterChangeIndication"
CHAR_FILTER_LIFE_LEVEL = "FilterLifeLevel"
CHAR_NAME = "Name"
CHAR_PM25_DENSITY = "PM2.5Density"
CHAR_ROTATION_SPEED = "RotationSpeed"
CHAR_SWING_MODE = "SwingMode"
CHAR_TARGET_AIR_PURIFIER_STATE = "TargetAirPurifierState"

SERV_AIR_PURIFIER = "AirPurifier"
SERV_AIR_QUALITY_SENSOR = "AirQualitySensor"
SERV_FANV2 = "Fanv2"
SERV_FILTER_MAINTENANCE = "FilterMaintenance"
SERV_HUMIDITY_SENSOR = "HumiditySensor"
SERV_TEMPERATURE_SENSOR = "TemperatureSensor"

HK_ACTIVE = 1
HK_INACTIVE = 0
HK_CURRENT_INACTIVE = 0
HK_CURRENT_IDLE = 1
HK_CURRENT_PURIFYING = 2
HK_TARGET_MANUAL = 0
HK_TARGET_AUTO = 1
HK_SWING_DISABLED = 0
HK_SWING_ENABLED = 1
HK_FILTER_OK = 0
HK_FILTER_CHANGE_NEEDED = 1

PROP_MIN_STEP = "minStep"
SPEED_PROPERTIES = {"minValue": 0, "maxValue": 100, PROP_MIN_STEP: 1}
IGNORED_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN}


@dataclass(slots=True)
class AirPurifierConfig:
    """Entities an air purifier accessory mirrors."""

    name: str
    fan: str
    air_quality_sensor: str | None
    pm25_sensor: str | None
    humidity_sensor: str | None
    temperature_sensor: str | None
    filter_life_sensor: str | None

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> AirPurifierConfig:
        """Build config from entry data merged with options."""
        data = {**entry.data, **entry.options}
        return cls(
            name=entry.title,
            fan=data[CONF_FAN],
            air_quality_sensor=data.get(CONF_AIR_QUALITY_SENSOR) or None,
            pm25_sensor=data.get(CONF_PM25_SENSOR) or None,
            humidity_sensor=data.get(CONF_HUMIDITY_SENSOR) or None,
            temperature_sensor=data.get(CONF_TEMPERATURE_SENSOR) or None,
            filter_life_sensor=data.get(CONF_FILTER_LIFE_SENSOR) or None,
        )

    @property
    def entities(self) -> list[str]:
        """Return every mirrored entity."""
        return [
            entity_id
            for entity_id in (
                self.fan,
                self.air_quality_sensor,
                self.pm25_sensor,
                self.humidity_sensor,
                self.temperature_sensor,
                self.filter_life_sensor,
            )
            if entity_id
        ]


def create_air_purifier(
    hass: HomeAssistant, driver: AccessoryDriver, entry: ConfigEntry
) -> AirPurifierAccessory:
    """Accessory factory for the shared server."""
    return AirPurifierAccessory(
        hass, driver, AirPurifierConfig.from_entry(entry), entry.entry_id
    )


class AirPurifierAccessory(Accessory):
    """One HomeKit accessory with purifier, fan, and sensor services."""

    category = CATEGORY_AIR_PURIFIER

    def __init__(
        self,
        hass: HomeAssistant,
        driver: AccessoryDriver,
        config: AirPurifierConfig,
        serial: str,
    ) -> None:
        """Initialize the accessory."""
        super().__init__(driver=driver, display_name=config.name)
        self.hass = hass
        self.config = config
        self._subscriptions: list[CALLBACK_TYPE] = []
        self._auto_preset: str | None = None

        fan_state = hass.states.get(config.fan)
        fan_attrs = dict(fan_state.attributes) if fan_state is not None else {}
        for preset in fan_attrs.get(ATTR_PRESET_MODES) or []:
            if str(preset).lower() == "auto":
                self._auto_preset = str(preset)
                break

        self.set_info_service(
            manufacturer=MANUFACTURER,
            model=MODELS[ACCESSORY_AIR_PURIFIER],
            serial_number=serial,
            firmware_revision=VERSION,
        )

        supports_speed = _supports(fan_attrs, FanEntityFeature.SET_SPEED)
        supports_swing = _supports(fan_attrs, FanEntityFeature.OSCILLATE)
        speed_props = {
            **SPEED_PROPERTIES,
            PROP_MIN_STEP: fan_attrs.get(ATTR_PERCENTAGE_STEP) or 1,
        }
        initial_speed = max(1, int(fan_attrs.get(ATTR_PERCENTAGE) or 100))

        extra_chars = []
        if supports_speed:
            extra_chars.append(CHAR_ROTATION_SPEED)
        if supports_swing:
            extra_chars.append(CHAR_SWING_MODE)

        purifier = self.add_preload_service(
            SERV_AIR_PURIFIER,
            [
                CHAR_NAME,
                CHAR_CONFIGURED_NAME,
                CHAR_ACTIVE,
                CHAR_CURRENT_AIR_PURIFIER_STATE,
                CHAR_TARGET_AIR_PURIFIER_STATE,
                *extra_chars,
            ],
        )
        purifier.is_primary_service = True
        purifier.configure_char(CHAR_NAME, value=config.name)
        purifier.configure_char(CHAR_CONFIGURED_NAME, value=config.name)
        self._active = purifier.configure_char(CHAR_ACTIVE, value=HK_INACTIVE)
        self._current_state = purifier.configure_char(
            CHAR_CURRENT_AIR_PURIFIER_STATE, value=HK_CURRENT_INACTIVE
        )
        self._target_state = purifier.configure_char(
            CHAR_TARGET_AIR_PURIFIER_STATE, value=HK_TARGET_MANUAL
        )
        self._rotation_speed = (
            purifier.configure_char(
                CHAR_ROTATION_SPEED, value=initial_speed, properties=speed_props
            )
            if supports_speed
            else None
        )
        self._swing_mode = (
            purifier.configure_char(CHAR_SWING_MODE, value=HK_SWING_DISABLED)
            if supports_swing
            else None
        )
        purifier.setter_callback = self._set_chars

        fan = self.add_preload_service(
            SERV_FANV2, [CHAR_NAME, CHAR_ACTIVE, *extra_chars]
        )
        purifier.add_linked_service(fan)
        fan.configure_char(CHAR_NAME, value=f"{config.name} Fan")
        self._fan_active = fan.configure_char(CHAR_ACTIVE, value=HK_INACTIVE)
        self._fan_rotation_speed = (
            fan.configure_char(
                CHAR_ROTATION_SPEED, value=initial_speed, properties=speed_props
            )
            if supports_speed
            else None
        )
        self._fan_swing_mode = (
            fan.configure_char(CHAR_SWING_MODE, value=HK_SWING_DISABLED)
            if supports_swing
            else None
        )
        fan.setter_callback = self._set_chars

        self._air_quality = None
        self._pm25_density = None
        if config.air_quality_sensor or config.pm25_sensor:
            air_quality = self.add_preload_service(
                SERV_AIR_QUALITY_SENSOR,
                [CHAR_NAME, CHAR_AIR_QUALITY, CHAR_PM25_DENSITY],
            )
            purifier.add_linked_service(air_quality)
            air_quality.configure_char(CHAR_NAME, value=f"{config.name} Air Quality")
            self._air_quality = air_quality.configure_char(CHAR_AIR_QUALITY, value=0)
            self._pm25_density = air_quality.configure_char(CHAR_PM25_DENSITY, value=0)

        self._current_humidity = None
        if config.humidity_sensor:
            humidity = self.add_preload_service(
                SERV_HUMIDITY_SENSOR, [CHAR_NAME, CHAR_CURRENT_HUMIDITY]
            )
            purifier.add_linked_service(humidity)
            humidity.configure_char(CHAR_NAME, value=f"{config.name} Humidity")
            self._current_humidity = humidity.configure_char(
                CHAR_CURRENT_HUMIDITY, value=0
            )

        self._current_temperature = None
        if config.temperature_sensor:
            temperature = self.add_preload_service(
                SERV_TEMPERATURE_SENSOR, [CHAR_NAME, CHAR_CURRENT_TEMPERATURE]
            )
            purifier.add_linked_service(temperature)
            temperature.configure_char(CHAR_NAME, value=f"{config.name} Temperature")
            self._current_temperature = temperature.configure_char(
                CHAR_CURRENT_TEMPERATURE, value=0
            )

        self._filter_change = None
        self._filter_life = None
        if config.filter_life_sensor:
            filter_service = self.add_preload_service(
                SERV_FILTER_MAINTENANCE,
                [CHAR_NAME, CHAR_FILTER_CHANGE_INDICATION, CHAR_FILTER_LIFE_LEVEL],
            )
            purifier.add_linked_service(filter_service)
            filter_service.configure_char(CHAR_NAME, value=f"{config.name} Filter")
            self._filter_change = filter_service.configure_char(
                CHAR_FILTER_CHANGE_INDICATION, value=HK_FILTER_OK
            )
            self._filter_life = filter_service.configure_char(
                CHAR_FILTER_LIFE_LEVEL, value=100
            )

        for entity_id in config.entities:
            if (state := hass.states.get(entity_id)) is not None:
                self._update_state(state)

        self._subscriptions.append(
            async_track_state_change_event(
                hass, config.entities, self._async_state_changed
            )
        )

    async def async_stop(self) -> None:
        """Release Home Assistant listeners."""
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()

    # HomeKit -> Home Assistant

    def _set_chars(self, char_values: dict[str, Any]) -> None:
        """Apply HomeKit characteristic writes to the fan."""
        _LOGGER.debug("HomeKit command for %s: %s", self.config.fan, char_values)
        if CHAR_ACTIVE in char_values:
            if char_values[CHAR_ACTIVE] != HK_ACTIVE:
                self._call_fan_service(SERVICE_TURN_OFF)
                return
            if CHAR_ROTATION_SPEED not in char_values:
                self._call_fan_service(SERVICE_TURN_ON)

        if CHAR_TARGET_AIR_PURIFIER_STATE in char_values:
            self._set_target_state(char_values[CHAR_TARGET_AIR_PURIFIER_STATE])

        if CHAR_SWING_MODE in char_values:
            self._call_fan_service(
                SERVICE_OSCILLATE,
                {ATTR_OSCILLATING: char_values[CHAR_SWING_MODE] == HK_SWING_ENABLED},
            )

        if CHAR_ROTATION_SPEED in char_values:
            percentage = max(0, min(100, int(char_values[CHAR_ROTATION_SPEED])))
            if percentage == 0:
                self._call_fan_service(SERVICE_TURN_OFF)
            else:
                self._call_fan_service(
                    SERVICE_SET_PERCENTAGE, {ATTR_PERCENTAGE: percentage}
                )

    def _set_target_state(self, value: int) -> None:
        """Switch between manual and automatic mode."""
        if value == HK_TARGET_AUTO and self._auto_preset is not None:
            self._call_fan_service(
                SERVICE_SET_PRESET_MODE, {ATTR_PRESET_MODE: self._auto_preset}
            )
        elif value == HK_TARGET_MANUAL and self._rotation_speed is not None:
            self._call_fan_service(
                SERVICE_SET_PERCENTAGE,
                {ATTR_PERCENTAGE: max(1, int(self._rotation_speed.get_value() or 50))},
            )

    def _call_fan_service(
        self, service: str, data: dict[str, Any] | None = None
    ) -> None:
        """Call a fan service without blocking the HAP request."""
        self.hass.async_create_task(
            self.hass.services.async_call(
                FAN_DOMAIN,
                service,
                {ATTR_ENTITY_ID: self.config.fan, **(data or {})},
                blocking=False,
            )
        )

    # Home Assistant -> HomeKit

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle a mirrored entity changing."""
        if (new_state := event.data["new_state"]) is not None:
            self._update_state(new_state)

    @callback
    def _update_state(self, state: State) -> None:
        """Route a state to the service that mirrors it."""
        handlers = {
            self.config.fan: self._update_fan,
            self.config.air_quality_sensor: self._update_air_quality,
            self.config.pm25_sensor: self._update_pm25,
            self.config.humidity_sensor: self._update_humidity,
            self.config.temperature_sensor: self._update_temperature,
            self.config.filter_life_sensor: self._update_filter_life,
        }
        if (handler := handlers.get(state.entity_id)) is not None:
            handler(state)

    @callback
    def _update_fan(self, state: State) -> None:
        """Mirror the fan onto the purifier and fan services."""
        available = state.state not in IGNORED_STATES
        is_on = available and state.state == STATE_ON

        self._active.set_value(HK_ACTIVE if is_on else HK_INACTIVE)
        self._fan_active.set_value(HK_ACTIVE if is_on else HK_INACTIVE)
        if is_on:
            self._current_state.set_value(HK_CURRENT_PURIFYING)
        elif available and state.state == STATE_OFF:
            self._current_state.set_value(HK_CURRENT_INACTIVE)
        else:
            self._current_state.set_value(HK_CURRENT_IDLE)

        attrs = state.attributes
        if self._rotation_speed is not None and is_on:
            percentage = attrs.get(ATTR_PERCENTAGE)
            if percentage == 0:
                percentage = max(
                    1, self._rotation_speed.properties.get(PROP_MIN_STEP, 1)
                )
            if percentage is not None:
                self._rotation_speed.set_value(percentage)
                if self._fan_rotation_speed is not None:
                    self._fan_rotation_speed.set_value(percentage)

        oscillating = attrs.get(ATTR_OSCILLATING)
        if self._swing_mode is not None and isinstance(oscillating, bool):
            swing = HK_SWING_ENABLED if oscillating else HK_SWING_DISABLED
            self._swing_mode.set_value(swing)
            if self._fan_swing_mode is not None:
                self._fan_swing_mode.set_value(swing)

        preset_mode = attrs.get(ATTR_PRESET_MODE)
        self._target_state.set_value(
            HK_TARGET_AUTO
            if preset_mode and str(preset_mode).lower() == "auto"
            else HK_TARGET_MANUAL
        )

    @callback
    def _update_air_quality(self, state: State) -> None:
        """Mirror a 1-5 quality or AQI-style sensor."""
        if self._air_quality is None:
            return
        if (value := _numeric(state)) is not None:
            self._air_quality.set_value(homekit_air_quality(value))

    @callback
    def _update_pm25(self, state: State) -> None:
        """Mirror a PM2.5 sensor, deriving quality when no quality sensor is set."""
        if (value := _numeric(state)) is None:
            return
        if self._pm25_density is not None:
            self._pm25_density.set_value(max(0, min(1000, value)))
        if self._air_quality is not None and not self.config.air_quality_sensor:
            self._air_quality.set_value(homekit_air_quality_from_pm25(value))

    @callback
    def _update_humidity(self, state: State) -> None:
        """Mirror a humidity sensor."""
        if (
            self._current_humidity is not None
            and (value := _numeric(state)) is not None
        ):
            self._current_humidity.set_value(max(0, min(100, value)))

    @callback
    def _update_temperature(self, state: State) -> None:
        """Mirror a temperature sensor, converting to Celsius."""
        if self._current_temperature is None or (value := _numeric(state)) is None:
            return
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT, UnitOfTemperature.CELSIUS)
        if unit != UnitOfTemperature.CELSIUS:
            value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        self._current_temperature.set_value(round(value, 1))

    @callback
    def _update_filter_life(self, state: State) -> None:
        """Mirror a filter life percentage."""
        if self._filter_life is None or (value := _numeric(state)) is None:
            return
        value = max(0, min(100, value))
        self._filter_life.set_value(value)
        if self._filter_change is not None:
            self._filter_change.set_value(
                HK_FILTER_CHANGE_NEEDED
                if value <= FILTER_CHANGE_THRESHOLD
                else HK_FILTER_OK
            )


def _numeric(state: State) -> float | None:
    """Return a usable numeric state, or None."""
    if state.state in IGNORED_STATES:
        return None
    return to_float(state.state)


def _supports(attrs: dict[str, Any], feature: FanEntityFeature) -> bool:
    """Return true if the fan advertises a feature."""
    return bool((attrs.get(ATTR_SUPPORTED_FEATURES) or 0) & feature)


def homekit_air_quality(value: float) -> int:
    """Map a 1-5 HomeKit value or an AQI-style value to HomeKit air quality."""
    if 1 <= value <= 5 and value == int(value):
        return int(value)
    for limit, quality in ((50, 1), (100, 2), (150, 3), (200, 4)):
        if value <= limit:
            return quality
    return 5


def homekit_air_quality_from_pm25(value: float) -> int:
    """Map PM2.5 density (µg/m³) to HomeKit air quality."""
    for limit, quality in ((12, 1), (35, 2), (55, 3), (150, 4)):
        if value <= limit:
            return quality
    return 5
