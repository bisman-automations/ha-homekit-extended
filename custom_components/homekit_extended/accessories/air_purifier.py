"""Air Purifier: a fan plus optional sensors as one HomeKit accessory."""

from __future__ import annotations

from typing import Any

from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_AIR_PURIFIER
import voluptuous as vol

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
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er

from ..const import (
    CONF_AIR_QUALITY_SENSOR,
    CONF_FAN,
    CONF_FILTER_LIFE_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_PM25_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    FILTER_CHANGE_THRESHOLD,
)
from .base import (
    IGNORED_STATES,
    STANDALONE_AID,
    AccessoryType,
    HomeAccessory,
    drop_empty,
    entity_field,
    find_entity,
    numeric_state,
    optional_entities,
)
from .sensors import (
    add_humidity_service,
    add_temperature_service,
    homekit_air_quality,
    pm25_air_quality,
)

CHAR_ACTIVE = "Active"
CHAR_AIR_QUALITY = "AirQuality"
CHAR_CURRENT_AIR_PURIFIER_STATE = "CurrentAirPurifierState"
CHAR_FILTER_CHANGE_INDICATION = "FilterChangeIndication"
CHAR_FILTER_LIFE_LEVEL = "FilterLifeLevel"
CHAR_PM25_DENSITY = "PM2.5Density"
CHAR_ROTATION_SPEED = "RotationSpeed"
CHAR_SWING_MODE = "SwingMode"
CHAR_TARGET_AIR_PURIFIER_STATE = "TargetAirPurifierState"

SERV_AIR_PURIFIER = "AirPurifier"
SERV_AIR_QUALITY_SENSOR = "AirQualitySensor"
SERV_FANV2 = "Fanv2"
SERV_FILTER_MAINTENANCE = "FilterMaintenance"

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

SENSOR_KEYS = (
    CONF_AIR_QUALITY_SENSOR,
    CONF_PM25_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_FILTER_LIFE_SENSOR,
)


def _schema() -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_FAN): entity_field("fan"),
        **{vol.Optional(key): entity_field("sensor") for key in SENSOR_KEYS},
    }


def _detect(hass: HomeAssistant, entries: list[er.RegistryEntry]) -> dict[str, Any]:
    filter_life = next(
        (
            entry.entity_id
            for entry in entries
            if entry.domain == "sensor"
            and "filter" in (entry.entity_id + (entry.original_name or "")).lower()
        ),
        None,
    )
    return drop_empty(
        {
            CONF_FAN: find_entity(entries, "fan"),
            CONF_AIR_QUALITY_SENSOR: find_entity(entries, "sensor", "aqi"),
            CONF_PM25_SENSOR: find_entity(entries, "sensor", "pm25"),
            CONF_HUMIDITY_SENSOR: find_entity(entries, "sensor", "humidity"),
            CONF_TEMPERATURE_SENSOR: find_entity(entries, "sensor", "temperature"),
            CONF_FILTER_LIFE_SENSOR: filter_life,
        }
    )


