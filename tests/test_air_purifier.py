"""Air purifier accessory tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.homekit_extended.accessories.air_purifier import _detect
from custom_components.homekit_extended.accessories.sensors import (
    homekit_air_quality,
    pm10_air_quality,
    pm25_air_quality,
    text_air_quality,
)
from custom_components.homekit_extended.const import CONF_CHILD_LOCK
from homeassistant.components.fan import FanEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant

from .conftest import char, server, services_by_type, setup_accessory

FAN = "fan.purifier"
FEATURES = (
    FanEntityFeature.SET_SPEED
    | FanEntityFeature.OSCILLATE
    | FanEntityFeature.PRESET_MODE
    | FanEntityFeature.TURN_ON
    | FanEntityFeature.TURN_OFF
)


async def _setup(hass: HomeAssistant, **sensors):
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
    return await setup_accessory(hass, "air_purifier", "Purifier", fan=FAN, **sensors)


async def test_services_and_state(hass: HomeAssistant) -> None:
    """Optional sensors add linked services and mirror state."""
    hass.states.async_set("sensor.pm25", "20")
    hass.states.async_set("sensor.pm10", "40")
    hass.states.async_set("sensor.temp", "68", {ATTR_UNIT_OF_MEASUREMENT: "°F"})
    hass.states.async_set("sensor.filter", "8")
    entry = await _setup(
        hass,
        pm25_sensor="sensor.pm25",
        pm10_sensor="sensor.pm10",
        temperature_sensor="sensor.temp",
        filter_life_sensor="sensor.filter",
    )
    services = services_by_type(server(hass, entry.entry_id).accessory)
    assert "HumiditySensor" not in services
    purifier = services["AirPurifier"][0]
    assert char(purifier, "LockPhysicalControls") is None
    assert char(purifier, "Active").value == 1
    assert char(purifier, "CurrentAirPurifierState").value == 2
    assert char(purifier, "RotationSpeed").value == 40
    assert char(purifier, "RotationSpeed").properties["minStep"] == 20
    air_svc = services["AirQualitySensor"][0]
    assert char(air_svc, "AirQuality").value == 2
    assert char(air_svc, "PM2.5Density").value == 20
    assert char(air_svc, "PM10Density").value == 40
    assert char(services["TemperatureSensor"][0], "CurrentTemperature").value == 20
    assert char(services["FilterMaintenance"][0], "FilterChangeIndication").value == 1

    hass.states.async_set(FAN, "off", {ATTR_SUPPORTED_FEATURES: FEATURES})
    await hass.async_block_till_done()
    assert char(purifier, "Active").value == 0
    assert char(services["Fanv2"][0], "Active").value == 0


async def test_pm10_and_text_air_quality(hass: HomeAssistant) -> None:
    """PM10 alone and text air quality values map correctly."""
    hass.states.async_set("sensor.aqi", "Good")
    entry = await _setup(hass, air_quality_sensor="sensor.aqi")
    services = services_by_type(server(hass, entry.entry_id).accessory)
    air_svc = services["AirQualitySensor"][0]
    assert char(air_svc, "AirQuality").value == 2

    # Text states update live
    hass.states.async_set("sensor.aqi", "Excellent")
    await hass.async_block_till_done()
    assert char(air_svc, "AirQuality").value == 1

    hass.states.async_set("sensor.aqi", "Poor")
    await hass.async_block_till_done()
    assert char(air_svc, "AirQuality").value == 5

    # Standalone PM10 maps to HomeKit air quality
    hass.states.async_set("sensor.pm10_only", "200")
    entry_pm10 = await _setup(hass, pm10_sensor="sensor.pm10_only")
    pm10_air = services_by_type(server(hass, entry_pm10.entry_id).accessory)[
        "AirQualitySensor"
    ][0]
    assert char(pm10_air, "PM10Density").value == 200
    assert char(pm10_air, "AirQuality").value == 3  # PM10 200 is Fair (3)

    # Worst pollutant takes precedence when both PM2.5 and PM10 are present
    hass.states.async_set("sensor.pm25_clean", "5")  # Excellent (1)
    hass.states.async_set("sensor.pm10_polluted", "200")  # Fair (3)
    entry_both = await _setup(
        hass,
        pm25_sensor="sensor.pm25_clean",
        pm10_sensor="sensor.pm10_polluted",
    )
    both_air = services_by_type(server(hass, entry_both.entry_id).accessory)[
        "AirQualitySensor"
    ][0]
    assert char(both_air, "PM2.5Density").value == 5
    assert char(both_air, "PM10Density").value == 200
    assert char(both_air, "AirQuality").value == 3  # Worst of 1 and 3 is 3


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
    """AQI, PM2.5, PM10 and text states map onto HomeKit's 1-5 scale."""
    assert [homekit_air_quality(v) for v in (1, 5, 30, 75, 120, 180, 300)] == [
        1, 5, 1, 2, 3, 4, 5,
    ]  # fmt: skip
    assert [homekit_air_quality(v) for v in ("Excellent", "Good", "Fair", "Inferior", "Poor")] == [
        1, 2, 3, 4, 5,
    ]  # fmt: skip
    assert [pm25_air_quality(v) for v in (5, 20, 50, 100, 200)] == [1, 2, 3, 4, 5]
    assert [pm10_air_quality(v) for v in (30, 100, 200, 300, 400)] == [1, 2, 3, 4, 5]
    assert [text_air_quality(t) for t in ("excellent", "Good ", "FAIR", "moderate", "poor", "unknown", "invalid")] == [
        1, 2, 3, 3, 5, 0, None,
    ]  # fmt: skip


