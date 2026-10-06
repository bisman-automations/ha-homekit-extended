"""Ceiling fan, buttons, multi-sensor, air quality monitor and power strip tests."""

from __future__ import annotations

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.homekit_extended.accessories.buttons import press_map
from homeassistant.components.fan import FanEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant

from .conftest import char, server, services_by_type, setup_accessory


async def test_ceiling_fan(hass: HomeAssistant) -> None:
    """Fan and light share an accessory; writes go to the right entity."""
    hass.states.async_set(
        "fan.ceiling",
        "on",
        {
            ATTR_SUPPORTED_FEATURES: FanEntityFeature.SET_SPEED
            | FanEntityFeature.DIRECTION,
            "percentage": 33,
            "direction": "reverse",
        },
    )
    hass.states.async_set(
        "light.ceiling",
        "on",
        {"brightness": 128, "supported_color_modes": ["brightness"]},
    )
    entry = await setup_accessory(
        hass, "ceiling_fan", "Bedroom Fan", fan="fan.ceiling", light="light.ceiling"
    )
    acc = server(hass, entry.entry_id).accessory
    services = services_by_type(acc)
    fan, light = services["Fanv2"][0], services["Lightbulb"][0]
    assert light in fan.linked_services
    assert char(fan, "RotationSpeed").value == 33
    assert char(fan, "RotationDirection").value == 1
    assert char(light, "Brightness").value == 50

    set_pct = async_mock_service(hass, "fan", "set_percentage")
    light_on = async_mock_service(hass, "light", "turn_on")
    light_off = async_mock_service(hass, "light", "turn_off")
    acc._set_fan({"Active": 1, "RotationSpeed": 75})
    acc._set_light({"Brightness": 20})
    acc._set_light({"On": False})
    await hass.async_block_till_done()
    assert set_pct[0].data == {"entity_id": "fan.ceiling", "percentage": 75}
    assert light_on[0].data == {"entity_id": "light.ceiling", "brightness_pct": 20}
    assert len(light_off) == 1


def test_press_map() -> None:
    """Common event vocabularies map onto HomeKit presses."""
    hue = ["initial_press", "repeat", "short_release", "long_press", "long_release"]
    assert press_map(hue) == {"short_release": 0, "long_press": 2}
    assert press_map(["single", "double", "long"]) == {
        "single": 0,
        "double": 1,
        "long": 2,
    }
    assert press_map(["multi_press_1", "multi_press_2", "long_press"]) == {
        "multi_press_1": 0,
        "multi_press_2": 1,
        "long_press": 2,
    }
    # Without a release event, the start of a press counts as a single press.
    assert press_map(["initial_press", "long_release"]) == {
        "initial_press": 0,
        "long_release": 2,
    }
    assert press_map(["toggle"]) == {"toggle": 0}
    assert press_map(None) is None


async def test_buttons(hass: HomeAssistant) -> None:
    """Each new event notifies HomeKit, including repeats of the same press."""
    types = {"event_types": ["single", "double", "long"]}
    hass.states.async_set(
        "event.top", "2026-10-06T10:00:00", {**types, "event_type": "single"}
    )
    hass.states.async_set("event.bottom", "unknown", types)
    entry = await setup_accessory(
        hass, "buttons", "Remote", events=["event.top", "event.bottom"]
    )
    acc = server(hass, entry.entry_id).accessory
    services = services_by_type(acc)
    switches = services["StatelessProgrammableSwitch"]
    assert [char(s, "ServiceLabelIndex").value for s in switches] == [1, 2]
    assert "ServiceLabel" in services

    sent: list[int | None] = []
    with patch.object(
        type(char(switches[0], "ProgrammableSwitchEvent")),
        "notify",
        autospec=True,
        side_effect=lambda self, *args, **kwargs: sent.append(self.value),
    ) as notify:
        for when, event_type in (
            ("2026-10-06T10:00:01", "double"),
            ("2026-10-06T10:00:02", "double"),
            ("2026-10-06T10:00:03", "unrecognized"),
        ):
            hass.states.async_set(
                "event.top", when, {**types, "event_type": event_type}
            )
            await hass.async_block_till_done()
        # Coming back from unavailable with the same timestamp isn't a press.
        hass.states.async_set("event.top", "unavailable", types)
        hass.states.async_set(
            "event.top", "2026-10-06T10:00:03", {**types, "event_type": "single"}
        )
        await hass.async_block_till_done()
    assert notify.call_count == 2
    assert sent == [1, 1]  # double press, twice
    # Reads between events return null, as HomeKit expects.
    assert char(switches[0], "ProgrammableSwitchEvent").value is None


async def test_multi_sensor(hass: HomeAssistant) -> None:
    """Configured sensors become linked services; battery flags low."""
    hass.states.async_set("binary_sensor.motion", "on")
    hass.states.async_set("binary_sensor.door", "on")
    hass.states.async_set("sensor.temp", "21.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.lux", "0")
    hass.states.async_set("sensor.battery", "15")
    entry = await setup_accessory(
        hass,
        "multi_sensor",
        "Hallway",
        motion_sensor="binary_sensor.motion",
        contact_sensor="binary_sensor.door",
        temperature_sensor="sensor.temp",
        illuminance_sensor="sensor.lux",
        battery_sensor="sensor.battery",
    )
    services = services_by_type(server(hass, entry.entry_id).accessory)
    motion = services["MotionSensor"][0]
    assert motion.is_primary_service
    assert char(motion, "MotionDetected").value is True
    assert char(services["ContactSensor"][0], "ContactSensorState").value == 1
    assert char(services["TemperatureSensor"][0], "CurrentTemperature").value == 21.5
    assert char(services["LightSensor"][0], "CurrentAmbientLightLevel").value == 0.0001
    battery = services["BatteryService"][0]
    assert char(battery, "BatteryLevel").value == 15
    assert char(battery, "StatusLowBattery").value == 1
    assert "HumiditySensor" not in services


