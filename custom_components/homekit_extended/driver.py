"""HAP-python driver lifecycle shared by every HomeKit Extended accessory."""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver

from homeassistant.components import network
from homeassistant.components.zeroconf import async_get_async_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PIN, CONF_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Listen on every IPv4 interface; advertise the addresses HA's network settings allow.
LISTEN_ADDRESS = "0.0.0.0"


AccessoryFactory = Callable[[HomeAssistant, AccessoryDriver, ConfigEntry], Accessory]


def persist_path(hass: HomeAssistant, entry: ConfigEntry) -> Path:
    """Return where HAP-python keeps pairing state for an entry."""
    return Path(hass.config.path(f".storage/{DOMAIN}.{entry.entry_id}.state"))


class HomeKitAccessoryServer:
    """Publish one standalone HomeKit accessory on its own port and pairing."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        factory: AccessoryFactory,
    ) -> None:
        """Initialize the server."""
        self.hass = hass
        self.entry = entry
        self._factory = factory
        self.driver: AccessoryDriver | None = None
        self.accessory: Accessory | None = None

    @property
    def port(self) -> int:
        """Return the configured HAP port."""
        return int({**self.entry.data, **self.entry.options}[CONF_PORT])

    @property
    def pin(self) -> str:
        """Return the configured setup code."""
        return str({**self.entry.data, **self.entry.options}[CONF_PIN])

    async def async_start(self) -> None:
        """Create the driver and accessory, then start advertising."""
        async_zeroconf = await async_get_async_instance(self.hass)
        advertised = await network.async_get_announce_addresses(self.hass)
        persist_file = persist_path(self.hass, self.entry)

        def _create_driver() -> AccessoryDriver:
            # The driver reads (or first writes) its persist file while initializing.
            persist_file.parent.mkdir(parents=True, exist_ok=True)
            return AccessoryDriver(
                address=LISTEN_ADDRESS,
                advertised_address=advertised or None,
                port=self.port,
                persist_file=str(persist_file),
                pincode=self.pin.encode(),
                loop=self.hass.loop,
                async_zeroconf_instance=async_zeroconf,
            )

        self.driver = await self.hass.async_add_executor_job(_create_driver)
        # Accessories subscribe to HA state events, so build them on the loop.
        self.accessory = self._factory(self.hass, self.driver, self.entry)
        await self.hass.async_add_executor_job(
            self.driver.add_accessory, self.accessory
        )
        try:
            await self.driver.async_start()
        except OSError:
            await self.async_stop()
            raise

        _LOGGER.info(
            "Publishing HomeKit accessory '%s' on port %s",
            self.entry.title,
            self.port,
        )

    async def async_stop(self) -> None:
        """Stop advertising and release resources."""
        accessory, self.accessory = self.accessory, None
        driver, self.driver = self.driver, None
        if accessory is not None:
            await accessory.async_stop()  # type: ignore[attr-defined]
        if driver is not None:
            await driver.async_stop()
