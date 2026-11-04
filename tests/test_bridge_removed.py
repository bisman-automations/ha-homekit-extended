"""Leftover "add to a HomeKit Bridge" entries are refused, not converted.

These tests start a real core HomeKit Bridge only to prove HomeKit Extended
does not patch it, write its accessory IDs, or change its accessories.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homekit_extended.const import DOMAIN
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from .conftest import PIN, server, setup_accessory

VALVES = ["valve.front", "valve.back"]


@pytest.fixture
def mock_core_driver() -> Generator[dict[str, AsyncMock]]:
    """Keep core HomeKit's driver off the network, and record its writes."""
    with (
        patch(
            "homeassistant.components.homekit.HomeDriver.async_start",
            new_callable=AsyncMock,
        ),
        patch(
            "homeassistant.components.homekit.HomeDriver.async_stop",
            new_callable=AsyncMock,
        ),
        patch(
            "homeassistant.components.homekit.HomeDriver.async_persist",
        ) as persist,
        patch(
            "homeassistant.components.homekit.HomeDriver.async_update_advertisement",
        ) as advertise,
        patch("homeassistant.components.homekit.async_show_setup_message"),
        patch(
            "homeassistant.components.homekit.async_port_is_available",
            return_value=True,
        ),
    ):
        yield {"persist": persist, "advertise": advertise}


def _valves(hass: HomeAssistant) -> None:
    for entity_id in (*VALVES, "light.porch"):
        hass.states.async_set(entity_id, "closed", {"friendly_name": entity_id})


async def _core_bridge(hass: HomeAssistant) -> MockConfigEntry:
    """Set up a core HomeKit Bridge publishing one light."""
    entry = MockConfigEntry(
        domain="homekit",
        title="HASS Bridge",
        data={"name": "HASS Bridge", "port": 21064},
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
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _bridge_entry(hass: HomeAssistant, bridge_id: str, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sprinklers",
        data={
            "accessory_type": "irrigation",
            "bridge": bridge_id,
            "valves": VALVES,
            "port": 51828,
            "pin": PIN,
            **data,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_does_not_patch_core_homekit(hass: HomeAssistant) -> None:
    """Loading the integration leaves core HomeKit's bridge factory alone."""
    from homeassistant.components.homekit import HomeKit

    original = HomeKit._async_create_bridge_accessory
    assert await async_setup_component(hass, DOMAIN, {})
    assert HomeKit._async_create_bridge_accessory is original
    assert not getattr(original, "_homekit_extended_hook", False)


async def test_bridge_entry_fails_and_core_storage_is_unchanged(
    hass: HomeAssistant, mock_core_driver: dict[str, AsyncMock]
) -> None:
    """A bridge-mode entry raises a repair and does not edit the core bridge."""
    _valves(hass)
    core = await _core_bridge(hass)
    homekit = core.runtime_data.homekit
    bridge = homekit.bridge
    storage = homekit.aid_storage
    allocations = dict(storage.allocations)
    accessory_ids = set(bridge.accessories)
    config_version = homekit.driver.state.config_version
    mock_core_driver["persist"].reset_mock()
    mock_core_driver["advertise"].reset_mock()

    # A repair left over from 2.x should be replaced, not left beside the new one.
    entry = _bridge_entry(hass, core.entry_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"bridge_missing_{entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="bridge_missing",
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "bridge_removed"
    assert entry.error_reason_translation_placeholders == {"name": "Sprinklers"}
    assert "was not converted" in entry.reason
    assert not hasattr(entry, "runtime_data") or entry.runtime_data is None
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"bridge_removed_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_key == "bridge_removed"
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders["name"] == "Sprinklers"
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"bridge_missing_{entry.entry_id}")
        is None
    )

    assert dict(storage.allocations) == allocations
    assert set(bridge.accessories) == accessory_ids
    assert homekit.driver.state.config_version == config_version
    mock_core_driver["persist"].assert_not_called()
    mock_core_driver["advertise"].assert_not_called()

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert dict(storage.allocations) == allocations
    assert set(bridge.accessories) == accessory_ids
    assert homekit.driver.state.config_version == config_version
    mock_core_driver["persist"].assert_not_called()
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"bridge_removed_{entry.entry_id}")
        is None
    )


async def test_bridge_in_options_only_is_refused(hass: HomeAssistant) -> None:
    """2.x stored a move into a bridge in options. That still refuses setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desk",
        data={
            "accessory_type": "power_strip",
            "port": 51828,
            "pin": PIN,
            "outlets": ["switch.a"],
        },
        options={"bridge": "core-bridge-id"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "bridge_removed"


async def test_moved_to_standalone_before_removal_still_runs(
    hass: HomeAssistant,
) -> None:
    """An entry 2.x already moved out of a bridge (bridge: null) still publishes."""
    hass.states.async_set("switch.a", "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desk",
        data={
            "accessory_type": "power_strip",
            "port": 51828,
            "pin": PIN,
            "bridge": "core-bridge-id",
            "outlets": ["switch.a"],
        },
        options={"bridge": None},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert server(hass, entry.entry_id).port == 51828
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"bridge_removed_{entry.entry_id}")
        is None
    )


async def test_options_explain_bridge_removal_without_saving(
    hass: HomeAssistant,
) -> None:
    """Configure on a bridge-mode entry explains the removal and writes nothing."""
    entry = _bridge_entry(hass, "missing-bridge")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    options_before = dict(entry.options)
    data_before = dict(entry.data)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bridge_removed"
    assert result["description_placeholders"]["name"] == "Sprinklers"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "bridge_removed"
    assert dict(entry.options) == options_before
    assert dict(entry.data) == data_before
    assert entry.data["bridge"] == "missing-bridge"


async def test_add_flow_has_no_bridge_step(hass: HomeAssistant) -> None:
    """New accessories are not offered a HomeKit Bridge."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "power_strip"}
    )
    assert "bridge" not in result["data_schema"].schema
    assert "duplicates" not in result["data_schema"].schema


async def test_removing_a_bridge_entry_needs_no_core_bridge(
    hass: HomeAssistant,
) -> None:
    """Deleting the entry does not require core HomeKit to be running."""
    entry = _bridge_entry(hass, "missing-bridge")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not hass.config_entries.async_entries(DOMAIN)
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"bridge_removed_{entry.entry_id}")
        is None
    )


async def test_standalone_still_publishes(hass: HomeAssistant) -> None:
    """A normal entry is unchanged by the bridge-mode removal."""
    hass.states.async_set("switch.a", "on")
    entry = await setup_accessory(hass, "power_strip", "Desk", outlets=["switch.a"])
    assert entry.state is ConfigEntryState.LOADED
    assert server(hass, entry.entry_id).port == 51828
    assert hass.states.get("binary_sensor.desk_paired").attributes["port"] == 51828
