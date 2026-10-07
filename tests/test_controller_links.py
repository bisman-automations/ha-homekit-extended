"""Valves whose controller has its own run-time and time-remaining entities.

Mirrors Rain Bird Extended: a controller device, one device per zone linked to
it, and on each zone device a valve, a run-time number (seconds, 1-minute
steps) and an end-time timestamp sensor.
"""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.homekit_extended.const import DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import PIN, server, setup_accessory

RUNTIME_ATTRS = {
    "unit_of_measurement": "s",
    "min": 60,
    "max": 86400,
    "step": 60,
    "device_class": "duration",
}


def _rain_bird(hass: HomeAssistant) -> str:
    """Create a Rain Bird-style controller with two zones; return its device id."""
    source = MockConfigEntry(domain="rainbird")
    source.add_to_hass(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    controller = devices.async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("rainbird", "ctrl")},
        name="Rain Bird Controller",
        manufacturer="Rain Bird",
        model="ARC8",
        sw_version="2.12",
        connections={(dr.CONNECTION_NETWORK_MAC, "70:B8:F6:9A:5B:1C")},
    )
    for zone, name in ((1, "Front Lawn"), (2, "Back Lawn")):
        device = devices.async_get_or_create(
            config_entry_id=source.entry_id,
            identifiers={("rainbird", f"ctrl-{zone}")},
            name=name,
            via_device=("rainbird", "ctrl"),
        )
        for domain, key, device_class in (
            ("valve", "valve", "water"),
            ("number", "valve_runtime", "duration"),
            ("sensor", "time_remaining", "timestamp"),
        ):
            entities.async_get_or_create(
                domain,
                "rainbird_extended",
                f"ctrl-{zone}-{key}",
                suggested_object_id=f"zone_{zone}_{key}",
                device_id=device.id,
                original_device_class=device_class,
                entity_category=(
                    er.EntityCategory.CONFIG if domain == "number" else None
                ),
            )
        hass.states.async_set(
            f"valve.zone_{zone}_valve", "closed", {"friendly_name": name}
        )
        hass.states.async_set(f"number.zone_{zone}_valve_runtime", "600", RUNTIME_ATTRS)
        hass.states.async_set(
            f"sensor.zone_{zone}_time_remaining",
            "unknown",
            {"device_class": "timestamp"},
        )
    return controller.id


VALVES = ["valve.zone_1_valve", "valve.zone_2_valve"]


async def test_picking_the_controller_finds_zone_valves(hass: HomeAssistant) -> None:
    """Valves on zone devices under the controller are detected."""
    controller = _rain_bird(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "irrigation"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"device": controller, "connection": {"port": 51828, "pin": PIN}},
    )
    valves = next(k for k in result["data_schema"].schema if k == "valves")
    assert valves.description["suggested_value"] == VALVES

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"valves": VALVES, "default_duration": 900}
    )
    # Run times come from, and are written to, the controller's numbers.
    assert result["step_id"] == "run_times"
    fields = {str(k): k for k in result["data_schema"].schema}
    assert fields["Front Lawn"].description["suggested_value"] == 600
    set_value = async_mock_service(hass, "number", "set_value")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"Front Lawn": 1200, "Back Lawn": 600}
    )
    assert result["data"]["run_times"] == {}
    assert [c.data for c in set_value] == [
        {"entity_id": "number.zone_1_valve_runtime", "value": 1200},
        {"entity_id": "number.zone_2_valve_runtime", "value": 600},
    ]


async def test_run_time_uses_controller_number(hass: HomeAssistant) -> None:
    """SetDuration mirrors the number, takes its limits, and writes back."""
    _rain_bird(hass)
    entry = await setup_accessory(hass, "irrigation", "Sprinklers", valves=VALVES)
    acc = server(hass, entry.entry_id).accessory
    zone = acc._zones["valve.zone_1_valve"]
    assert zone.links.duration == "number.zone_1_valve_runtime"
    assert zone.links.end_time == "sensor.zone_1_time_remaining"
    assert zone.set_duration.value == 600
    assert zone.set_duration.properties["maxValue"] == 86400
    assert zone.set_duration.properties["minStep"] == 60

    set_value = async_mock_service(hass, "number", "set_value")
    zone.set_duration.client_update_value(1800)
    await hass.async_block_till_done()
    assert set_value[0].data == {
        "entity_id": "number.zone_1_valve_runtime",
        "value": 1800,
    }
    # Not stored here: the controller is the source of truth.
    assert "run_times" not in entry.options

    hass.states.async_set("number.zone_1_valve_runtime", "2400", RUNTIME_ATTRS)
    await hass.async_block_till_done()
    assert zone.set_duration.value == 2400 and zone.duration == 2400


