"""Pairing notification, QR code, Paired sensor, diagnostics and start failures."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from zeroconf import NonUniqueNameException, ServiceInfo

from custom_components.homekit_extended.const import DOMAIN
from custom_components.homekit_extended.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.homekit_extended.driver import PlainNameAdvertiser
from custom_components.homekit_extended.pairing import notification_id
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from .conftest import PIN, server, setup_accessory


async def test_pairing_notification_and_qr(hass: HomeAssistant, hass_client) -> None:
    """An unpaired accessory shows a notification whose QR code is served."""
    await async_setup_component(hass, "http", {})
    entry = await setup_accessory(hass, "power_strip", "Desk", outlets=["switch.a"])
    notifications = hass.data["persistent_notification"]
    note = notifications[notification_id(entry.entry_id)]
    assert PIN in note["message"]
    url = note["message"].split("](", 1)[1].rstrip(")")

    client = await hass_client()
    response = await client.get(url)
    assert response.status == 200
    assert response.content_type == "image/svg+xml"
    assert (await client.get(url.replace("token=", "token=x"))).status == 404

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert notification_id(entry.entry_id) not in notifications


async def test_paired_sensor_and_device(hass: HomeAssistant) -> None:
    """A device with a Paired sensor follows pairing changes."""
    entry = await setup_accessory(hass, "power_strip", "Desk", outlets=["switch.a"])
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    assert device.name == "Desk"
    assert device.model == "HomeKit Extended Power Strip"
    state = hass.states.get("binary_sensor.desk_paired")
    assert state.state == "off"
    assert state.attributes["port"] == 51828

    srv = server(hass, entry.entry_id)
    srv.driver.state.paired_clients = {"controller": b"key"}
    with patch("pyhap.accessory_driver.AccessoryDriver.finish_pair"):
        srv.driver.finish_pair()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.desk_paired").state == "on"
    assert notification_id(entry.entry_id) not in hass.data["persistent_notification"]


async def test_diagnostics_redacts_pin(hass: HomeAssistant) -> None:
    """Diagnostics include the accessory but not the pairing code."""
    entry = await setup_accessory(hass, "power_strip", "Desk", outlets=["switch.a"])
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"]["data"]["pin"] == "**REDACTED**"
    assert diagnostics["paired"] is False
    assert diagnostics["accessory"]["aid"] == 1


async def test_busy_port_retries_setup(hass: HomeAssistant, mock_hap_network) -> None:
    """A busy port leaves the entry retrying, with the real error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sprinklers",
        data={
            "accessory_type": "power_strip",
            "port": 51828,
            "pin": PIN,
            "outlets": ["switch.a"],
        },
    )
    entry.add_to_hass(hass)
    error = OSError(98, "error while attempting to bind on address: address in use")
    with patch(
        "custom_components.homekit_extended.driver.ExtendedDriver.async_start",
        AsyncMock(side_effect=error),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "51828" in entry.reason
    # The never-advertised driver must not be stopped (that used to crash).
    mock_hap_network["stop"].assert_not_awaited()


class FakeZeroconf:
    """Record registrations; reject names listed as taken."""

    def __init__(self, taken: set[str] = frozenset()) -> None:
        self.taken = taken
        self.calls: list[tuple[str, str]] = []

    async def async_register_service(self, info, **kwargs):
        self.calls.append(("register", info.name))

        async def _announce():
            if info.name in self.taken:
                raise NonUniqueNameException

        return asyncio.ensure_future(_announce())

    async def async_update_service(self, info, **kwargs):
        self.calls.append(("update", info.name))

    async def async_unregister_service(self, info, **kwargs):
        self.calls.append(("unregister", info.name))

    async def async_close(self):
        self.calls.append(("close", ""))


def _info(name: str) -> ServiceInfo:
    return ServiceInfo(
        "_hap._tcp.local.",
        f"{name}._hap._tcp.local.",
        port=51828,
        properties={"md": "x"},
        server="host.local.",
        parsed_addresses=["192.168.1.2"],
    )


async def test_plain_name_advertising() -> None:
    """The ID suffix is dropped consistently for register, update and unregister."""
    inner = FakeZeroconf()
    advertiser = PlainNameAdvertiser(inner)
    info = _info("Irrigation System 8D111D")
    await advertiser.async_register_service(info, cooperating_responders=True)
    await advertiser.async_update_service(info)
    await advertiser.async_unregister_service(info)
    await advertiser.async_close()
    plain = "Irrigation System._hap._tcp.local."
    assert inner.calls == [
        ("register", plain),
        ("update", plain),
        ("unregister", plain),
        ("close", ""),
    ]


async def test_plain_name_falls_back_on_conflict() -> None:
    """If the plain name is taken, pyhap's suffixed name is used throughout."""
    inner = FakeZeroconf(taken={"Irrigation System._hap._tcp.local."})
    advertiser = PlainNameAdvertiser(inner)
    info = _info("Irrigation System 8D111D")
    await advertiser.async_register_service(info)
    await advertiser.async_unregister_service(info)
    assert inner.calls[-2:] == [
        ("register", info.name),
        ("unregister", info.name),
    ]


async def test_plain_name_option_installs_advertiser(hass: HomeAssistant) -> None:
    """The option swaps in the plain-name advertiser."""
    entry = await setup_accessory(
        hass, "power_strip", "Desk", outlets=["switch.a"], plain_name=True
    )
    assert isinstance(
        server(hass, entry.entry_id).driver.advertiser, PlainNameAdvertiser
    )
    other = await setup_accessory(
        hass, "power_strip", "Desk 2", 51829, outlets=["switch.b"]
    )
    assert not isinstance(
        server(hass, other.entry_id).driver.advertiser, PlainNameAdvertiser
    )
