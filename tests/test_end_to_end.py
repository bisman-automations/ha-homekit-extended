"""End to end over the network: real HAP servers and a real HAP client.

Core HomeKit Bridge and HomeKit Extended run their actual HAP servers on
localhost, and aiohomekit (the library behind Home Assistant's HomeKit Device
integration) pairs with them the way Apple Home would. This guards the hook
into core HomeKit, which the other tests reach with the network mocked out.
"""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import MagicMock, patch

from aiohomekit.controller.ip.discovery import IpDiscovery
from aiohomekit.exceptions import AuthenticationError
from aiohomekit.model.categories import Categories
from aiohomekit.model.feature_flags import FeatureFlags
from aiohomekit.model.status_flags import StatusFlags
from aiohomekit.zeroconf import HomeKitService
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)
import pytest_socket

from custom_components.homekit_extended.const import DOMAIN
from homeassistant.core import HomeAssistant

from .conftest import PIN, server

IRRIGATION = "000000CF-0000-1000-8000-0026BB765291"
VALVE = "000000D0-0000-1000-8000-0026BB765291"
OUTLET = "00000047-0000-1000-8000-0026BB765291"
PROTOCOL = "000000A2-0000-1000-8000-0026BB765291"
ACTIVE = "000000B0-0000-1000-8000-0026BB765291"
NAME = "00000023-0000-1000-8000-0026BB765291"


@pytest.fixture
def mock_hap_network(mock_async_zeroconf: MagicMock):
    """Override conftest's mocks so the real HAP servers start."""
    with patch(
        "custom_components.homekit_extended.config_flow.port_available",
        return_value=True,
    ):
        yield {}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _pair(hass, driver, port: int, pin: str):
    from aiohomekit.characteristic_cache import CharacteristicCacheMemory

    controller = MagicMock(pairings={}, _char_cache=CharacteristicCacheMemory())
    description = HomeKitService(
        name="x",
        id=driver.state.mac,
        model="x",
        feature_flags=FeatureFlags(0),
        status_flags=StatusFlags.UNPAIRED,
        config_num=driver.state.config_version,
        state_num=1,
        category=Categories.BRIDGE,
        protocol_version="1.1",
        type="_hap._tcp.local.",
        address="127.0.0.1",
        addresses=["127.0.0.1"],
        port=port,
    )
    # Pair setup between aiohomekit and HAP-python fails at random at step 4
    # (roughly 1 pairing in 80 in testing), independent of this integration;
    # retry rather than let it fail CI.
    for attempt in range(3):
        discovery = IpDiscovery(controller, description)
        try:
            finish = await discovery.async_start_pairing("test")
            return await finish(pin)
        except AuthenticationError:
            await discovery.close()
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def _by_type(accessory, service_type):
    return [
        s for s in accessory["services"] if s["type"].upper() == service_type.upper()
    ]


