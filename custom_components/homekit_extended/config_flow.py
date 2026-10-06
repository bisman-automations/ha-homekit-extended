"""Config flow for HomeKit Extended.

Adding an accessory is two short steps: pick a name and (optionally) the device
it represents, then confirm the entities, which are pre-filled from that device.
The port and pairing code are suggested automatically and tucked away in a
collapsed section.
"""

from __future__ import annotations

import socket
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
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr, selector

from . import async_reset_pairing_state
from .accessories import ACCESSORY_TYPES, AccessoryType
from .accessories.base import (
    FIRMWARE_RE,
    INFO_KEYS,
    MAX_INFO_LENGTH,
    device_entities,
    device_name,
    firmware_version,
)
from .accessories.valves import (
    async_save_run_times,
    run_times_details,
    run_times_form,
    zone_run_times,
)
from .const import (
    CONF_ACCESSORY_TYPE,
    CONF_CONNECTION,
    CONF_DEVICE,
    CONF_FIRMWARE,
    CONF_INFO,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_PIN,
    CONF_PLAIN_NAME,
    CONF_PORT,
    CONF_RUN_TIMES,
    CONF_SERIAL,
    CONF_USE_CONTROLLER,
    DEFAULT_PORT,
    DOMAIN,
    VERSION,
)
from .helpers import generate_pin, validate_pin
from .pairing import pairing_markdown

# Core HomeKit Bridge and the standalone integrations this one replaced.
OTHER_HAP_DOMAINS = ("homekit", "homekit_irrigation", "homekit_air_purifier")
CONF_RESET_PAIRING = "reset_pairing"

PORT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=1024, max=65535, step=1, mode=selector.NumberSelectorMode.BOX
    )
)


def _connection_fields() -> dict[vol.Marker, Any]:
    return {
        vol.Required(CONF_PORT): PORT_SELECTOR,
        vol.Required(CONF_PIN): selector.TextSelector(),
        vol.Optional(CONF_PLAIN_NAME, default=False): selector.BooleanSelector(),
    }


def _info_fields() -> dict[vol.Marker, Any]:
    return {vol.Optional(key): selector.TextSelector() for key in INFO_KEYS}


def _device_info(hass: HomeAssistant, device_id: str) -> dict[str, str]:
    """Suggest accessory information from the device it represents."""
    if (device := dr.async_get(hass).async_get(device_id)) is None:
        return {}
    suggested = {
        CONF_MANUFACTURER: device.manufacturer,
        CONF_MODEL: device.model,
        CONF_SERIAL: device.serial_number,
        CONF_FIRMWARE: firmware_version(device.sw_version),
    }
    return {key: str(value) for key, value in suggested.items() if value}


