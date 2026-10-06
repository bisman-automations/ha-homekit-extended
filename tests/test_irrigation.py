"""Irrigation and faucet accessory tests."""

from __future__ import annotations

from datetime import timedelta
import os

from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.homekit_extended.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import char, server, services_by_type, setup_accessory

VALVES = ["valve.front", "valve.back"]


async def _setup(hass: HomeAssistant, valves=VALVES, **data):
    hass.states.async_set("valve.front", "closed", {"friendly_name": "Front Lawn"})
    hass.states.async_set("valve.back", "closed", {"friendly_name": "Back Lawn"})
    data.setdefault("default_duration", 600)
    return await setup_accessory(
        hass, "irrigation", "Sprinklers", valves=valves, **data
    )


async def test_zones_created(hass: HomeAssistant) -> None:
    """Each valve becomes a linked, named Valve service with its own run time."""
    entry = await _setup(hass, run_times={"valve.back": 1200})
    services = services_by_type(server(hass, entry.entry_id).accessory)
    system = services["IrrigationSystem"][0]
    valves = services["Valve"]
    assert [char(v, "Name").value for v in valves] == ["Front Lawn", "Back Lawn"]
    assert set(system.linked_services) == set(valves)
    assert [char(v, "SetDuration").value for v in valves] == [600, 1200]
    assert all(char(v, "ValveType").value == 1 for v in valves)


async def test_legacy_valve_format(hass: HomeAssistant) -> None:
    """Entries from the standalone integration still load."""
    entry = await _setup(hass, valves=[{"entity_id": "valve.front", "name": "Front"}])
    assert list(server(hass, entry.entry_id).accessory._zones) == ["valve.front"]


async def test_run_closes_valve_when_time_is_up(hass: HomeAssistant) -> None:
    """A HomeKit run opens the valve and closes it after the zone's run time."""
    entry = await _setup(hass, run_times={"valve.front": 120})
    acc = server(hass, entry.entry_id).accessory
    open_calls = async_mock_service(hass, "valve", "open_valve")
    close_calls = async_mock_service(hass, "valve", "close_valve")
    zone = acc._zones["valve.front"]

    acc._set_zone_active("valve.front", 1)
    await hass.async_block_till_done()
    assert open_calls[0].data == {"entity_id": "valve.front"}
    assert zone.in_use.value == 1
    assert 119 <= zone.remaining.get_value() <= 120
    assert acc._system_in_use.value == 1
    assert 119 <= acc._system_remaining.get_value() <= 120

    hass.states.async_set("valve.front", "open")
    await hass.async_block_till_done()
    assert zone.ends_at is not None

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=121), fire_all=True
    )
    await hass.async_block_till_done()
    assert close_calls[0].data == {"entity_id": "valve.front"}
    assert zone.remaining.get_value() == 0
    assert acc._system_in_use.value == 0


async def test_duration_changed_in_home_is_saved(hass: HomeAssistant) -> None:
    """A run time set in Apple Home is stored and doesn't re-publish."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    acc._set_zone_duration("valve.back", 45)
    await hass.async_block_till_done()
    assert entry.options["run_times"] == {"valve.front": 600, "valve.back": 45}
    assert server(hass, entry.entry_id).accessory is acc


async def test_external_close_cancels_run(hass: HomeAssistant) -> None:
    """Closing the valve outside HomeKit clears the countdown."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    async_mock_service(hass, "valve", "open_valve")
    close_calls = async_mock_service(hass, "valve", "close_valve")
    zone = acc._zones["valve.back"]

    acc._set_zone_active("valve.back", 1)
    hass.states.async_set("valve.back", "open")
    await hass.async_block_till_done()
    hass.states.async_set("valve.back", "closed")
    await hass.async_block_till_done()
    assert zone.ends_at is None and zone.close_handle is None
    assert zone.active.value == 0
    assert close_calls == []


async def test_system_off_stops_running_zones(hass: HomeAssistant) -> None:
    """Turning the irrigation system off closes the zones that are running."""
    entry = await _setup(hass, one_at_a_time=False)
    acc = server(hass, entry.entry_id).accessory
    async_mock_service(hass, "valve", "open_valve")
    close_calls = async_mock_service(hass, "valve", "close_valve")
    acc._set_zone_active("valve.front", 1)
    acc._set_zone_active("valve.back", 1)
    acc._set_system_active(0)
    await hass.async_block_till_done()
    assert sorted(c.data["entity_id"] for c in close_calls) == sorted(VALVES)
    assert acc._system_active.value == 0


