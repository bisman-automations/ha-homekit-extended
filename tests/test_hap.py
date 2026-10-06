"""Run the real HAP server and serialize accessories the way iOS sees them."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from custom_components.homekit_extended.driver import ExtendedDriver
from homeassistant.components.fan import FanEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant

from .conftest import server, setup_accessory


async def test_every_type_serializes(hass: HomeAssistant) -> None:
    """Every accessory type builds, and every value fits its HAP format and range."""
    hass.states.async_set(
        "fan.f",
        "on",
        {ATTR_SUPPORTED_FEATURES: FanEntityFeature.SET_SPEED, "percentage": 50},
    )
    hass.states.async_set(
        "light.l", "on", {"supported_color_modes": ["brightness"], "brightness": 255}
    )
    hass.states.async_set("sensor.pm", "9000")
    hass.states.async_set("sensor.co2", "250000")
    hass.states.async_set("sensor.lux", "500000")
    hass.states.async_set("sensor.battery", "140")
    hass.states.async_set("sensor.temp", "-400")
    hass.states.async_set("valve.a", "open")
    hass.states.async_set("event.b", "2026-10-06T10:00:00", {"event_types": ["single"]})
    hass.states.async_set("switch.s", "on")
    configs = [
        (
            "air_purifier",
            {
                "fan": "fan.f",
                "pm25_sensor": "sensor.pm",
                "temperature_sensor": "sensor.temp",
            },
        ),
        ("irrigation", {"valves": ["valve.a"], "default_duration": 3600}),
        (
            "faucet",
            {"valves": ["valve.a"], "valve_type": "faucet", "default_duration": 0},
        ),
        ("ceiling_fan", {"fan": "fan.f", "light": "light.l"}),
        ("buttons", {"events": ["event.b"]}),
        (
            "multi_sensor",
            {
                "illuminance_sensor": "sensor.lux",
                "battery_sensor": "sensor.battery",
                "temperature_sensor": "sensor.temp",
            },
        ),
        ("air_quality", {"pm25_sensor": "sensor.pm", "co2_sensor": "sensor.co2"}),
        ("power_strip", {"outlets": ["switch.s"]}),
    ]
    for port, (accessory_type, data) in enumerate(configs, start=51828):
        entry = await setup_accessory(
            hass, accessory_type, accessory_type, port, **data
        )
        hap = server(hass, entry.entry_id).accessory.to_HAP()
        for service in hap["services"]:
            for char in service["characteristics"]:
                if "minValue" in char and char.get("value") is not None:
                    assert char["minValue"] <= char["value"] <= char["maxValue"], (
                        accessory_type,
                        char,
                    )


async def test_real_server_listens(hass: HomeAssistant, socket_enabled) -> None:
    """With sockets allowed, the HAP server really binds its port."""
    hass.states.async_set("valve.a", "closed")
    holder = {}

    async def real_start(self):
        # Only the HTTP server; mDNS is mocked by the test harness.
        await self.http_server.async_start(self.loop)
        holder["server"] = self.http_server

    async def real_stop(self):
        self.http_server.async_stop()

    with (
        patch.object(ExtendedDriver, "async_start", real_start),
        patch.object(ExtendedDriver, "async_stop", real_stop),
    ):
        entry = await setup_accessory(
            hass, "irrigation", "Real", 0, valves=["valve.a"], default_duration=60
        )
        port = holder["server"].server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /accessories HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), 5)
        writer.close()
        # Unpaired clients are refused, which proves the HAP stack answered.
        assert status.startswith((b"HTTP/1.1 401", b"HTTP/1.1 470")), status
        assert await hass.config_entries.async_unload(entry.entry_id)