async def test_fan_service_setting(hass: HomeAssistant) -> None:
    """Purifiers set up before the setting keep their Fan; new ones leave it out.

    Siri counts a Fan service as a fan in the room, so "turn off the fan"
    turned the purifier off too.
    """
    old = await _setup(hass)
    assert "Fanv2" in services_by_type(server(hass, old.entry_id).accessory)

    hass.states.async_set(FAN, "on", {ATTR_SUPPORTED_FEATURES: FEATURES})
    new = await setup_accessory(
        hass, "air_purifier", "New", port=51829, fan=FAN, fan_service=False
    )
    accessory = server(hass, new.entry_id).accessory
    services = services_by_type(accessory)
    assert "Fanv2" not in services
    # Still fully controllable on the purifier itself.
    purifier = services["AirPurifier"][0]
    assert char(purifier, "Active").value == 1
    turn_off = async_mock_service(hass, "fan", "turn_off")
    accessory._set_chars({"Active": 0})
    await hass.async_block_till_done()
    assert len(turn_off) == 1
    hass.states.async_set(FAN, "off", {ATTR_SUPPORTED_FEATURES: FEATURES})
    await hass.async_block_till_done()
    assert char(purifier, "Active").value == 0

    # The options form shows an older purifier's Fan as on, so saving other
    # changes doesn't remove it; switching it off re-publishes without it.
    result = await hass.config_entries.options.async_init(old.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "entities"}
    )
    field = next(k for k in result["data_schema"].schema if k == "fan_service")
    assert field.description["suggested_value"] is True
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"fan": FAN, "fan_service": False}
    )
    await hass.async_block_till_done()
    assert "Fanv2" not in services_by_type(server(hass, old.entry_id).accessory)


async def test_child_lock_switch(hass: HomeAssistant) -> None:
    """Child lock as a switch mirrors state and handles HomeKit commands."""
    hass.states.async_set("switch.purifier_lock", "on")
    entry = await _setup(hass, child_lock="switch.purifier_lock")
    acc = server(hass, entry.entry_id).accessory
    services = services_by_type(acc)
    purifier = services["AirPurifier"][0]
    fan = services["Fanv2"][0]
    assert char(purifier, "LockPhysicalControls").value == 1
    assert char(fan, "LockPhysicalControls").value == 1

    hass.states.async_set("switch.purifier_lock", "off")
    await hass.async_block_till_done()
    assert char(purifier, "LockPhysicalControls").value == 0
    assert char(fan, "LockPhysicalControls").value == 0

    turn_on = async_mock_service(hass, "switch", "turn_on")
    turn_off = async_mock_service(hass, "switch", "turn_off")
    acc._set_chars({"LockPhysicalControls": 1})
    await hass.async_block_till_done()
    assert turn_on[0].data == {"entity_id": "switch.purifier_lock"}

    acc._set_chars({"LockPhysicalControls": 0})
    await hass.async_block_till_done()
    assert turn_off[0].data == {"entity_id": "switch.purifier_lock"}


async def test_child_lock_lock_domain(hass: HomeAssistant) -> None:
    """Child lock as a lock domain entity uses lock/unlock services."""
    hass.states.async_set("lock.purifier_lock", "locked")
    entry = await _setup(hass, child_lock="lock.purifier_lock")
    acc = server(hass, entry.entry_id).accessory
    services = services_by_type(acc)
    purifier = services["AirPurifier"][0]
    assert char(purifier, "LockPhysicalControls").value == 1

    hass.states.async_set("lock.purifier_lock", "unlocked")
    await hass.async_block_till_done()
    assert char(purifier, "LockPhysicalControls").value == 0

    lock_svc = async_mock_service(hass, "lock", "lock")
    unlock_svc = async_mock_service(hass, "lock", "unlock")
    acc._set_chars({"LockPhysicalControls": 1})
    await hass.async_block_till_done()
    assert lock_svc[0].data == {"entity_id": "lock.purifier_lock"}

    acc._set_chars({"LockPhysicalControls": 0})
    await hass.async_block_till_done()
    assert unlock_svc[0].data == {"entity_id": "lock.purifier_lock"}


def test_detect_child_lock(hass: HomeAssistant) -> None:
    """Device detection picks up child lock switches."""
    fan_entry = MagicMock(
        domain="fan",
        entity_id="fan.purifier",
        device_class=None,
        original_device_class=None,
    )
    lock_entry = MagicMock(
        domain="switch",
        entity_id="switch.purifier_child_lock",
        original_name="Child Lock",
        translation_key="child_lock",
    )
    detected = _detect(hass, [fan_entry, lock_entry])
    assert detected.get(CONF_CHILD_LOCK) == "switch.purifier_child_lock"


async def test_child_lock_without_separate_fan(hass: HomeAssistant) -> None:
    """With Separate fan control off, the child lock is on the purifier only."""
    hass.states.async_set(FAN, "on", {ATTR_SUPPORTED_FEATURES: FEATURES})
    hass.states.async_set("switch.purifier_lock", "on")
    entry = await setup_accessory(
        hass,
        "air_purifier",
        "Locked",
        port=51830,
        fan=FAN,
        fan_service=False,
        child_lock="switch.purifier_lock",
    )
    services = services_by_type(server(hass, entry.entry_id).accessory)
    assert "Fanv2" not in services
    assert char(services["AirPurifier"][0], "LockPhysicalControls").value == 1
