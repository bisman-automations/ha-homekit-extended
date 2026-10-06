"""Config and options flow tests."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homekit_extended.const import (
    ACCESSORY_AIR_PURIFIER,
    ACCESSORY_IRRIGATION,
    DOMAIN,
)
from custom_components.homekit_extended.helpers import generate_pin, validate_pin
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

PIN = "031-45-154"


async def _start(hass: HomeAssistant, accessory_type: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [ACCESSORY_AIR_PURIFIER, ACCESSORY_IRRIGATION]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": accessory_type}
    )


async def test_air_purifier_flow(hass: HomeAssistant) -> None:
    """An air purifier entry stores the fan and blank sensors as None."""
    hass.states.async_set("fan.purifier", "off")
    result = await _start(hass, ACCESSORY_AIR_PURIFIER)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Bedroom Purifier",
            "port": 51829,
            "pin": PIN,
            "fan": "fan.purifier",
            "pm25_sensor": "sensor.pm25",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Bedroom Purifier"
    assert result["data"] == {
        "accessory_type": ACCESSORY_AIR_PURIFIER,
        "port": 51829,
        "pin": PIN,
        "fan": "fan.purifier",
        "air_quality_sensor": None,
        "pm25_sensor": "sensor.pm25",
        "humidity_sensor": None,
        "temperature_sensor": None,
        "filter_life_sensor": None,
    }


async def test_irrigation_flow(hass: HomeAssistant) -> None:
    """An irrigation entry stores valve entity ids."""
    result = await _start(hass, ACCESSORY_IRRIGATION)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Sprinklers",
            "port": 51828,
            "pin": PIN,
            "valves": ["valve.front", "valve.back"],
            "default_duration": 600,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["valves"] == ["valve.front", "valve.back"]
    assert result["data"]["default_duration"] == 600


async def test_flow_errors(hass: HomeAssistant) -> None:
    """Bad pins, taken ports and empty valve lists are rejected."""
    MockConfigEntry(domain="homekit", data={"port": 51828}).add_to_hass(hass)
    result = await _start(hass, ACCESSORY_IRRIGATION)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Sprinklers",
            "port": 51828,
            "pin": "123-45-678",
            "valves": [],
            "default_duration": 600,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        "pin": "invalid_pin",
        "port": "port_in_use",
        "valves": "no_valves_selected",
    }


async def test_suggested_port_skips_used(hass: HomeAssistant) -> None:
    """The suggested port avoids ports already in use."""
    MockConfigEntry(
        domain=DOMAIN, data={"accessory_type": "irrigation", "port": 51829}
    ).add_to_hass(hass)
    result = await _start(hass, ACCESSORY_AIR_PURIFIER)
    port_key = next(k for k in result["data_schema"].schema if k == "port")
    assert port_key.description["suggested_value"] == 51830


async def test_options_flow(hass: HomeAssistant) -> None:
    """Options replace entities, and the entry's own port is not a conflict."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sprinklers",
        data={
            "accessory_type": ACCESSORY_IRRIGATION,
            "port": 51828,
            "pin": PIN,
            # Format stored by the old standalone homekit-irrigation integration.
            "valves": [{"entity_id": "valve.front", "name": "Front"}],
            "default_duration": 900,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "port": 51828,
            "pin": PIN,
            "valves": ["valve.front", "valve.side"],
            "default_duration": 300,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["valves"] == ["valve.front", "valve.side"]
    assert entry.options["default_duration"] == 300


def test_pins() -> None:
    """Generated pins are always valid; spec-forbidden ones are not."""
    assert all(validate_pin(generate_pin()) for _ in range(200))
    for bad in ("111-11-111", "123-45-678", "876-54-321", "12345678", "123-456-78"):
        assert not validate_pin(bad)
