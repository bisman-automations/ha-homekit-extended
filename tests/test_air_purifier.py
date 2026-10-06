"""Air purifier accessory tests."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.homekit_extended.air_purifier import (
    homekit_air_quality,
    homekit_air_quality_from_pm25,
)
from custom_components.homekit_extended.const import DOMAIN
from homeassistant.components.fan import FanEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant

from .conftest import server, services_by_type

FAN = "fan.purifier"
FEATURES = (
    FanEntityFeature.SET_SPEED
    | FanEntityFeature.OSCILLATE
    | FanEntityFeature.PRESET_MODE
    | FanEntityFeature.TURN_ON
    | FanEntityFeature.TURN_OFF
)


async def _setup(hass: HomeAssistant, **sensors) -> MockConfigEntry:
    hass.states.async_set(
        FAN,
        "on",
        {
            ATTR_SUPPORTED_FEATURES: FEATURES,
            "percentage": 40,
            "percentage_step": 20,
            "oscillating": False,
            "preset_modes": ["Auto", "Sleep"],
            "preset_mode": None,
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Purifier",
        data={
            "accessory_type": "air_purifier",
            "port": 51829,
            "pin": "031-45-154",
            "fan": FAN,
            **sensors,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _char(service, name):
    return service.get_characteristic(name)


async def test_services_and_initial_state(hass: HomeAssistant) -> None:
    """Optional sensors add linked services and mirror state."""
    hass.states.async_set("sensor.pm25", "20")
    hass.states.async_set("sensor.temp", "68", {ATTR_UNIT_OF_MEASUREMENT: "°F"})
    hass.states.async_set("sensor.filter", "8")
    entry = await _setup(
        hass,
        pm25_sensor="sensor.pm25",
        temperature_sensor="sensor.temp",
        filter_life_sensor="sensor.filter",
    )
    acc = server(hass, entry.entry_id).accessory
    services = services_by_type(acc)
    assert "HumiditySensor" not in services
    purifier = services["AirPurifier"][0]

    assert _char(purifier, "Active").value == 1
    assert _char(purifier, "CurrentAirPurifierState").value == 2
    assert _char(purifier, "RotationSpeed").value == 40
    assert _char(purifier, "RotationSpeed").properties["minStep"] == 20
    assert _char(services["AirQualitySensor"][0], "AirQuality").value == 2
    assert _char(services["AirQualitySensor"][0], "PM2.5Density").value == 20
    assert _char(services["TemperatureSensor"][0], "CurrentTemperature").value == 20
    assert _char(services["FilterMaintenance"][0], "FilterChangeIndication").value == 1

    hass.states.async_set(FAN, "off", {ATTR_SUPPORTED_FEATURES: FEATURES})
    await hass.async_block_till_done()
    assert _char(purifier, "Active").value == 0
    assert _char(services["Fanv2"][0], "Active").value == 0


async def test_homekit_commands(hass: HomeAssistant) -> None:
    """HomeKit writes become fan service calls."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    set_pct = async_mock_service(hass, "fan", "set_percentage")
    preset = async_mock_service(hass, "fan", "set_preset_mode")
    oscillate = async_mock_service(hass, "fan", "oscillate")
    turn_off = async_mock_service(hass, "fan", "turn_off")

    acc._set_chars({"RotationSpeed": 60, "SwingMode": 1})
    acc._set_chars({"TargetAirPurifierState": 1})
    acc._set_chars({"Active": 0})
    await hass.async_block_till_done()

    assert set_pct[0].data == {"entity_id": FAN, "percentage": 60}
    assert oscillate[0].data == {"entity_id": FAN, "oscillating": True}
    assert preset[0].data == {"entity_id": FAN, "preset_mode": "Auto"}
    assert len(turn_off) == 1


async def test_unload(hass: HomeAssistant, mock_hap_network) -> None:
    """Unloading stops the driver and drops listeners."""
    entry = await _setup(hass)
    acc = server(hass, entry.entry_id).accessory
    assert await hass.config_entries.async_unload(entry.entry_id)
    mock_hap_network["stop"].assert_awaited_once()
    assert acc._subscriptions == []


def test_air_quality_mapping() -> None:
    """AQI and PM2.5 map onto HomeKit's 1-5 scale."""
    assert [homekit_air_quality(v) for v in (1, 5, 30, 75, 120, 180, 300)] == [
        1,
        5,
        1,
        2,
        3,
        4,
        5,
    ]
    assert [homekit_air_quality_from_pm25(v) for v in (5, 20, 50, 100, 200)] == [
        1,
        2,
        3,
        4,
        5,
    ]
