"""Config and options flow tests."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homekit_extended.const import DOMAIN
from custom_components.homekit_extended.helpers import generate_pin, validate_pin
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import PIN, server, setup_accessory

CONNECTION = {"port": 51828, "pin": PIN}


async def _menu(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return result


async def _start(hass: HomeAssistant, accessory_type: str):
    result = await _menu(hass)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": accessory_type}
    )


def _sprinkler_device(hass: HomeAssistant) -> str:
    """A controller device with two water valves and a diagnostic sensor."""
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("test", "controller")},
        name="Backyard Controller",
    )
    registry = er.async_get(hass)
    for zone in ("front", "back"):
        registry.async_get_or_create(
            "valve",
            "test",
            zone,
            suggested_object_id=zone,
            device_id=device.id,
            original_device_class="water",
        )
    registry.async_get_or_create(
        "sensor",
        "test",
        "rssi",
        device_id=device.id,
        entity_category=er.EntityCategory.DIAGNOSTIC,
    )
    return device.id


async def test_menu_lists_every_type(hass: HomeAssistant) -> None:
    """All accessory types are offered."""
    result = await _menu(hass)
    assert result["menu_options"] == [
        "irrigation",
        "ceiling_fan",
        "buttons",
        "multi_sensor",
        "air_quality",
        "air_purifier",
        "power_strip",
        "faucet",
    ]


async def test_irrigation_from_device(hass: HomeAssistant) -> None:
    """Picking a device fills in its valves, its name and per-zone run times."""
    device_id = _sprinkler_device(hass)
    result = await _start(hass, "irrigation")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "irrigation"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device": device_id, "connection": CONNECTION}
    )
    assert result["step_id"] == "entities"
    valves = next(k for k in result["data_schema"].schema if k == "valves")
    assert valves.description["suggested_value"] == ["valve.front", "valve.back"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"valves": ["valve.front", "valve.back"], "default_duration": 600},
    )
    assert result["step_id"] == "run_times"
    labels = [str(k) for k in result["data_schema"].schema]
    assert len(labels) == 2

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {labels[0]: 300, labels[1]: 1200}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Backyard Controller"
    assert result["data"] == {
        "accessory_type": "irrigation",
        "port": 51828,
        "pin": PIN,
        "valves": ["valve.front", "valve.back"],
        "default_duration": 600,
        "one_at_a_time": True,
        "master": None,
        "run_times": {"valve.front": 300, "valve.back": 1200},
    }


async def test_ceiling_fan_without_device(hass: HomeAssistant) -> None:
    """Without a device, entities are chosen by hand and the name defaults."""
    result = await _start(hass, "ceiling_fan")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"connection": {"port": 51830, "pin": PIN}}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"fan": "fan.living", "light": "light.living"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ceiling Fan"
    assert result["data"]["fan"] == "fan.living"


async def test_connection_errors(hass: HomeAssistant, mock_hap_network) -> None:
    """Bad pins, ports claimed by other HAP integrations and busy ports fail."""
    MockConfigEntry(domain="homekit_irrigation", data={"port": 51828}).add_to_hass(hass)
    result = await _start(hass, "irrigation")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"connection": {"port": 51828, "pin": "123-45-678"}},
    )
    assert result["errors"] == {"pin": "invalid_pin", "port": "port_in_use"}

    mock_hap_network["port_available"].return_value = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"connection": {"port": 51900, "pin": PIN}}
    )
    assert result["errors"] == {"port": "port_busy"}


async def test_entity_errors(hass: HomeAssistant) -> None:
    """Each type rejects an empty selection."""
    for accessory_type, user_input, error in (
        (
            "irrigation",
            {"valves": [], "default_duration": 60},
            {"valves": "no_valves_selected"},
        ),
        ("buttons", {"events": []}, {"events": "no_buttons_selected"}),
        ("power_strip", {"outlets": []}, {"outlets": "no_outlets_selected"}),
        (
            "multi_sensor",
            {"battery_sensor": "sensor.b"},
            {"base": "no_sensors_selected"},
        ),
        (
            "air_quality",
            {"temperature_sensor": "sensor.t"},
            {"base": "no_pollutants_selected"},
        ),
    ):
        result = await _start(hass, accessory_type)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"connection": {"port": 52000, "pin": PIN}}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
        assert result["errors"] == error, accessory_type


async def test_suggested_port_skips_used(hass: HomeAssistant, mock_hap_network) -> None:
    """The suggested port avoids configured and busy ports."""
    MockConfigEntry(
        domain=DOMAIN, data={"accessory_type": "irrigation", "port": 51828}
    ).add_to_hass(hass)
    mock_hap_network["port_available"].side_effect = lambda port: port != 51829
    result = await _start(hass, "air_purifier")
    section = result["data_schema"].schema["connection"]
    port = next(k for k in section.schema.schema if k == "port")
    assert port.description["suggested_value"] == 51830


async def test_options_menu(hass: HomeAssistant) -> None:
    """Zone types get a run times option; others don't."""
    hass.states.async_set("valve.front", "closed")
    irrigation = await setup_accessory(
        hass, "irrigation", valves=["valve.front"], default_duration=600
    )
    result = await hass.config_entries.options.async_init(irrigation.entry_id)
    assert result["menu_options"] == ["entities", "run_times", "connection", "pairing"]

    strip = await setup_accessory(hass, "power_strip", port=51829, outlets=["switch.a"])
    result = await hass.config_entries.options.async_init(strip.entry_id)
    assert result["menu_options"] == ["entities", "connection", "pairing"]


