"""Fixtures for HomeKit Extended tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homekit_extended.const import DOMAIN
from homeassistant.core import HomeAssistant

PIN = "031-45-154"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture(autouse=True)
def mock_hap_network(mock_async_zeroconf: MagicMock) -> Generator[dict[str, Any]]:
    """Keep HAP-python from binding sockets or advertising."""
    with (
        patch(
            "custom_components.homekit_extended.driver.ExtendedDriver.async_start",
            new_callable=AsyncMock,
        ) as start,
        patch(
            "custom_components.homekit_extended.driver.ExtendedDriver.async_stop",
            new_callable=AsyncMock,
        ) as stop,
        patch(
            "custom_components.homekit_extended.config_flow.port_available",
            return_value=True,
        ) as port_available,
    ):
        yield {"start": start, "stop": stop, "port_available": port_available}


async def setup_accessory(
    hass: HomeAssistant,
    accessory_type: str,
    title: str = "Test",
    port: int = 51828,
    **data: Any,
) -> MockConfigEntry:
    """Create and set up an entry for an accessory type."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={"accessory_type": accessory_type, "port": port, "pin": PIN, **data},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def server(hass: HomeAssistant, entry_id: str):
    """Return the running server for an entry."""
    return hass.config_entries.async_get_entry(entry_id).runtime_data


def services_by_type(accessory) -> dict[str, list]:
    """Group an accessory's HAP services by type name."""
    grouped: dict[str, list] = {}
    for service in accessory.services:
        grouped.setdefault(service.display_name, []).append(service)
    return grouped


def char(service, name):
    """Return a characteristic of a service."""
    return service.get_characteristic(name)
