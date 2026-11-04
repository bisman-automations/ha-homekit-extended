"""Instance and accessory IDs survive from the files written before 3.0.0.

Those files are Home Assistant storage documents. The versions and the
``data.allocations`` maps are unchanged, so an accessory paired with an
earlier release keeps the IDs Apple Home already knows. The test harness
keeps that document in memory; it is the same object Store reads from disk.
"""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.homekit_extended.const import DOMAIN
from custom_components.homekit_extended.id_storage import (
    aid_storage_key,
    iid_storage_key,
)
from homeassistant.core import HomeAssistant

from .conftest import PIN, server

# HAP type of the Valve service, then the valve's entity id, then no characteristic.
VALVE_SERVICE_KEY = "D0_valve.front__"
# Name characteristic on that same service.
VALVE_NAME_KEY = "D0_valve.front_23_"


def _document(key: str, version: int, allocations: dict) -> dict:
    """The on-disk storage document written since IDs were first stored."""
    return {
        "version": version,
        "minor_version": 1,
        "key": key,
        "data": {"allocations": allocations},
    }


async def _irrigation(hass: HomeAssistant, entry_id: str, **data) -> MockConfigEntry:
    hass.states.async_set("valve.front", "closed", {"friendly_name": "Front Lawn"})
    hass.states.async_set("valve.back", "closed", {"friendly_name": "Back Lawn"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=entry_id,
        title="Sprinklers",
        data={
            "accessory_type": "irrigation",
            "port": 51828,
            "pin": PIN,
            "valves": ["valve.front", "valve.back"],
            "default_duration": 600,
            **data,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_old_iid_file_is_used(hass: HomeAssistant, hass_storage: dict) -> None:
    """A 2.x instance-ID file assigns the same IIDs after the storage rewrite."""
    entry_id = "kept-iids"
    key = iid_storage_key(entry_id)
    hass_storage[key] = _document(
        key, 2, {"1": {VALVE_SERVICE_KEY: 40, VALVE_NAME_KEY: 41}}
    )
    entry = await _irrigation(hass, entry_id)
    accessory = server(hass, entry.entry_id).accessory
    valve = next(
        service
        for service in accessory.services
        if service.display_name == "Valve" and service.unique_id == "valve.front"
    )
    assert accessory.iid_manager.get_iid(valve) == 40
    assert accessory.iid_manager.get_iid(valve.get_characteristic("Name")) == 41

    # Saving again keeps the same document version and key.
    await server(hass, entry.entry_id).iid_storage.async_save()
    stored = hass_storage[key]
    assert stored["version"] == 2
    assert stored["key"] == key
    assert stored["data"]["allocations"]["1"][VALVE_SERVICE_KEY] == 40
    assert stored["data"]["allocations"]["1"][VALVE_NAME_KEY] == 41


async def test_old_aid_file_is_used(hass: HomeAssistant, hass_storage: dict) -> None:
    """A 2.x accessory-ID file keeps each zone's AID inside our own bridge."""
    entry_id = "kept-aids"
    key = aid_storage_key(entry_id)
    hass_storage[key] = _document(key, 1, {"valve.front": 42, "valve.back": 43})
    entry = await _irrigation(hass, entry_id, zone_layout="accessories")
    aids = {
        zone.entity_id: zone.aid
        for zone in server(hass, entry.entry_id).published.accessories.values()
    }
    assert aids == {"valve.front": 42, "valve.back": 43}

    await server(hass, entry.entry_id).aid_storage.async_save()
    stored = hass_storage[key]
    assert stored["version"] == 1
    assert stored["key"] == key
    assert stored["data"]["allocations"] == {"valve.front": 42, "valve.back": 43}


async def test_remove_entry_deletes_id_files(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """Removing an entry deletes its instance and accessory ID documents."""
    entry = await _irrigation(hass, "remove-ids", zone_layout="accessories")
    srv = server(hass, entry.entry_id)
    await srv.iid_storage.async_save()
    await srv.aid_storage.async_save()
    iid_key = iid_storage_key(entry.entry_id)
    aid_key = aid_storage_key(entry.entry_id)
    assert iid_key in hass_storage
    assert aid_key in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert iid_key not in hass_storage
    assert aid_key not in hass_storage
