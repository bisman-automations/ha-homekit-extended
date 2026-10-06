"""Fixtures for HomeKit Extended tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture(autouse=True)
def mock_hap_network(mock_async_zeroconf: MagicMock) -> Generator[dict[str, AsyncMock]]:
    """Keep HAP-python from binding sockets or advertising."""
    with (
        patch(
            "custom_components.homekit_extended.driver.AccessoryDriver.async_start",
            new_callable=AsyncMock,
        ) as start,
        patch(
            "custom_components.homekit_extended.driver.AccessoryDriver.async_stop",
            new_callable=AsyncMock,
        ) as stop,
    ):
        yield {"start": start, "stop": stop}


def server(hass: HomeAssistant, entry_id: str):
    """Return the running server for an entry."""
    return hass.config_entries.async_get_entry(entry_id).runtime_data


def services_by_type(accessory) -> dict[str, list]:
    """Group an accessory's HAP services by type name."""
    grouped: dict[str, list] = {}
    for service in accessory.services:
        grouped.setdefault(service.display_name, []).append(service)
    return grouped
