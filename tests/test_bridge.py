"""Accessories inside a core HomeKit Bridge, and stable instance IDs."""

from __future__ import annotations

from collections.abc import Generator
import dataclasses
from unittest.mock import AsyncMock, Mock, patch

from pyhap.accessory import Accessory
from pyhap.iid_manager import IIDManager
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homekit_extended.accessories import ACCESSORY_TYPES
from custom_components.homekit_extended.bridge import aid_key
from custom_components.homekit_extended.const import DOMAIN
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir

from .conftest import PIN, server, setup_accessory

VALVES = ["valve.front", "valve.back"]


@pytest.fixture(autouse=True)
def mock_core_driver() -> Generator[None]:
    """Keep core HomeKit's driver off the network too."""
    with (
        patch(
            "homeassistant.components.homekit.HomeDriver.async_start",
            new_callable=AsyncMock,
        ),
        patch(
            "homeassistant.components.homekit.HomeDriver.async_stop",
            new_callable=AsyncMock,
        ),
        patch("homeassistant.components.homekit.HomeDriver.async_persist"),
        patch("homeassistant.components.homekit.HomeDriver.async_update_advertisement"),
        patch("homeassistant.components.homekit.async_show_setup_message"),
        patch(
            "homeassistant.components.homekit.async_port_is_available",
            return_value=True,
        ),
    ):
        yield


def _valves(hass: HomeAssistant, *entity_ids: str) -> None:
    for entity_id in entity_ids or (*VALVES, "light.porch"):
        hass.states.async_set(entity_id, "closed", {"friendly_name": entity_id})


async def _core_bridge(
    hass: HomeAssistant, include: list[str] | None = None, title: str = "HASS Bridge"
) -> MockConfigEntry:
    """Set up a core HomeKit Bridge publishing the given entities."""
    entry = MockConfigEntry(
        domain="homekit",
        title=title,
        data={"name": title, "port": 21064},
        options={
            "mode": "bridge",
            "filter": {
                "include_domains": [],
                "include_entities": include or ["light.porch"],
                "exclude_domains": [],
                "exclude_entities": [],
            },
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _bridge(hass: HomeAssistant, entry: MockConfigEntry):
    return entry.runtime_data.homekit.bridge


async def _bridged(
    hass: HomeAssistant, bridge: MockConfigEntry, **data
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sprinklers",
        data={
            "accessory_type": "irrigation",
            "bridge": bridge.entry_id,
            "valves": VALVES,
            **data,
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_joins_running_bridge(hass: HomeAssistant) -> None:
    """An accessory added to a running bridge appears in it with a stored AID."""
    _valves(hass)
    core = await _core_bridge(hass)
    driver = core.runtime_data.homekit.driver
    hash_before = driver.state.accessories_hash
    entry = await _bridged(hass, core)

    ours = server(hass, entry.entry_id)
    assert ours.attached
    aid = core.runtime_data.homekit.aid_storage.allocations[aid_key(entry.entry_id)]
    assert _bridge(hass, core).accessories[aid] is ours.accessory
    assert driver.state.accessories_hash != hash_before  # Apple Home is told

    paired = hass.states.get("binary_sensor.sprinklers_paired")
    assert paired.attributes["bridge"] == "HASS Bridge"
    assert "port" not in paired.attributes

    await hass.config_entries.async_unload(entry.entry_id)
    assert aid not in _bridge(hass, core).accessories


async def test_rejoins_after_bridge_reload(hass: HomeAssistant) -> None:
    """When core rebuilds the bridge, the accessory is added before it starts."""
    _valves(hass)
    core = await _core_bridge(hass)
    entry = await _bridged(hass, core)
    old = server(hass, entry.entry_id).accessory

    assert await hass.config_entries.async_reload(core.entry_id)
    await hass.async_block_till_done()

    ours = server(hass, entry.entry_id)
    assert ours.attached
    assert ours.accessory is not old
    assert ours.accessory.aid == old.aid
    assert ours.homekit is core.runtime_data.homekit
    assert not old._subscriptions  # the old copy stopped listening


async def test_waits_for_bridge_to_start(hass: HomeAssistant) -> None:
    """Set up before the bridge has started, it's added when the bridge builds."""
    _valves(hass)
    core = await _core_bridge(hass)
    await hass.config_entries.async_unload(core.entry_id)
    entry = await _bridged(hass, core)
    assert entry.state is ConfigEntryState.LOADED
    assert not server(hass, entry.entry_id).attached

    assert await hass.config_entries.async_setup(core.entry_id)
    await hass.async_block_till_done()
    assert server(hass, entry.entry_id).attached


async def test_duplicates_raise_repair(hass: HomeAssistant) -> None:
    """Entities the bridge also publishes itself raise a repair issue."""
    _valves(hass)
    core = await _core_bridge(hass, ["light.porch", "valve.front"])
    entry = await _bridged(hass, core)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"duplicate_entities_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders["entities"] == "`valve.front`"

    await hass.config_entries.async_unload(entry.entry_id)
    assert not ir.async_get(hass).async_get_issue(
        DOMAIN, f"duplicate_entities_{entry.entry_id}"
    )


async def test_missing_bridge(hass: HomeAssistant) -> None:
    """A deleted bridge fails setup with a repair explaining what to do."""
    _valves(hass)
    core = await _core_bridge(hass)
    entry = await _bridged(hass, core)
    assert entry.state is ConfigEntryState.LOADED

    await hass.config_entries.async_remove(core.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"bridge_missing_{entry.entry_id}"
    )


async def test_flow_adds_to_bridge(hass: HomeAssistant) -> None:
    """Picking a bridge skips the port and warns about duplicates."""
    _valves(hass)
    core = await _core_bridge(hass, ["valve.back"], title="Garden Bridge")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "irrigation"}
    )
    options = result["data_schema"].schema["bridge"].config["options"]
    assert [o["label"] for o in options] == ["Standalone", "Add to Garden Bridge"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Sprinklers",
            "bridge": core.entry_id,
            "connection": {"port": 51828, "pin": "bad"},  # ignored when bridged
        },
    )
    assert result["step_id"] == "entities"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"valves": VALVES, "default_duration": 600}
    )
    assert result["step_id"] == "duplicates"
    assert result["description_placeholders"]["entities"] == "`valve.back`"
    assert result["description_placeholders"]["bridge"] == "Garden Bridge"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "run_times"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {str(key): 600 for key in result["data_schema"].schema}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["bridge"] == core.entry_id
    assert "port" not in result["data"]
    await hass.async_block_till_done()
    assert server(hass, result["result"].entry_id).attached


