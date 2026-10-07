"""A "Paired" sensor and a device for each HomeKit Extended accessory."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import HomeKitExtendedConfigEntry
from .const import DOMAIN, MANUFACTURER, SIGNAL_PAIRING_CHANGED, VERSION


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HomeKitExtendedConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the Paired sensor."""
    async_add_entities([PairedSensor(entry)])


class PairedSensor(BinarySensorEntity):
    """On while at least one Apple Home controller is paired."""

    _attr_has_entity_name = True
    _attr_translation_key = "paired"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: HomeKitExtendedConfigEntry) -> None:
        """Initialize the sensor."""
        self._entry = entry
        server = entry.runtime_data
        # Core HomeKit doesn't announce pairing changes, so check now and then.
        self._attr_should_poll = getattr(server, "bridged", False)
        self._attr_unique_id = f"{entry.entry_id}_paired"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=server.accessory_type.model,
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        """Return true when paired."""
        return self._entry.runtime_data.paired

    @property
    def extra_state_attributes(self) -> dict[str, int | str]:
        """Expose the HAP port, or the bridge, for troubleshooting."""
        server = self._entry.runtime_data
        if getattr(server, "bridged", False):
            return {"bridge": server.bridge_name}
        return {"port": server.port}

    async def async_added_to_hass(self) -> None:
        """Update when pairing changes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_PAIRING_CHANGED.format(self._entry.entry_id),
                self._async_changed,
            )
        )

    @callback
    def _async_changed(self) -> None:
        self.async_write_ha_state()