async def test_one_zone_at_a_time(hass: HomeAssistant) -> None:
    """Starting a zone closes the one already running (the default)."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    async_mock_service(hass, "valve", "open_valve")
    close_calls = async_mock_service(hass, "valve", "close_valve")
    acc._set_zone_active("valve.front", 1)
    acc._set_zone_active("valve.back", 1)
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in close_calls] == ["valve.front"]
    assert not acc._zones["valve.front"].running
    assert acc._zones["valve.back"].running


async def test_run_all_zones_in_sequence(hass: HomeAssistant) -> None:
    """Turning the system on runs enabled zones in turn, skipping disabled ones."""
    hass.states.async_set("valve.side", "closed")
    entry = await _setup(
        hass,
        valves=["valve.front", "valve.side", "valve.back"],
        run_times={"valve.front": 60, "valve.side": 30, "valve.back": 120},
        disabled_zones=["valve.side"],
    )
    acc = server(hass, entry.entry_id).accessory
    open_calls = async_mock_service(hass, "valve", "open_valve")
    close_calls = async_mock_service(hass, "valve", "close_valve")
    assert acc._zones["valve.side"].configured.value == 0

    acc._set_system_active(1)
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in open_calls] == ["valve.front"]
    assert acc._system_active.value == 1
    assert 179 <= acc._system_remaining.get_value() <= 180  # 60 + 120

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=61), fire_all=True
    )
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in open_calls] == ["valve.front", "valve.back"]
    assert [c.data["entity_id"] for c in close_calls] == ["valve.front"]

    # Stopping the current zone in Apple Home ends the run when it's the last.
    acc._set_zone_active("valve.back", 0)
    await hass.async_block_till_done()
    assert acc._system_active.value == 0
    assert acc._sequence_current is None


async def test_stopping_a_zone_skips_to_the_next(hass: HomeAssistant) -> None:
    """During a run, stopping the current zone moves on; a manual start ends it."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    open_calls = async_mock_service(hass, "valve", "open_valve")
    async_mock_service(hass, "valve", "close_valve")
    acc._set_system_active(1)
    acc._set_zone_active("valve.front", 0)
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in open_calls] == ["valve.front", "valve.back"]
    acc._set_system_active(1)
    acc._set_zone_active("valve.back", 1)  # manual start
    assert acc._sequence_current is None and not acc._sequence


async def test_zone_enabled_in_home_is_saved(hass: HomeAssistant) -> None:
    """Turning a zone off in Apple Home is stored and applied without reloading."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    acc._zones["valve.back"].configured.client_update_value(0)
    await hass.async_block_till_done()
    assert entry.options["disabled_zones"] == ["valve.back"]
    assert server(hass, entry.entry_id).accessory is acc


async def test_unavailable_valve_is_a_fault(hass: HomeAssistant) -> None:
    """An unavailable valve shows a fault on its zone and the system."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    hass.states.async_set("valve.front", "unavailable")
    await hass.async_block_till_done()
    assert acc._zones["valve.front"].fault.value == 1
    assert acc._system_fault.value == 1
    hass.states.async_set("valve.front", "closed")
    await hass.async_block_till_done()
    assert acc._system_fault.value == 0


async def test_pump_follows_zones_with_delay(hass: HomeAssistant) -> None:
    """The pump starts with the first zone and stops 5 s after the last one."""
    hass.states.async_set("switch.pump", "off")
    entry = await _setup(hass, master="switch.pump")
    acc = server(hass, entry.entry_id).accessory
    async_mock_service(hass, "valve", "open_valve")
    async_mock_service(hass, "valve", "close_valve")
    pump_on = async_mock_service(hass, "switch", "turn_on")
    pump_off = async_mock_service(hass, "switch", "turn_off")

    acc._set_zone_active("valve.front", 1)
    acc._set_zone_active("valve.back", 1)  # switch zones; pump stays on
    await hass.async_block_till_done()
    assert len(pump_on) == 1

    acc._set_zone_active("valve.back", 0)
    await hass.async_block_till_done()
    assert pump_off == []
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=6), fire_all=True
    )
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in pump_off] == ["switch.pump"]


async def test_faucet_opens_all_outlets(hass: HomeAssistant) -> None:
    """Turning a shower on opens every outlet; outlets are shower heads."""
    hass.states.async_set("valve.head", "closed")
    hass.states.async_set("valve.rain", "closed")
    entry = await setup_accessory(
        hass,
        "faucet",
        "Shower",
        valves=["valve.head", "valve.rain"],
        valve_type="shower_head",
        default_duration=0,
    )
    acc = server(hass, entry.entry_id).accessory
    services = services_by_type(acc)
    assert "Faucet" in services
    assert all(char(v, "ValveType").value == 2 for v in services["Valve"])
    open_calls = async_mock_service(hass, "valve", "open_valve")
    acc._set_system_active(1)
    await hass.async_block_till_done()
    assert sorted(c.data["entity_id"] for c in open_calls) == [
        "valve.head",
        "valve.rain",
    ]
    # A run time of 0 runs until stopped.
    assert all(zone.close_handle is None for zone in acc._zones.values())


async def test_unload_cancels_timers(hass: HomeAssistant) -> None:
    """Unloading mid-run leaves no timers behind."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    async_mock_service(hass, "valve", "open_valve")
    acc._set_zone_active("valve.front", 1)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert all(z.close_handle is None for z in acc._zones.values())


async def test_remove_entry_deletes_pairing(hass: HomeAssistant) -> None:
    """Removing the entry deletes HAP pairing state."""
    entry = await _setup(hass)
    path = hass.config.path(f".storage/{DOMAIN}.{entry.entry_id}.state")
    assert os.path.exists(path)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not os.path.exists(path)
