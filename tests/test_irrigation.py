"""Irrigation accessory tests."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.homekit_extended.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import server, services_by_type

VALVES = ["valve.front", "valve.back"]


async def _setup(hass: HomeAssistant, valves=VALVES) -> MockConfigEntry:
    hass.states.async_set("valve.front", "closed", {"friendly_name": "Front Lawn"})
    hass.states.async_set("valve.back", "closed", {"friendly_name": "Back Lawn"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Sprinklers",
        data={
            "accessory_type": "irrigation",
            "port": 51828,
            "pin": "031-45-154",
            "valves": valves,
            "default_duration": 600,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _zone_chars(acc, entity_id):
    return acc._zones[entity_id]


async def test_zones_created(hass: HomeAssistant) -> None:
    """Each valve becomes a linked, named Valve service."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    services = services_by_type(acc)
    system = services["IrrigationSystem"][0]
    valves = services["Valve"]
    assert [v.get_characteristic("Name").value for v in valves] == [
        "Front Lawn",
        "Back Lawn",
    ]
    assert set(system.linked_services) == set(valves)
    assert all(v.get_characteristic("SetDuration").value == 600 for v in valves)


async def test_legacy_valve_format(hass: HomeAssistant) -> None:
    """Entries from the standalone integration still load."""
    entry = await _setup(hass, valves=[{"entity_id": "valve.front", "name": "Front"}])
    assert list(server(hass, entry.entry_id).accessory._zones) == ["valve.front"]


async def test_run_closes_valve_when_time_is_up(hass: HomeAssistant) -> None:
    """A HomeKit run opens the valve and closes it after SetDuration."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    open_calls = async_mock_service(hass, "valve", "open_valve")
    close_calls = async_mock_service(hass, "valve", "close_valve")
    zone = _zone_chars(acc, "valve.front")

    acc._set_zone_duration("valve.front", 120)
    acc._set_zone_active("valve.front", 1)
    await hass.async_block_till_done()
    assert open_calls[0].data == {"entity_id": "valve.front"}
    assert zone.in_use.value == 1
    assert 119 <= zone.remaining.get_value() <= 120
    assert acc._system_in_use.value == 1
    assert 119 <= acc._system_remaining.get_value() <= 120

    hass.states.async_set("valve.front", "open")
    await hass.async_block_till_done()
    assert zone.ends_at is not None  # HA confirming the open keeps the timer

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=121), fire_all=True
    )
    await hass.async_block_till_done()
    assert close_calls[0].data == {"entity_id": "valve.front"}
    assert zone.remaining.get_value() == 0
    assert acc._system_in_use.value == 0


async def test_external_close_cancels_run(hass: HomeAssistant) -> None:
    """Closing the valve outside HomeKit clears the countdown."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    async_mock_service(hass, "valve", "open_valve")
    close_calls = async_mock_service(hass, "valve", "close_valve")
    zone = _zone_chars(acc, "valve.back")

    acc._set_zone_active("valve.back", 1)
    hass.states.async_set("valve.back", "open")
    await hass.async_block_till_done()
    hass.states.async_set("valve.back", "closed")
    await hass.async_block_till_done()

    assert zone.ends_at is None and zone.close_handle is None
    assert zone.active.value == 0
    assert close_calls == []


async def test_system_off_stops_all_zones(hass: HomeAssistant) -> None:
    """Turning the irrigation system off closes every zone."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    close_calls = async_mock_service(hass, "valve", "close_valve")
    acc._set_system_active(0)
    await hass.async_block_till_done()
    assert sorted(c.data["entity_id"] for c in close_calls) == sorted(VALVES)


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
    import os

    assert os.path.exists(path)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not os.path.exists(path)