def _normalize(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    optional_entities(data, SENSOR_KEYS)
    return data, {}


def _supports(attrs: dict[str, Any], feature: FanEntityFeature) -> bool:
    return bool((attrs.get(ATTR_SUPPORTED_FEATURES) or 0) & feature)


class AirPurifierAccessory(HomeAccessory):
    """Air Purifier service with linked fan, air quality and sensor services."""

    category = CATEGORY_AIR_PURIFIER

    def __init__(
        self,
        hass: HomeAssistant,
        driver: AccessoryDriver,
        entry: ConfigEntry,
        aid: int = STANDALONE_AID,
    ) -> None:
        """Initialize the accessory."""
        super().__init__(hass, driver, entry, AIR_PURIFIER.model, aid)
        self.fan: str = self.data[CONF_FAN]
        self.air_quality_sensor: str | None = self.data.get(CONF_AIR_QUALITY_SENSOR)
        fan_state = hass.states.get(self.fan)
        attrs = dict(fan_state.attributes) if fan_state is not None else {}

        self._auto_preset = next(
            (
                str(p)
                for p in attrs.get(ATTR_PRESET_MODES) or []
                if str(p).lower() == "auto"
            ),
            None,
        )
        supports_speed = _supports(attrs, FanEntityFeature.SET_SPEED)
        supports_swing = _supports(attrs, FanEntityFeature.OSCILLATE)
        speed_props = {
            **SPEED_PROPERTIES,
            PROP_MIN_STEP: attrs.get(ATTR_PERCENTAGE_STEP) or 1,
        }
        initial_speed = max(1, int(attrs.get(ATTR_PERCENTAGE) or 100))
        extra = [CHAR_ROTATION_SPEED] * supports_speed + [
            CHAR_SWING_MODE
        ] * supports_swing

        purifier = self.add_named_service(
            SERV_AIR_PURIFIER,
            self.name,
            [
                CHAR_ACTIVE,
                CHAR_CURRENT_AIR_PURIFIER_STATE,
                CHAR_TARGET_AIR_PURIFIER_STATE,
                *extra,
            ],
        )
        purifier.is_primary_service = True
        fan = self.add_named_service(
            SERV_FANV2, f"{self.name} Fan", [CHAR_ACTIVE, *extra]
        )
        purifier.add_linked_service(fan)

        self._active = [
            s.configure_char(CHAR_ACTIVE, value=HK_INACTIVE) for s in (purifier, fan)
        ]
        self._current_state = purifier.configure_char(
            CHAR_CURRENT_AIR_PURIFIER_STATE, value=HK_CURRENT_INACTIVE
        )
        self._target_state = purifier.configure_char(
            CHAR_TARGET_AIR_PURIFIER_STATE, value=HK_TARGET_MANUAL
        )
        self._speed = (
            [
                s.configure_char(
                    CHAR_ROTATION_SPEED, value=initial_speed, properties=speed_props
                )
                for s in (purifier, fan)
            ]
            if supports_speed
            else []
        )
        self._swing = (
            [
                s.configure_char(CHAR_SWING_MODE, value=HK_SWING_DISABLED)
                for s in (purifier, fan)
            ]
            if supports_swing
            else []
        )
        purifier.setter_callback = self._set_chars
        fan.setter_callback = self._set_chars

        self._air_quality = self._pm25 = None
        if self.air_quality_sensor or self.data.get(CONF_PM25_SENSOR):
            air = self.add_named_service(
                SERV_AIR_QUALITY_SENSOR,
                f"{self.name} Air Quality",
                [CHAR_AIR_QUALITY, CHAR_PM25_DENSITY],
            )
            purifier.add_linked_service(air)
            self._air_quality = air.configure_char(CHAR_AIR_QUALITY, value=0)
            self._pm25 = air.configure_char(CHAR_PM25_DENSITY, value=0)

        handlers = {
            self.fan: self._update_fan,
            self.air_quality_sensor: self._update_air_quality,
            self.data.get(CONF_PM25_SENSOR): self._update_pm25,
        }
        for key, add in (
            (CONF_HUMIDITY_SENSOR, add_humidity_service),
            (CONF_TEMPERATURE_SENSOR, add_temperature_service),
        ):
            if entity_id := self.data.get(key):
                service, handler = add(self, f"{self.name} {key.split('_')[0].title()}")
                purifier.add_linked_service(service)
                handlers[entity_id] = handler

        self._filter_change = self._filter_life = None
        if entity_id := self.data.get(CONF_FILTER_LIFE_SENSOR):
            filt = self.add_named_service(
                SERV_FILTER_MAINTENANCE,
                f"{self.name} Filter",
                [CHAR_FILTER_CHANGE_INDICATION, CHAR_FILTER_LIFE_LEVEL],
            )
            purifier.add_linked_service(filt)
            self._filter_change = filt.configure_char(
                CHAR_FILTER_CHANGE_INDICATION, value=HK_FILTER_OK
            )
            self._filter_life = filt.configure_char(CHAR_FILTER_LIFE_LEVEL, value=100)
            handlers[entity_id] = self._update_filter_life

        self.track(handlers)

    # HomeKit -> Home Assistant

    def _set_chars(self, values: dict[str, Any]) -> None:
        if CHAR_ACTIVE in values:
            if values[CHAR_ACTIVE] != HK_ACTIVE:
                self._fan_service(SERVICE_TURN_OFF)
                return
            if CHAR_ROTATION_SPEED not in values:
                self._fan_service(SERVICE_TURN_ON)
        if CHAR_TARGET_AIR_PURIFIER_STATE in values:
            if values[CHAR_TARGET_AIR_PURIFIER_STATE] == HK_TARGET_AUTO:
                if self._auto_preset is not None:
                    self._fan_service(
                        SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: self._auto_preset}
                    )
            elif self._speed:
                self._fan_service(
                    SERVICE_SET_PERCENTAGE,
                    **{ATTR_PERCENTAGE: max(1, int(self._speed[0].get_value() or 50))},
                )
        if CHAR_SWING_MODE in values:
            self._fan_service(
                SERVICE_OSCILLATE,
                **{ATTR_OSCILLATING: values[CHAR_SWING_MODE] == HK_SWING_ENABLED},
            )
        if CHAR_ROTATION_SPEED in values:
            percentage = max(0, min(100, int(values[CHAR_ROTATION_SPEED])))
            if percentage == 0:
                self._fan_service(SERVICE_TURN_OFF)
            else:
                self._fan_service(
                    SERVICE_SET_PERCENTAGE, **{ATTR_PERCENTAGE: percentage}
                )

    def _fan_service(self, service: str, **data: Any) -> None:
        self.call_service(FAN_DOMAIN, service, self.fan, **data)

    # Home Assistant -> HomeKit

    @callback
    def _update_fan(self, state: State) -> None:
        available = state.state not in IGNORED_STATES
        is_on = available and state.state == STATE_ON
        for char in self._active:
            char.set_value(HK_ACTIVE if is_on else HK_INACTIVE)
        if is_on:
            self._current_state.set_value(HK_CURRENT_PURIFYING)
        elif available and state.state == STATE_OFF:
            self._current_state.set_value(HK_CURRENT_INACTIVE)
        else:
            self._current_state.set_value(HK_CURRENT_IDLE)

        attrs = state.attributes
        if (
            self._speed
            and is_on
            and (percentage := attrs.get(ATTR_PERCENTAGE)) is not None
        ):
            if percentage == 0:
                percentage = max(1, self._speed[0].properties.get(PROP_MIN_STEP, 1))
            for char in self._speed:
                char.set_value(percentage)
        if isinstance(oscillating := attrs.get(ATTR_OSCILLATING), bool):
            for char in self._swing:
                char.set_value(HK_SWING_ENABLED if oscillating else HK_SWING_DISABLED)
        preset = attrs.get(ATTR_PRESET_MODE)
        self._target_state.set_value(
            HK_TARGET_AUTO
            if preset and str(preset).lower() == "auto"
            else HK_TARGET_MANUAL
        )

    @callback
    def _update_air_quality(self, state: State) -> None:
        if (
            self._air_quality is not None
            and (value := numeric_state(state)) is not None
        ):
            self._air_quality.set_value(homekit_air_quality(value))

    @callback
    def _update_pm25(self, state: State) -> None:
        if (value := numeric_state(state)) is None or self._pm25 is None:
            return
        self._pm25.set_value(max(0, min(1000, value)))
        if self._air_quality is not None and not self.air_quality_sensor:
            self._air_quality.set_value(pm25_air_quality(value))

    @callback
    def _update_filter_life(self, state: State) -> None:
        if self._filter_life is None or (value := numeric_state(state)) is None:
            return
        value = max(0, min(100, value))
        self._filter_life.set_value(value)
        self._filter_change.set_value(
            HK_FILTER_CHANGE_NEEDED
            if value <= FILTER_CHANGE_THRESHOLD
            else HK_FILTER_OK
        )


AIR_PURIFIER = AccessoryType(
    key="air_purifier",
    model="HomeKit Extended Air Purifier",
    default_name="Air Purifier",
    device_domains=("fan",),
    schema=_schema,
    detect=_detect,
    normalize=_normalize,
    factory=AirPurifierAccessory,
    entity_keys=(CONF_FAN, *SENSOR_KEYS),
)