async def test_options_run_times_apply_without_reload(hass: HomeAssistant) -> None:
    """Changing run times updates the running accessory in place."""
    hass.states.async_set("valve.front", "closed", {"friendly_name": "Front"})
    entry = await setup_accessory(
        hass, "irrigation", valves=["valve.front"], default_duration=600
    )
    accessory = server(hass, entry.entry_id).accessory
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "run_times"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"Front": 90}
    )
    await hass.async_block_till_done()
    assert entry.options["run_times"] == {"valve.front": 90}
    assert server(hass, entry.entry_id).accessory is accessory  # not reloaded
    assert accessory._zones["valve.front"].duration == 90
    assert accessory._zones["valve.front"].set_duration.value == 90


async def test_options_entities_and_connection(hass: HomeAssistant) -> None:
    """Entities and connection changes are saved and re-publish the accessory."""
    entry = await setup_accessory(
        hass,
        "irrigation",
        # Format stored by the old standalone homekit-irrigation integration.
        valves=[{"entity_id": "valve.front", "name": "Front"}],
        default_duration=900,
    )
    old = server(hass, entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "entities"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"valves": ["valve.front", "valve.side"], "default_duration": 300},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["valves"] == ["valve.front", "valve.side"]
    assert server(hass, entry.entry_id) is not old

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection"}
    )
    # The entry's own current port is fine even though its server holds it.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"port": 51828, "pin": "246-13-579"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["pin"] == "246-13-579"


async def test_options_reset_pairing(hass: HomeAssistant) -> None:
    """Resetting pairing deletes stored pairings and restarts the accessory."""
    entry = await setup_accessory(hass, "power_strip", outlets=["switch.a"])
    first = server(hass, entry.entry_id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "pairing"}
    )
    assert "Add Accessory" in result["description_placeholders"]["status"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"reset_pairing": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert server(hass, entry.entry_id) is not first


def test_pins() -> None:
    """Generated pins are always valid; spec-forbidden ones are not."""
    assert all(validate_pin(generate_pin()) for _ in range(200))
    for bad in ("111-11-111", "123-45-678", "876-54-321", "12345678", "123-456-78"):
        assert not validate_pin(bad)