async def test_air_quality_monitor(hass: HomeAssistant) -> None:
    """Overall quality is the worst pollutant; CO₂ gets its own service."""
    hass.states.async_set("sensor.pm25", "8")
    hass.states.async_set("sensor.voc", "600")
    hass.states.async_set("sensor.co2", "1200")
    entry = await setup_accessory(
        hass,
        "air_quality",
        "Office Air",
        pm25_sensor="sensor.pm25",
        voc_sensor="sensor.voc",
        co2_sensor="sensor.co2",
    )
    services = services_by_type(server(hass, entry.entry_id).accessory)
    air = services["AirQualitySensor"][0]
    assert char(air, "PM2.5Density").value == 8
    assert char(air, "VOCDensity").value == 600
    assert char(air, "AirQuality").value == 3  # VOC 600 and CO₂ 1200 are "fair"
    co2 = services["CarbonDioxideSensor"][0]
    assert char(co2, "CarbonDioxideDetected").value == 1

    hass.states.async_set("sensor.voc", "100")
    hass.states.async_set("sensor.co2", "500")
    await hass.async_block_till_done()
    assert char(air, "AirQuality").value == 1
    assert char(co2, "CarbonDioxideDetected").value == 0

    hass.states.async_set("sensor.pm25", "unavailable")
    hass.states.async_set("sensor.voc", "unavailable")
    hass.states.async_set("sensor.co2", "unavailable")
    await hass.async_block_till_done()
    assert char(air, "AirQuality").value == 0  # unknown


async def test_power_strip(hass: HomeAssistant) -> None:
    """Each switch becomes a numbered outlet."""
    hass.states.async_set("switch.one", "on", {"friendly_name": "Lamp"})
    hass.states.async_set("input_boolean.two", "off", {"friendly_name": "Heater"})
    entry = await setup_accessory(
        hass, "power_strip", "Desk", outlets=["switch.one", "input_boolean.two"]
    )
    acc = server(hass, entry.entry_id).accessory
    outlets = services_by_type(acc)["Outlet"]
    assert [char(o, "Name").value for o in outlets] == ["Lamp", "Heater"]
    assert [char(o, "On").value for o in outlets] == [True, False]
    assert [char(o, "ServiceLabelIndex").value for o in outlets] == [1, 2]

    turn_on = async_mock_service(hass, "input_boolean", "turn_on")
    char(outlets[1], "On").client_update_value(True)
    await hass.async_block_till_done()
    assert turn_on[0].data == {"entity_id": "input_boolean.two"}


def test_split_buttons() -> None:
    """Multi-button entities are split by button; single buttons are not."""
    from custom_components.homekit_extended.accessories.buttons import (
        button_key,
        split_buttons,
    )

    assert button_key("button_1_single") == "button_1"
    assert button_key("single_left") == "left"
    assert button_key("2_multi_press_2") == "2"
    assert button_key("short_release") == ""
    hue = ["initial_press", "repeat", "short_release", "long_press", "long_release"]
    assert split_buttons(hue) == {"": hue}
    assert split_buttons(["single", "double", "hold"]) == {
        "": ["single", "double", "hold"]
    }
    assert split_buttons(
        ["button_1_single", "button_1_hold", "button_2_single", "button_2_double"]
    ) == {
        "button_1": ["button_1_single", "button_1_hold"],
        "button_2": ["button_2_single", "button_2_double"],
    }
    assert split_buttons(["on", "off"]) == {"on": ["on"], "off": ["off"]}
    assert split_buttons(None) == {"": []}


async def test_multi_button_entity(hass: HomeAssistant) -> None:
    """One event entity with several buttons becomes several HomeKit buttons."""
    types = {
        "event_types": [
            "button_1_single",
            "button_1_double",
            "button_1_hold",
            "button_2_single",
            "button_2_hold",
        ],
        "friendly_name": "Scene Controller",
    }
    hass.states.async_set("event.remote", "2026-10-06T10:00:00", types)
    entry = await setup_accessory(hass, "buttons", "Remote", events=["event.remote"])
    acc = server(hass, entry.entry_id).accessory
    switches = services_by_type(acc)["StatelessProgrammableSwitch"]
    assert [char(s, "Name").value for s in switches] == [
        "Scene Controller Button 1",
        "Scene Controller Button 2",
    ]
    assert [char(s, "ServiceLabelIndex").value for s in switches] == [1, 2]

    sent: list[tuple[str, int]] = []
    names = {
        id(char(s, "ProgrammableSwitchEvent")): char(s, "Name").value for s in switches
    }
    with patch.object(
        type(char(switches[0], "ProgrammableSwitchEvent")),
        "notify",
        autospec=True,
        side_effect=lambda self, *a, **k: sent.append((names[id(self)], self.value)),
    ):
        for when, event_type in (
            ("2026-10-06T10:00:01", "button_2_hold"),
            ("2026-10-06T10:00:02", "button_1_double"),
            ("2026-10-06T10:00:03", "button_3_single"),  # unknown button
        ):
            hass.states.async_set(
                "event.remote", when, {**types, "event_type": event_type}
            )
            await hass.async_block_till_done()
    assert sent == [
        ("Scene Controller Button 2", 2),
        ("Scene Controller Button 1", 1),
    ]