async def test_end_to_end(hass: HomeAssistant, mock_async_zeroconf) -> None:
    """Pair, list, control and restart, as Apple Home would see it."""
    pytest_socket.enable_socket()
    bridge_port, desk_port = _free_port(), _free_port()
    for entity_id, name in (
        ("valve.front", "Front Lawn"),
        ("valve.back", "Back Lawn"),
        ("light.porch", "Porch"),
        ("switch.a", "Lamp"),
        ("switch.b", "Fan"),
    ):
        hass.states.async_set(entity_id, "closed", {"friendly_name": name})

    core = MockConfigEntry(
        domain="homekit",
        title="HASS Bridge",
        data={"name": "HASS Bridge", "port": bridge_port},
        options={
            "mode": "bridge",
            "filter": {
                "include_domains": [],
                "include_entities": ["light.porch"],
                "exclude_domains": [],
                "exclude_entities": [],
            },
        },
    )
    core.add_to_hass(hass)
    with patch("homeassistant.components.homekit.async_show_setup_message"):
        assert await hass.config_entries.async_setup(core.entry_id)
        await hass.async_block_till_done()
    homekit = core.runtime_data.homekit
    for _ in range(50):
        if homekit.status == 1:
            break
        await asyncio.sleep(0.05)
    assert homekit.status == 1, homekit.status
    core_driver = homekit.driver
    config_before = core_driver.state.config_version

    bridged = MockConfigEntry(
        domain=DOMAIN,
        title="Sprinklers",
        data={
            "accessory_type": "irrigation",
            "bridge": core.entry_id,
            "valves": ["valve.front", "valve.back"],
        },
    )
    bridged.add_to_hass(hass)
    assert await hass.config_entries.async_setup(bridged.entry_id)
    await hass.async_block_till_done()
    ours = server(hass, bridged.entry_id)
    assert ours.attached
    assert core_driver.state.config_version > config_before

    pairing = await _pair(
        hass, core_driver, bridge_port, core_driver.state.pincode.decode()
    )
    accessories = await pairing.list_accessories_and_characteristics()
    mine = next(a for a in accessories if a["aid"] == ours.accessory.aid)
    assert len(_by_type(mine, IRRIGATION)) == 1
    valves = _by_type(mine, VALVE)
    assert [
        next(c["value"] for c in v["characteristics"] if c["type"].upper() == NAME)
        for v in valves
    ] == ["Front Lawn", "Back Lawn"]

    open_calls = async_mock_service(hass, "valve", "open_valve")
    active = next(
        c for c in valves[0]["characteristics"] if c["type"].upper() == ACTIVE
    )
    await pairing.put_characteristics([(mine["aid"], active["iid"], 1)])
    await hass.async_block_till_done()
    assert open_calls and open_calls[0].data["entity_id"] == "valve.front"

    # Valves unavailable -> No Response for our accessory only.
    hass.states.async_set("valve.front", "unavailable")
    hass.states.async_set("valve.back", "unavailable")
    read = await pairing.get_characteristics([(mine["aid"], active["iid"])])
    assert read[(mine["aid"], active["iid"])].get("status") == -70402

    # Core bridge restarts (reload): ours is there again with the same AID.
    hass.states.async_set("valve.front", "closed")
    await pairing.close()
    with patch("homeassistant.components.homekit.async_show_setup_message"):
        assert await hass.config_entries.async_reload(core.entry_id)
        await hass.async_block_till_done()
    homekit = core.runtime_data.homekit
    for _ in range(50):
        if homekit.status == 1:
            break
        await asyncio.sleep(0.05)
    from aiohomekit.controller.ip.pairing import IpPairing

    pairing = IpPairing(pairing.controller, pairing.pairing_data)
    accessories = await pairing.list_accessories_and_characteristics()
    again = next(a for a in accessories if a["aid"] == mine["aid"])
    assert [s["iid"] for s in again["services"]] == [s["iid"] for s in mine["services"]]

    # Leaving the bridge removes it for Apple Home.
    await hass.config_entries.async_unload(bridged.entry_id)
    accessories = await pairing.list_accessories_and_characteristics()
    assert all(a["aid"] != mine["aid"] for a in accessories)
    await pairing.close()

    # Standalone accessory: pairs on its own port, no protocol service.
    standalone = MockConfigEntry(
        domain=DOMAIN,
        title="Desk",
        data={
            "accessory_type": "power_strip",
            "port": desk_port,
            "pin": PIN,
            "outlets": ["switch.a", "switch.b"],
        },
    )
    standalone.add_to_hass(hass)
    assert await hass.config_entries.async_setup(standalone.entry_id)
    await hass.async_block_till_done()
    desk = server(hass, standalone.entry_id)
    pairing = await _pair(hass, desk.driver, desk_port, PIN)
    accessories = await pairing.list_accessories_and_characteristics()
    assert [a["aid"] for a in accessories] == [1]
    assert len(_by_type(accessories[0], OUTLET)) == 2
    assert not _by_type(accessories[0], PROTOCOL)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.desk_paired").state == "on"
    await pairing.close()

    await hass.config_entries.async_unload(standalone.entry_id)

    # Irrigation with zones as separate valves: one accessory, numbered valves.
    zones_port = _free_port()
    hass.states.async_set("valve.front", "closed", {"friendly_name": "Front Lawn"})
    hass.states.async_set("valve.back", "closed", {"friendly_name": "Back Lawn"})
    zones = MockConfigEntry(
        domain=DOMAIN,
        title="Yard",
        data={
            "accessory_type": "irrigation",
            "port": zones_port,
            "pin": PIN,
            "valves": ["valve.front", "valve.back"],
            "separate_zones": True,
        },
    )
    zones.add_to_hass(hass)
    assert await hass.config_entries.async_setup(zones.entry_id)
    await hass.async_block_till_done()
    pairing = await _pair(hass, server(hass, zones.entry_id).driver, zones_port, PIN)
    accessories = await pairing.list_accessories_and_characteristics()
    assert [a["aid"] for a in accessories] == [1]
    assert not _by_type(accessories[0], IRRIGATION)
    valves = _by_type(accessories[0], VALVE)
    assert len(valves) == 2
    assert [v.get("primary", False) for v in valves] == [True, False]
    await pairing.close()
    await hass.config_entries.async_unload(zones.entry_id)
    await hass.config_entries.async_unload(core.entry_id)
    await hass.async_block_till_done()
