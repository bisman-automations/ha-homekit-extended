"""One valve as its own accessory, and zones under a controller.

A valve can be published on its own port and pairing code, whether it is the
only valve or one zone of a controller. An irrigation entry can also publish
every zone as its own accessory inside that entry's bridge, which is still
one port and one pairing code.
"""

from __future__ import annotations

from pyhap.accessory import Bridge
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.homekit_extended.accessories.base import device_entities
from custom_components.homekit_extended.const import DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from .conftest import PIN, char, server, services_by_type
from .test_controller_links import _rain_bird

OTHER_PIN = "246-13-579"
ZONE_VALVES = ["valve.zone_1_valve", "valve.zone_2_valve"]


async def _publish(
    hass: HomeAssistant,
    title: str,
    port: int,
    pin: str,
    valves: list[str],
    **extra,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={
            "accessory_type": "irrigation",
            "port": port,
            "pin": pin,
            "valves": valves,
            "default_duration": 600,
            **extra,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_one_valve_has_its_own_port_and_pairing(hass: HomeAssistant) -> None:
    """A single valve is one standalone accessory, not a bridge."""
    hass.states.async_set("valve.front", "closed", {"friendly_name": "Front Lawn"})
    entry = await _publish(hass, "Front", 51840, PIN, ["valve.front"])
    srv = server(hass, entry.entry_id)
    assert srv.port == 51840
    assert srv.pin == PIN
    assert srv.published is srv.accessory
    assert not isinstance(srv.published, Bridge)
    assert srv.accessory.aid == 1
    assert list(srv.accessory._zones) == ["valve.front"]
    assert len(services_by_type(srv.accessory)["Valve"]) == 1

    opens = async_mock_service(hass, "valve", "open_valve")
    srv.accessory._set_zone_active("valve.front", 1)
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in opens] == ["valve.front"]


async def test_one_faucet_valve_is_standalone(hass: HomeAssistant) -> None:
    """A shower with one outlet is the same kind of standalone accessory."""
    hass.states.async_set("valve.shower", "closed", {"friendly_name": "Shower"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Shower",
        data={
            "accessory_type": "faucet",
            "port": 51844,
            "pin": PIN,
            "valves": ["valve.shower"],
            "valve_type": "shower_head",
            "default_duration": 0,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    srv = server(hass, entry.entry_id)
    assert srv.port == 51844
    assert srv.pin == PIN
    assert not isinstance(srv.published, Bridge)
    assert [
        char(v, "Name").value for v in services_by_type(srv.accessory)["Valve"]
    ] == ["Shower"]


async def test_each_zone_under_a_controller_is_its_own_accessory(
    hass: HomeAssistant,
) -> None:
    """Picking one zone device publishes only that valve, on its own pairing."""
    controller_id = _rain_bird(hass)
    devices = dr.async_get(hass)
    zones = [
        device
        for device in dr.async_entries_for_config_entry(
            devices, devices.async_get(controller_id).config_entry_id
        )
        if device.via_device_id == controller_id
    ]
    assert len(zones) == 2
    front_device = next(device for device in zones if device.name == "Front Lawn")
    assert {device.name for device in zones} == {"Front Lawn", "Back Lawn"}
    assert {e.entity_id for e in device_entities(hass, front_device.id)} >= {
        "valve.zone_1_valve"
    }
    assert "valve.zone_2_valve" not in {
        e.entity_id for e in device_entities(hass, front_device.id)
    }

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "irrigation"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"device": front_device.id, "connection": {"port": 51841, "pin": PIN}},
    )
    suggested = next(k for k in result["data_schema"].schema if k == "valves")
    assert suggested.description["suggested_value"] == ["valve.zone_1_valve"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"valves": ["valve.zone_1_valve"], "default_duration": 600},
    )
    async_mock_service(hass, "number", "set_value")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "run_times"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {str(key): 600 for key in result["data_schema"].schema}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["port"] == 51841
    assert result["data"]["pin"] == PIN
    assert result["data"]["valves"] == ["valve.zone_1_valve"]
    await hass.async_block_till_done()

    back = await _publish(hass, "Back Lawn", 51842, OTHER_PIN, ["valve.zone_2_valve"])
    front = server(hass, result["result"].entry_id)
    back_server = server(hass, back.entry_id)
    assert front.port == 51841 and back_server.port == 51842
    assert front.pin == PIN and back_server.pin == OTHER_PIN
    assert front.published is front.accessory
    assert back_server.published is back_server.accessory
    assert list(front.accessory._zones) == ["valve.zone_1_valve"]
    assert list(back_server.accessory._zones) == ["valve.zone_2_valve"]

    opens = async_mock_service(hass, "valve", "open_valve")
    front.accessory._set_zone_active("valve.zone_1_valve", 1)
    back_server.accessory._set_zone_active("valve.zone_2_valve", 1)
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in opens] == ZONE_VALVES


async def test_controller_zones_can_share_one_bridge(hass: HomeAssistant) -> None:
    """The separate-accessories layout is one pairing, with one accessory per zone."""
    _rain_bird(hass)
    entry = await _publish(
        hass,
        "Yard",
        51850,
        PIN,
        ZONE_VALVES,
        zone_layout="accessories",
    )
    srv = server(hass, entry.entry_id)
    assert srv.port == 51850
    assert srv.pin == PIN
    assert isinstance(srv.published, Bridge)
    zones = list(srv.published.accessories.values())
    assert sorted(zone.entity_id for zone in zones) == ZONE_VALVES
    assert len({zone.aid for zone in zones}) == 2
    assert all(zone.aid != 1 for zone in zones)
    # Still the controller's valves: opening one calls that valve.
    opens = async_mock_service(hass, "valve", "open_valve")
    front = next(zone for zone in zones if zone.entity_id == "valve.zone_1_valve")
    char(services_by_type(front)["Valve"][0], "Active").client_update_value(1)
    await hass.async_block_till_done()
    assert [call.data["entity_id"] for call in opens] == ["valve.zone_1_valve"]
