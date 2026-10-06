"""Run the real HAP server and serialize accessories the way iOS sees them."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from pyhap.accessory_driver import AccessoryDriver
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homekit_extended.const import DOMAIN
from homeassistant.components.fan import FanEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant

from .conftest import server


async def _entry(hass: HomeAssistant, accessory_type: str, port: int, **data):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=accessory_type,
        data={
            "accessory_type": accessory_type,
            "port": port,
            "pin": "031-45-154",
            **data,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_accessories_serialize(hass: HomeAssistant) -> None:
    """Every characteristic value fits its HAP format and range."""
    hass.states.async_set(
        "fan.p",
        "on",
        {ATTR_SUPPORTED_FEATURES: FanEntityFeature.SET_SPEED, "percentage": 50},
    )
    hass.states.async_set("sensor.pm", "900")
    hass.states.async_set("valve.a", "open")
    purifier = await _entry(
        hass, "air_purifier", 51829, fan="fan.p", pm25_sensor="sensor.pm"
    )
    irrigation = await _entry(
        hass, "irrigation", 51828, valves=["valve.a"], default_duration=3600
    )
    for entry in (purifier, irrigation):
        hap = server(hass, entry.entry_id).accessory.to_HAP()
        for service in hap["services"]:
            for char in service["characteristics"]:
                if "minValue" in char and "value" in char:
                    assert char["minValue"] <= char["value"] <= char["maxValue"], char


async def test_real_server_listens(hass: HomeAssistant, socket_enabled) -> None:
    """With sockets allowed, the HAP server really binds its port."""
    hass.states.async_set("valve.a", "closed")

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Real",
        data={
            "accessory_type": "irrigation",
            "port": 0,
            "pin": "031-45-154",
            "valves": ["valve.a"],
            "default_duration": 60,
        },
    )
    entry.add_to_hass(hass)
    srv_holder = {}

    async def real_start(self):
        # Only the HTTP server; mDNS is mocked by the test harness.
        await self.http_server.async_start(self.loop)
        srv_holder["server"] = self.http_server

    async def real_stop(self):
        self.http_server.async_stop()

    with (
        patch.object(AccessoryDriver, "async_start", real_start),
        patch.object(AccessoryDriver, "async_stop", real_stop),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        http = srv_holder["server"]
        port = http.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /accessories HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), 5)
        writer.close()
        # Unpaired clients are refused, which proves the HAP stack answered.
        assert status.startswith((b"HTTP/1.1 401", b"HTTP/1.1 470")), status
        assert await hass.config_entries.async_unload(entry.entry_id)
