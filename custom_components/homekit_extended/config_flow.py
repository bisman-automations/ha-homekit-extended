"""Config flow for HomeKit Extended."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    ACCESSORY_AIR_PURIFIER,
    ACCESSORY_IRRIGATION,
    ACCESSORY_TYPES,
    AIR_PURIFIER_SENSOR_KEYS,
    CONF_ACCESSORY_TYPE,
    CONF_DEFAULT_DURATION,
    CONF_FAN,
    CONF_PIN,
    CONF_PORT,
    CONF_VALVES,
    DEFAULT_DURATION,
    DEFAULT_PORTS,
    DOMAIN,
)
from .helpers import generate_pin, validate_pin
from .irrigation import MAX_DURATION, valve_entity_ids

CORE_HOMEKIT_DOMAIN = "homekit"
DEFAULT_NAMES = {
    ACCESSORY_AIR_PURIFIER: "Air Purifier",
    ACCESSORY_IRRIGATION: "Irrigation",
}

PORT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1024, max=65535, step=1, mode=selector.NumberSelectorMode.BOX
    )
)
SENSOR_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor")
)


def _connection_schema() -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_PORT): PORT_SELECTOR,
        vol.Required(CONF_PIN): selector.TextSelector(),
    }


def _air_purifier_schema() -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_FAN): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="fan")
        ),
        **{vol.Optional(key): SENSOR_SELECTOR for key in AIR_PURIFIER_SENSOR_KEYS},
    }


def _irrigation_schema() -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_VALVES): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="valve", multiple=True)
        ),
        vol.Required(CONF_DEFAULT_DURATION): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=MAX_DURATION,
                step=1,
                unit_of_measurement="s",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    }


TYPE_SCHEMAS = {
    ACCESSORY_AIR_PURIFIER: _air_purifier_schema,
    ACCESSORY_IRRIGATION: _irrigation_schema,
}


def _used_ports(hass: HomeAssistant, exclude_entry_id: str | None = None) -> set[int]:
    """Ports taken by this integration and by core HomeKit Bridge entries."""
    ports: set[int] = set()
    for domain in (DOMAIN, CORE_HOMEKIT_DOMAIN):
        for entry in hass.config_entries.async_entries(domain):
            if entry.entry_id == exclude_entry_id:
                continue
            if (port := {**entry.data, **entry.options}.get(CONF_PORT)) is not None:
                ports.add(int(port))
    return ports


def _next_free_port(hass: HomeAssistant, accessory_type: str) -> int:
    used = _used_ports(hass)
    port = DEFAULT_PORTS[accessory_type]
    while port in used:
        port += 1
    return port


def _validate(
    hass: HomeAssistant,
    accessory_type: str,
    user_input: dict[str, Any],
    exclude_entry_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalize user input and collect form errors."""
    errors: dict[str, str] = {}
    data = dict(user_input)
    data[CONF_PORT] = int(data[CONF_PORT])
    data[CONF_PIN] = str(data[CONF_PIN]).strip()

    if not validate_pin(data[CONF_PIN]):
        errors[CONF_PIN] = "invalid_pin"
    if data[CONF_PORT] in _used_ports(hass, exclude_entry_id):
        errors[CONF_PORT] = "port_in_use"

    if accessory_type == ACCESSORY_AIR_PURIFIER:
        for key in AIR_PURIFIER_SENSOR_KEYS:
            data[key] = data.get(key) or None
    else:
        data[CONF_VALVES] = valve_entity_ids(data.get(CONF_VALVES))
        data[CONF_DEFAULT_DURATION] = int(data[CONF_DEFAULT_DURATION])
        if not data[CONF_VALVES]:
            errors[CONF_VALVES] = "no_valves_selected"

    return data, errors


class HomeKitExtendedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a HomeKit Extended accessory."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which kind of accessory to publish."""
        return self.async_show_menu(step_id="user", menu_options=list(ACCESSORY_TYPES))

    async def async_step_air_purifier(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure an air purifier accessory."""
        return await self._async_step_accessory(ACCESSORY_AIR_PURIFIER, user_input)

    async def async_step_irrigation(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure an irrigation system accessory."""
        return await self._async_step_accessory(ACCESSORY_IRRIGATION, user_input)

    async def _async_step_accessory(
        self, accessory_type: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _validate(self.hass, accessory_type, user_input)
            if not errors:
                title = data.pop(CONF_NAME).strip() or DEFAULT_NAMES[accessory_type]
                return self.async_create_entry(
                    title=title, data={CONF_ACCESSORY_TYPE: accessory_type, **data}
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): selector.TextSelector(),
                **_connection_schema(),
                **TYPE_SCHEMAS[accessory_type](),
            }
        )
        suggested = user_input or {
            CONF_NAME: DEFAULT_NAMES[accessory_type],
            CONF_PORT: _next_free_port(self.hass, accessory_type),
            CONF_PIN: generate_pin(),
            CONF_DEFAULT_DURATION: DEFAULT_DURATION,
        }
        return self.async_show_form(
            step_id=accessory_type,
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return HomeKitExtendedOptionsFlow()


class HomeKitExtendedOptionsFlow(OptionsFlow):
    """Change the entities, port or PIN of an accessory.

    Rename the accessory by renaming the integration entry.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        accessory_type = self.config_entry.data[CONF_ACCESSORY_TYPE]
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _validate(
                self.hass, accessory_type, user_input, self.config_entry.entry_id
            )
            if not errors:
                return self.async_create_entry(data=data)

        current = {**self.config_entry.data, **self.config_entry.options}
        current[CONF_VALVES] = valve_entity_ids(current.get(CONF_VALVES))
        schema = vol.Schema({**_connection_schema(), **TYPE_SCHEMAS[accessory_type]()})
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or current
            ),
            errors=errors,
            description_placeholders={"title": self.config_entry.title},
        )