def _validate_info(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Blank fields fall back to defaults; filled ones must suit HomeKit."""
    data = {key: str(raw.get(key) or "").strip() or None for key in INFO_KEYS}
    errors: dict[str, str] = {}
    for key, value in data.items():
        if value and len(value) > MAX_INFO_LENGTH:
            errors[key] = "info_too_long"
    if data[CONF_SERIAL] and len(data[CONF_SERIAL]) < 2:
        errors[CONF_SERIAL] = "serial_too_short"
    if data[CONF_FIRMWARE] and not FIRMWARE_RE.match(data[CONF_FIRMWARE]):
        errors[CONF_FIRMWARE] = "invalid_firmware"
    return data, errors


def _used_ports(hass: HomeAssistant, exclude_entry_id: str | None = None) -> set[int]:
    """Ports taken by this integration and by core HomeKit Bridge entries."""
    ports: set[int] = set()
    for domain in (DOMAIN, *OTHER_HAP_DOMAINS):
        for entry in hass.config_entries.async_entries(domain):
            if entry.entry_id == exclude_entry_id:
                continue
            if (port := {**entry.data, **entry.options}.get(CONF_PORT)) is not None:
                ports.add(int(port))
    return ports


def port_available(port: int) -> bool:
    """Return true if nothing on this host is listening on the TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


async def _next_free_port(hass: HomeAssistant) -> int:
    used = _used_ports(hass)
    port = DEFAULT_PORT
    while port in used or not await hass.async_add_executor_job(port_available, port):
        port += 1
    return port


async def _validate_connection(
    hass: HomeAssistant,
    user_input: dict[str, Any],
    exclude_entry_id: str | None = None,
    current_port: int | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    data = {
        CONF_PORT: int(user_input[CONF_PORT]),
        CONF_PIN: str(user_input[CONF_PIN]).strip(),
        CONF_PLAIN_NAME: bool(user_input.get(CONF_PLAIN_NAME, False)),
    }
    errors: dict[str, str] = {}
    if not validate_pin(data[CONF_PIN]):
        errors[CONF_PIN] = "invalid_pin"
    port = data[CONF_PORT]
    if port in _used_ports(hass, exclude_entry_id):
        errors[CONF_PORT] = "port_in_use"
    # The entry's own running accessory holds its current port.
    elif port != current_port and not await hass.async_add_executor_job(
        port_available, port
    ):
        errors[CONF_PORT] = "port_busy"
    return data, errors


class HomeKitExtendedConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a HomeKit Extended accessory."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._type: AccessoryType | None = None
        self._title = ""
        self._connection: dict[str, Any] = {}
        self._suggested: dict[str, Any] = {}
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which kind of accessory to publish."""
        return self.async_show_menu(step_id="user", menu_options=list(ACCESSORY_TYPES))

    async def _async_step_basics(
        self, accessory_type: AccessoryType, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Name, source device and connection settings."""
        self._type = accessory_type
        errors: dict[str, str] = {}
        if user_input is not None:
            self._connection, errors = await _validate_connection(
                self.hass, user_input[CONF_CONNECTION]
            )
            if not errors:
                device_id = user_input.get(CONF_DEVICE)
                if device_id:
                    entries = device_entities(self.hass, device_id)
                    self._suggested = {
                        **accessory_type.detect(self.hass, entries),
                        CONF_INFO: _device_info(self.hass, device_id),
                    }
                self._title = (
                    (user_input.get(CONF_NAME) or "").strip()
                    or (device_name(self.hass, device_id) if device_id else None)
                    or accessory_type.default_name
                )
                return await self.async_step_entities()

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME): selector.TextSelector(),
                vol.Optional(CONF_DEVICE): selector.DeviceSelector(
                    selector.DeviceSelectorConfig(
                        entity=[
                            selector.EntityFilterSelectorConfig(domain=domain)
                            for domain in accessory_type.device_domains
                        ]
                    )
                ),
                vol.Required(CONF_CONNECTION): section(
                    vol.Schema(_connection_fields()), {"collapsed": True}
                ),
            }
        )
        suggested = user_input or {
            CONF_CONNECTION: {
                CONF_PORT: await _next_free_port(self.hass),
                CONF_PIN: generate_pin(),
            }
        }
        return self.async_show_form(
            step_id=accessory_type.key,
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the entities, pre-filled from the chosen device."""
        assert self._type is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_input = dict(user_input)
            info, info_errors = _validate_info(entity_input.pop(CONF_INFO, {}))
            data, errors = self._type.normalize(entity_input)
            errors = {**errors, **info_errors}
            if not errors:
                self._data = {
                    CONF_ACCESSORY_TYPE: self._type.key,
                    **self._connection,
                    **data,
                    **{key: value for key, value in info.items() if value},
                }
                if self._type.zones_key:
                    return await self.async_step_run_times()
                return self.async_create_entry(title=self._title, data=self._data)
        schema = vol.Schema(
            {
                **self._type.schema(),
                vol.Required(CONF_INFO, default={}): section(
                    vol.Schema(_info_fields()), {"collapsed": True}
                ),
            }
        )
        return self.async_show_form(
            step_id="entities",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or self._suggested
            ),
            errors=errors,
            description_placeholders={"name": self._title},
        )

    async def async_step_run_times(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set how long each zone runs when started from Apple Home."""
        assert self._type is not None and self._type.zones_key
        valves = self._data[self._type.zones_key]
        use_controller = bool(self._data.get(CONF_USE_CONTROLLER, True))
        schema, labels, suggested = run_times_form(
            self.hass, valves, zone_run_times(self._data, valves), use_controller
        )
        if user_input is not None:
            self._data[CONF_RUN_TIMES] = await async_save_run_times(
                self.hass, user_input, labels, use_controller
            )
            return self.async_create_entry(title=self._title, data=self._data)
        return self.async_show_form(
            step_id="run_times",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), suggested
            ),
            description_placeholders={
                "name": self._title,
                "details": run_times_details(self.hass, valves, use_controller),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return HomeKitExtendedOptionsFlow()


def _make_type_step(accessory_type: AccessoryType):
    async def step(
        self: HomeKitExtendedConfigFlow, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_basics(accessory_type, user_input)

    step.__name__ = f"async_step_{accessory_type.key}"
    step.__doc__ = f"Configure a {accessory_type.default_name} accessory."
    return step


for _accessory_type in ACCESSORY_TYPES.values():
    setattr(
        HomeKitExtendedConfigFlow,
        f"async_step_{_accessory_type.key}",
        _make_type_step(_accessory_type),
    )


class HomeKitExtendedOptionsFlow(OptionsFlow):
    """Manage an accessory: entities, connection and pairing.

    Rename the accessory by renaming the integration entry.
    """

    @property
    def _type(self) -> AccessoryType:
        return ACCESSORY_TYPES[self.config_entry.data[CONF_ACCESSORY_TYPE]]

    @property
    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    def _save(self, changes: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose what to manage."""
        return self.async_show_menu(
            step_id="init",
            menu_options=(
                ["entities", "run_times", "info", "connection", "pairing"]
                if self._type.zones_key
                else ["entities", "info", "connection", "pairing"]
            ),
            description_placeholders={"name": self.config_entry.title},
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change which entities the accessory mirrors."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = self._type.normalize(dict(user_input))
            if not errors:
                return self._save(data)
        current = self._current
        for key in self._type.entity_keys:
            value = current.get(key)
            if isinstance(value, list):
                current[key] = [
                    v["entity_id"] if isinstance(v, dict) else v for v in value
                ]
        return self.async_show_form(
            step_id="entities",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(self._type.schema()), user_input or current
            ),
            errors=errors,
            description_placeholders={"name": self.config_entry.title},
        )

    async def async_step_run_times(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change each zone's run time."""
        current = self._current
        valves = [
            v["entity_id"] if isinstance(v, dict) else v
            for v in current[self._type.zones_key]  # type: ignore[index]
        ]
        use_controller = bool(current.get(CONF_USE_CONTROLLER, True))
        schema, labels, suggested = run_times_form(
            self.hass, valves, zone_run_times(current, valves), use_controller
        )
        if user_input is not None:
            stored = await async_save_run_times(
                self.hass, user_input, labels, use_controller
            )
            return self._save({CONF_RUN_TIMES: stored})
        return self.async_show_form(
            step_id="run_times",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), suggested
            ),
            description_placeholders={
                "name": self.config_entry.title,
                "details": run_times_details(self.hass, valves, use_controller),
            },
        )

    async def async_step_info(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change what Apple Home shows under the accessory's details."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _validate_info(user_input)
            if not errors:
                return self._save(data)
        return self.async_show_form(
            step_id="info",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(_info_fields()), user_input or self._current
            ),
            errors=errors,
            description_placeholders={
                "name": self.config_entry.title,
                "model": self._type.model,
                "version": VERSION,
            },
        )

    async def async_step_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the port or pairing code."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _validate_connection(
                self.hass,
                user_input,
                self.config_entry.entry_id,
                current_port=int(self._current[CONF_PORT]),
            )
            if not errors:
                return self._save(data)
        return self.async_show_form(
            step_id="connection",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(_connection_fields()), user_input or self._current
            ),
            errors=errors,
        )

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the pairing code, or reset pairing so it can be added again."""
        server = getattr(self.config_entry, "runtime_data", None)
        if user_input is not None:
            if user_input.get(CONF_RESET_PAIRING):
                entry_id = self.config_entry.entry_id
                await self.hass.config_entries.async_unload(entry_id)
                await async_reset_pairing_state(self.hass, self.config_entry)
                await self.hass.config_entries.async_setup(entry_id)
            return self.async_create_entry(data=dict(self.config_entry.options))

        if server is None or server.accessory is None:
            status = (
                "The accessory isn't running. Check the logs, then reload the entry."
            )
        elif server.paired:
            status = (
                "**Paired with Apple Home.** To move it to another home, remove it "
                "in Apple Home, or reset pairing below."
            )
        else:
            status = pairing_markdown(self.hass, server)
        return self.async_show_form(
            step_id="pairing",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_RESET_PAIRING, default=False
                    ): selector.BooleanSelector()
                }
            ),
            description_placeholders={"status": status},
        )