async def test_options_switch_to_standalone(hass: HomeAssistant) -> None:
    """The connection step moves a bridged accessory to its own pairing."""
    _valves(hass)
    core = await _core_bridge(hass)
    entry = await _bridged(hass, core)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "pairing" not in result["menu_options"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"bridge": "standalone", "port": 51840, "pin": PIN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    ours = server(hass, entry.entry_id)
    assert not getattr(ours, "bridged", False)
    assert ours.port == 51840
    assert not any(
        acc.display_name == "Sprinklers"
        for acc in _bridge(hass, core).accessories.values()
    )


def _iids(accessory: Accessory) -> dict[str, int]:
    """Service and characteristic IIDs keyed by service name and type."""
    found = {}
    for service in accessory.services:
        label = service.unique_id or service.display_name
        found[label] = accessory.iid_manager.get_iid(service)
        for char in service.characteristics:
            found[f"{label}/{char.display_name}"] = accessory.iid_manager.get_iid(char)
    return found


LEGACY_TYPES = {
    "irrigation": {"valves": VALVES},
    "faucet": {"valves": VALVES},
    "power_strip": {"outlets": ["switch.a", "switch.b"]},
    "multi_sensor": {
        "motion_sensor": "binary_sensor.motion",
        "temperature_sensor": "sensor.temperature",
        "battery_sensor": "sensor.battery",
    },
    "ceiling_fan": {"fan": "fan.ceiling", "light": "light.ceiling"},
    "air_purifier": {"fan": "fan.purifier", "pm25_sensor": "sensor.pm25"},
}


@pytest.mark.parametrize(("accessory_type", "data"), LEGACY_TYPES.items())
async def test_first_run_keeps_pyhap_iids(
    hass: HomeAssistant, accessory_type: str, data: dict
) -> None:
    """Accessories paired before IIDs were stored keep the same IIDs.

    Before 2.0.0 accessories were created without an AID (the driver set it
    later) and with pyhap's own IID counter.
    """
    _valves(hass, *VALVES, "switch.a", "switch.b", "fan.ceiling", "light.ceiling")
    for entity_id in ("binary_sensor.motion", "sensor.temperature", "sensor.battery"):
        hass.states.async_set(entity_id, "1")
    hass.states.async_set("fan.purifier", "on")
    hass.states.async_set("sensor.pm25", "5")
    entry = await setup_accessory(hass, accessory_type, **data)
    stored = server(hass, entry.entry_id).accessory
    assert stored.get_service("HAPProtocolInformation") is None

    with patch(
        "custom_components.homekit_extended.accessories.base.HomeIIDManager",
        side_effect=lambda storage: IIDManager(),
    ):
        legacy = ACCESSORY_TYPES[accessory_type].factory(
            hass, stored.driver, entry, None
        )
    assert _iids(stored) == _iids(legacy)
    await legacy.async_stop()


async def test_iids_survive_new_zone(hass: HomeAssistant) -> None:
    """Adding a zone keeps the IIDs of every existing service."""
    _valves(hass, *VALVES, "valve.side")
    entry = await setup_accessory(hass, "irrigation", valves=VALVES)
    before = _iids(server(hass, entry.entry_id).accessory)

    hass.config_entries.async_update_entry(
        entry, options={"valves": ["valve.side", *VALVES]}
    )
    await hass.async_block_till_done()
    after = _iids(server(hass, entry.entry_id).accessory)
    assert {k: after[k] for k in before} == before
    assert "valve.side" in after


async def test_attach_failure_never_blocks_bridge(hass: HomeAssistant) -> None:
    """If adding ours fails while core builds the bridge, the bridge still starts."""
    from homeassistant.components.homekit import STATUS_RUNNING

    _valves(hass)
    core = await _core_bridge(hass)
    entry = await _bridged(hass, core)
    broken = dataclasses.replace(
        ACCESSORY_TYPES["irrigation"], factory=Mock(side_effect=RuntimeError("boom"))
    )
    with patch.object(server(hass, entry.entry_id), "accessory_type", broken):
        assert await hass.config_entries.async_reload(core.entry_id)
        await hass.async_block_till_done()
    assert core.runtime_data.homekit.status == STATUS_RUNNING
    assert not server(hass, entry.entry_id).attached
    assert not server(hass, entry.entry_id).paired


async def test_failed_entry_moves_to_standalone(hass: HomeAssistant) -> None:
    """Choosing standalone for an entry whose bridge is gone sets it up again."""
    _valves(hass)
    core = await _core_bridge(hass)
    entry = await _bridged(hass, core)
    await hass.config_entries.async_remove(core.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection"}
    )
    options = result["data_schema"].schema["bridge"].config["options"]
    assert [o["label"] for o in options] == [
        "Standalone",
        "Missing HomeKit Bridge (pick another)",
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"bridge": "standalone", "port": 51841, "pin": PIN}
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert server(hass, entry.entry_id).port == 51841
    assert not ir.async_get(hass).async_get_issue(
        DOMAIN, f"bridge_missing_{entry.entry_id}"
    )


async def test_bridge_disabled_then_enabled(hass: HomeAssistant) -> None:
    """Disabling the bridge raises a repair; enabling it brings ours back."""
    _valves(hass)
    core = await _core_bridge(hass)
    entry = await _bridged(hass, core)

    await hass.config_entries.async_set_disabled_by(
        core.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"bridge_missing_{entry.entry_id}"
    )

    await hass.config_entries.async_set_disabled_by(core.entry_id, None)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert server(hass, entry.entry_id).attached
    assert not ir.async_get(hass).async_get_issue(
        DOMAIN, f"bridge_missing_{entry.entry_id}"
    )


async def test_bridge_switched_to_accessory_mode(hass: HomeAssistant) -> None:
    """A bridge switched to accessory mode can't hold ours any more."""
    _valves(hass, *VALVES, "light.porch")
    core = await _core_bridge(hass)
    entry = await _bridged(hass, core)
    hass.config_entries.async_update_entry(
        core, options={**core.options, "mode": "accessory"}
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_removed_entry_frees_bridge_aid(hass: HomeAssistant) -> None:
    """Deleting a bridged accessory forgets its AID in the bridge's storage."""
    _valves(hass)
    core = await _core_bridge(hass)
    entry = await _bridged(hass, core)
    storage = core.runtime_data.homekit.aid_storage
    assert aid_key(entry.entry_id) in storage.allocations
    await hass.config_entries.async_remove(entry.entry_id)
    assert aid_key(entry.entry_id) not in storage.allocations


async def test_core_diagnostics_hide_pin(hass: HomeAssistant) -> None:
    """Core's diagnostics read our accessory's config without redacting it."""
    _valves(hass)
    core = await _core_bridge(hass)
    entry = await _bridged(hass, core, pin=PIN, port=51828)
    assert "pin" not in server(hass, entry.entry_id).accessory.config


async def test_options_reject_missing_bridge(hass: HomeAssistant) -> None:
    """Re-choosing a deleted bridge shows an error instead of saving it."""
    _valves(hass)
    core = await _core_bridge(hass)
    entry = await _bridged(hass, core)
    await hass.config_entries.async_remove(core.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "connection"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"bridge": core.entry_id, "port": 51842, "pin": PIN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"bridge": "bridge_missing"}
    assert entry.options.get("bridge") is None
