"""Ceiling fan: a fan and its light as one HomeKit accessory."""

from __future__ import annotations

from typing import Any

from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_FAN
import voluptuous as vol

from homeassistant.components.fan import (
    ATTR_DIRECTION,
    ATTR_OSCILLATING,
    ATTR_PERCENTAGE,
    ATTR_PERCENTAGE_STEP,
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    DOMAIN as FAN_DOMAIN,
    SERVICE_OSCILLATE,
    SERVICE_SET_DIRECTION,
    SERVICE_SET_PERCENTAGE,
    FanEntityFeature,
)
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_BRIGHTNESS_PCT,
    ATTR_SUPPORTED_COLOR_MODES,
    DOMAIN as LIGHT_DOMAIN,
    brightness_supported,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er

from ..const import CONF_FAN, CONF_LIGHT
from .base import (
    IGNORED_STATES,
    AccessoryType,
    HomeAccessory,
    drop_empty,
    entity_field,
    find_entity,
)

HK_DIRECTION = {DIRECTION_FORWARD: 0, DIRECTION_REVERSE: 1}  # clockwise, counter


def _schema() -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_FAN): entity_field("fan"),
        vol.Required(CONF_LIGHT): entity_field("light"),
    }


def _detect(hass: HomeAssistant, entries: list[er.RegistryEntry]) -> dict[str, Any]:
    return drop_empty(
        {
            CONF_FAN: find_entity(entries, "fan"),
            CONF_LIGHT: find_entity(entries, "light"),
        }
    )


def _normalize(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    return data, {}


class CeilingFanAccessory(HomeAccessory):
    """Fan service with a linked Lightbulb service."""

    category = CATEGORY_FAN

    def __init__(
        self, hass: HomeAssistant, driver: AccessoryDriver, entry: ConfigEntry
    ) -> None:
        """Initialize the accessory."""
        super().__init__(hass, driver, entry, CEILING_FAN.model)
        self.fan: str = self.data[CONF_FAN]
        self.light: str = self.data[CONF_LIGHT]

        fan_state = hass.states.get(self.fan)
        fan_attrs = dict(fan_state.attributes) if fan_state else {}
        features = fan_attrs.get(ATTR_SUPPORTED_FEATURES) or 0
        fan_chars = ["Active"]
        if features & FanEntityFeature.SET_SPEED:
            fan_chars.append("RotationSpeed")
        if features & FanEntityFeature.OSCILLATE:
            fan_chars.append("SwingMode")
        if features & FanEntityFeature.DIRECTION:
            fan_chars.append("RotationDirection")

        fan = self.add_named_service("Fanv2", self.name, fan_chars)
        fan.is_primary_service = True
        self._fan_active = fan.configure_char("Active", value=0)
        self._speed = (
            fan.configure_char(
                "RotationSpeed",
                value=0,
                properties={"minStep": fan_attrs.get(ATTR_PERCENTAGE_STEP) or 1},
            )
            if "RotationSpeed" in fan_chars
            else None
        )
        self._swing = (
            fan.configure_char("SwingMode", value=0)
            if "SwingMode" in fan_chars
            else None
        )
        self._direction = (
            fan.configure_char("RotationDirection", value=0)
            if "RotationDirection" in fan_chars
            else None
        )
        fan.setter_callback = self._set_fan

        light_state = hass.states.get(self.light)
        modes = (
            light_state.attributes.get(ATTR_SUPPORTED_COLOR_MODES)
            if light_state
            else None
        ) or []
        light_chars = ["On"]
        if brightness_supported(modes):
            light_chars.append("Brightness")
        light = self.add_named_service("Lightbulb", f"{self.name} Light", light_chars)
        fan.add_linked_service(light)
        self._light_on = light.configure_char("On", value=False)
        self._brightness = (
            light.configure_char("Brightness", value=100)
            if "Brightness" in light_chars
            else None
        )
        light.setter_callback = self._set_light

        self.track({self.fan: self._update_fan, self.light: self._update_light})

    # HomeKit -> Home Assistant

    def _set_fan(self, values: dict[str, Any]) -> None:
        if values.get("Active") == 0:
            self.call_service(FAN_DOMAIN, SERVICE_TURN_OFF, self.fan)
            return
        if "RotationSpeed" in values:
            percentage = int(values["RotationSpeed"])
            if percentage == 0:
                self.call_service(FAN_DOMAIN, SERVICE_TURN_OFF, self.fan)
                return
            self.call_service(
                FAN_DOMAIN,
                SERVICE_SET_PERCENTAGE,
                self.fan,
                **{ATTR_PERCENTAGE: percentage},
            )
        elif values.get("Active") == 1:
            self.call_service(FAN_DOMAIN, SERVICE_TURN_ON, self.fan)
        if "SwingMode" in values:
            self.call_service(
                FAN_DOMAIN,
                SERVICE_OSCILLATE,
                self.fan,
                **{ATTR_OSCILLATING: values["SwingMode"] == 1},
            )
        if "RotationDirection" in values:
            direction = (
                DIRECTION_REVERSE
                if values["RotationDirection"] == 1
                else DIRECTION_FORWARD
            )
            self.call_service(
                FAN_DOMAIN,
                SERVICE_SET_DIRECTION,
                self.fan,
                **{ATTR_DIRECTION: direction},
            )

    def _set_light(self, values: dict[str, Any]) -> None:
        if values.get("On") is False or values.get("Brightness") == 0:
            self.call_service(LIGHT_DOMAIN, SERVICE_TURN_OFF, self.light)
        elif "Brightness" in values:
            self.call_service(
                LIGHT_DOMAIN,
                SERVICE_TURN_ON,
                self.light,
                **{ATTR_BRIGHTNESS_PCT: int(values["Brightness"])},
            )
        elif values.get("On") is True:
            self.call_service(LIGHT_DOMAIN, SERVICE_TURN_ON, self.light)

    # Home Assistant -> HomeKit

    @callback
    def _update_fan(self, state: State) -> None:
        if state.state in IGNORED_STATES:
            return
        is_on = state.state == STATE_ON
        self._fan_active.set_value(1 if is_on else 0)
        attrs = state.attributes
        if self._speed is not None and is_on and (pct := attrs.get(ATTR_PERCENTAGE)):
            self._speed.set_value(pct)
        if self._swing is not None and isinstance(
            osc := attrs.get(ATTR_OSCILLATING), bool
        ):
            self._swing.set_value(1 if osc else 0)
        if (
            self._direction is not None
            and (direction := attrs.get(ATTR_DIRECTION)) in HK_DIRECTION
        ):
            self._direction.set_value(HK_DIRECTION[direction])

    @callback
    def _update_light(self, state: State) -> None:
        if state.state in IGNORED_STATES:
            return
        is_on = state.state == STATE_ON
        self._light_on.set_value(is_on)
        if (
            self._brightness is not None
            and is_on
            and (brightness := state.attributes.get(ATTR_BRIGHTNESS))
        ):
            self._brightness.set_value(max(1, round(brightness / 255 * 100)))


CEILING_FAN = AccessoryType(
    key="ceiling_fan",
    model="HomeKit Extended Ceiling Fan",
    default_name="Ceiling Fan",
    device_domains=("fan", "light"),
    schema=_schema,
    detect=_detect,
    normalize=_normalize,
    factory=CeilingFanAccessory,
    entity_keys=(CONF_FAN, CONF_LIGHT),
)
