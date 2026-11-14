"""Entities keep running when their entity ID is changed (Home Assistant 2026.11+)."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from unittest.mock import patch

import pytest

import custom_components.homekit_extended as package
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity

from .conftest import server, setup_accessory

ADD_REMOVE_METHODS = (
    "async_prepare_to_add_to_hass",
    "async_added_to_hass",
    "async_will_remove_from_hass",
)
# HAP accessories, not Home Assistant entities.
SKIPPED_PACKAGES = (f"{package.__name__}.accessories",)


def _entity_classes() -> list[type[Entity]]:
    """Every Entity subclass defined in the integration's importable modules."""
    classes: list[type[Entity]] = []
    for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
        if info.name.startswith(SKIPPED_PACKAGES):
            continue
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            continue
        classes.extend(
            cls
            for _, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, Entity) and cls.__module__ == module.__name__
        )
    return classes


def test_every_add_remove_class_handles_entity_id_changes() -> None:
    """A class with its own add or remove code also has its own rename hook."""
    classes = _entity_classes()
    assert classes, "no entity classes found"
    missing = sorted(
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in classes
        if any(method in cls.__dict__ for method in ADD_REMOVE_METHODS)
        and "async_entity_id_changed" not in cls.__dict__
    )
    assert not missing, f"missing async_entity_id_changed: {missing}"


@pytest.mark.skipif(
    not hasattr(Entity, "async_entity_id_changed"),
    reason="Entity ID changes are applied in place from Home Assistant 2026.11",
)
async def test_paired_sensor_follows_pairing_after_rename(hass: HomeAssistant) -> None:
    """The renamed Paired sensor still follows pairing changes."""
    entry = await setup_accessory(hass, "power_strip", "Desk", outlets=["switch.a"])
    old_id = "binary_sensor.desk_paired"
    new_id = "binary_sensor.desk_homekit_paired"
    assert hass.states.get(old_id).state == "off"

    er.async_get(hass).async_update_entity(old_id, new_entity_id=new_id)
    await hass.async_block_till_done()
    assert hass.states.get(old_id) is None
    assert hass.states.get(new_id).state == "off"

    srv = server(hass, entry.entry_id)
    srv.driver.state.paired_clients = {"controller": b"key"}
    with patch("pyhap.accessory_driver.AccessoryDriver.finish_pair"):
        srv.driver.finish_pair()
    await hass.async_block_till_done()
    assert hass.states.get(new_id).state == "on"
    assert hass.states.get(old_id) is None
