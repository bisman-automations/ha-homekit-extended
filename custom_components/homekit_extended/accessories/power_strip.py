"""Power strip: several switches as the outlets of one HomeKit accessory."""

from __future__ import annotations

from typing import Any

from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_OUTLET
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_ON
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er

from ..const import CONF_OUTLETS
from ..helpers import friendly_name
from .base import (
    IGNORED_STATES,
    STANDALONE_AID,
    AccessoryType,
    HomeAccessory,
    entity_field,
    entity_list,
    find_entities,
)

OUTLET_DOMAINS = ["switch", "input_boolean"]


def _schema() -> dict[vol.Marker, Any]:
    return {vol.Required(CONF_OUTLETS): entity_field(OUTLET_DOMAINS, multiple=True)}


def _detect(hass: HomeAssistant, entries: list[er.RegistryEntry]) -> dict[str, Any]:
    outlets = find_entities(entries, "switch", "outlet") or find_entities(
        entries, "switch"
    )
    return {CONF_OUTLETS: outlets} if outlets else {}


def _normalize(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    data[CONF_OUTLETS] = entity_list(data.get(CONF_OUTLETS))
    return data, {} if data[CONF_OUTLETS] else {CONF_OUTLETS: "no_outlets_selected"}


class PowerStripAccessory(HomeAccessory):
    """One numbered Outlet service per switch."""

    category = CATEGORY_OUTLET

    def __init__(
        self,
        hass: HomeAssistant,
        driver: AccessoryDriver,
        entry: ConfigEntry,
        aid: int = STANDALONE_AID,
    ) -> None:
        """Initialize the accessory."""
        super().__init__(hass, driver, entry, POWER_STRIP.model, aid)
        self.outlets = entity_list(self.data.get(CONF_OUTLETS))
        self._chars: dict[str, tuple[Characteristic, Characteristic]] = {}

        self.add_service_label()
        for index, entity_id in enumerate(self.outlets, start=1):
            service = self.add_named_service(
                "Outlet",
                friendly_name(hass, entity_id),
                ["On", "OutletInUse"],
                label_index=index,
                unique_id=entity_id,
            )
            if index == 1:
                service.is_primary_service = True
            on = service.configure_char(
                "On",
                value=False,
                setter_callback=lambda value, entity_id=entity_id: self._set_on(
                    entity_id, value
                ),
            )
            self._chars[entity_id] = (
                on,
                service.configure_char("OutletInUse", value=False),
            )

        self.track({entity_id: self._update for entity_id in self.outlets})

    def _set_on(self, entity_id: str, value: bool) -> None:
        domain = entity_id.split(".", 1)[0]
        self.call_service(
            domain, SERVICE_TURN_ON if value else SERVICE_TURN_OFF, entity_id
        )

    @callback
    def _update(self, state: State) -> None:
        if state.state in IGNORED_STATES or state.entity_id not in self._chars:
            return
        is_on = state.state == STATE_ON
        for char in self._chars[state.entity_id]:
            char.set_value(is_on)


POWER_STRIP = AccessoryType(
    key="power_strip",
    model="HomeKit Extended Power Strip",
    default_name="Power Strip",
    device_domains=("switch",),
    schema=_schema,
    detect=_detect,
    normalize=_normalize,
    factory=PowerStripAccessory,
    entity_keys=(CONF_OUTLETS,),
)
