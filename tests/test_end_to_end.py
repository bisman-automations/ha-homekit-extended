"""End to end over the network: a real HAP server and a real HAP client.

HomeKit Extended runs its HAP server on localhost, and aiohomekit (the library
behind Home Assistant's HomeKit Device integration) pairs with it the way
Apple Home would.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

from aiohomekit.controller.ip.discovery import IpDiscovery
from aiohomekit.exceptions import AuthenticationError
from aiohomekit.model.categories import Categories
from aiohomekit.model.feature_flags import FeatureFlags
from aiohomekit.model.status_flags import StatusFlags
from aiohomekit.zeroconf import HomeKitService
from pyhap.const import CATEGORY_BRIDGE
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
    """Pair, list and control a standalone accessory, as Apple Home would."""
    pytest_socket.enable_socket()
    desk_port = _free_port()
    for entity_id, name in (
        ("valve.front", "Front Lawn"),
        ("valve.back", "Back Lawn"),
        ("switch.a", "Lamp"),
        ("switch.b", "Fan"),
    ):
        hass.states.async_set(entity_id, "closed", {"friendly_name": name})

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

    # Zones as separate accessories: our own bridge, one accessory per zone.
    yard_port = _free_port()
    yard = MockConfigEntry(
        domain=DOMAIN,
        title="Yard Bridge",
        data={
            "accessory_type": "irrigation",
            "port": yard_port,
            "pin": PIN,
            "valves": ["valve.front", "valve.back"],
            "zone_layout": "accessories",
        },
    )
    yard.add_to_hass(hass)
    assert await hass.config_entries.async_setup(yard.entry_id)
    await hass.async_block_till_done()
    yard_server = server(hass, yard.entry_id)
    assert yard_server.published.category == CATEGORY_BRIDGE  # QR says bridge
    pairing = await _pair(hass, yard_server.driver, yard_port, PIN)
    accessories = await pairing.list_accessories_and_characteristics()
    assert accessories[0]["aid"] == 1  # the bridge
    zone_accessories = accessories[1:]
    assert len(zone_accessories) == 2
    for zone in zone_accessories:
        (valve,) = _by_type(zone, VALVE)
        assert valve.get("primary") is True
    open_calls = async_mock_service(hass, "valve", "open_valve")
    by_name = {
        next(
            c["value"] for c in valve["characteristics"] if c["type"].upper() == NAME
        ): (
            zone["aid"],
            valve,
        )
        for zone in zone_accessories
        for valve in _by_type(zone, VALVE)
    }
    assert set(by_name) == {"Front Lawn", "Back Lawn"}
    aid, back = by_name["Back Lawn"]
    active = next(c for c in back["characteristics"] if c["type"].upper() == ACTIVE)
    await pairing.put_characteristics([(aid, active["iid"], 1)])
    await hass.async_block_till_done()
    assert open_calls[-1].data["entity_id"] == "valve.back"
    await pairing.close()
    await hass.config_entries.async_unload(yard.entry_id)
    await hass.async_block_till_done()