async def test_countdown_and_close_come_from_controller(hass: HomeAssistant) -> None:
    """No local close timer; remaining time comes from the end-time sensor."""
    _rain_bird(hass)
    entry = await setup_accessory(hass, "irrigation", "Sprinklers", valves=VALVES)
    acc = server(hass, entry.entry_id).accessory
    open_calls = async_mock_service(hass, "valve", "open_valve")
    close_calls = async_mock_service(hass, "valve", "close_valve")
    zone = acc._zones["valve.zone_1_valve"]

    acc._set_zone_active("valve.zone_1_valve", 1)
    await hass.async_block_till_done()
    assert open_calls[0].data == {"entity_id": "valve.zone_1_valve"}
    assert zone.close_handle is None

    ends = dt_util.utcnow() + timedelta(seconds=300)
    hass.states.async_set("valve.zone_1_valve", "open")
    hass.states.async_set(
        "sensor.zone_1_time_remaining", ends.isoformat(), {"device_class": "timestamp"}
    )
    await hass.async_block_till_done()
    assert 298 <= zone.remaining.get_value() <= 300
    assert 298 <= acc._system_remaining.get_value() <= 300

    # Past the run time nothing closes the valve from here; the controller does.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2), fire_all=True)
    await hass.async_block_till_done()
    assert close_calls == []
    hass.states.async_set("valve.zone_1_valve", "closed")
    hass.states.async_set(
        "sensor.zone_1_time_remaining", "unknown", {"device_class": "timestamp"}
    )
    await hass.async_block_till_done()
    assert zone.remaining.get_value() == 0
    assert acc._system_in_use.value == 0


async def test_run_all_advances_when_controller_closes(hass: HomeAssistant) -> None:
    """A run of all zones moves on when the controller finishes each zone."""
    _rain_bird(hass)
    entry = await setup_accessory(hass, "irrigation", "Sprinklers", valves=VALVES)
    acc = server(hass, entry.entry_id).accessory
    open_calls = async_mock_service(hass, "valve", "open_valve")
    async_mock_service(hass, "valve", "close_valve")

    acc._set_system_active(1)
    hass.states.async_set("valve.zone_1_valve", "open")
    await hass.async_block_till_done()
    assert 599 <= acc._system_remaining.get_value() <= 1200
    hass.states.async_set("valve.zone_1_valve", "closed")
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in open_calls] == VALVES


async def test_controller_timers_can_be_turned_off(hass: HomeAssistant) -> None:
    """With the option off, zones keep their own run times and timers."""
    _rain_bird(hass)
    entry = await setup_accessory(
        hass,
        "irrigation",
        "Sprinklers",
        valves=VALVES,
        default_duration=120,
        use_controller_timers=False,
    )
    zone = server(hass, entry.entry_id).accessory._zones["valve.zone_1_valve"]
    assert not zone.links
    assert zone.set_duration.value == 120
    assert zone.set_duration.properties["maxValue"] == 3600


async def test_run_times_screen_explains_sources(hass: HomeAssistant) -> None:
    """The run-times screen names controller zones and their real limits."""
    _rain_bird(hass)
    hass.states.async_set("valve.plain", "closed", {"friendly_name": "Garden"})
    entry = await setup_accessory(
        hass, "irrigation", "Sprinklers", valves=[*VALVES, "valve.plain"]
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "run_times"}
    )
    details = result["description_placeholders"]["details"]
    assert details == (
        "**Set on the controller:** Front Lawn, Back Lawn."
        " From 1 minute to 24 hours in 1 minute steps.\n\n"
        "**Stored by HomeKit Extended:** Garden. Up to 1 hour; 0 runs until stopped."
    )


async def test_accessory_info_defaults_to_controller(hass: HomeAssistant) -> None:
    """Empty accessory information follows the controller behind the zones."""
    controller = _rain_bird(hass)
    # Older entries don't store the device: it's found from the valves.
    entry = await setup_accessory(hass, "irrigation", valves=VALVES)
    info = server(hass, entry.entry_id).accessory.get_service("AccessoryInformation")
    assert info.get_characteristic("Manufacturer").value == "Rain Bird"
    assert info.get_characteristic("Model").value == "ARC8"
    assert info.get_characteristic("SerialNumber").value == "70:B8:F6:9A:5B:1C"
    assert info.get_characteristic("FirmwareRevision").value == "2.12"

    # The options step shows what an empty field falls back to.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "info"}
    )
    assert "Rain Bird" in result["description_placeholders"]["defaults"]
    assert "70:B8:F6:9A:5B:1C" in result["description_placeholders"]["defaults"]

    # A value set by hand still wins over the device.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"model": "ARC8 Garage"}
    )
    await hass.async_block_till_done()
    assert info.get_characteristic("Model").value == "ARC8 Garage"
    assert info.get_characteristic("Manufacturer").value == "Rain Bird"
    assert controller
