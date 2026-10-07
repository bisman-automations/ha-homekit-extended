"""HAP-python driver lifecycle shared by every HomeKit Extended accessory."""

from __future__ import annotations

from collections.abc import Callable
import logging
from pathlib import Path
import re
from typing import Any

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from zeroconf import IPVersion, NonUniqueNameException, ServiceInfo

from homeassistant.components import network
from homeassistant.components.homekit.iidmanager import (
    IID_MANAGER_STORAGE_VERSION,
    AccessoryIIDStorage,
    IIDStorage,
)
from homeassistant.components.zeroconf import async_get_async_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .accessories import ACCESSORY_TYPES
from .const import CONF_ACCESSORY_TYPE, CONF_PIN, CONF_PLAIN_NAME, CONF_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Listen on every IPv4 interface; advertise the addresses HA's network settings allow.
LISTEN_ADDRESS = "0.0.0.0"


def persist_path(hass: HomeAssistant, entry: ConfigEntry) -> Path:
    """Return where HAP-python keeps pairing state for an entry."""
    return Path(hass.config.path(f".storage/{DOMAIN}.{entry.entry_id}.state"))


def iid_storage_key(entry_id: str) -> str:
    """Storage key holding an entry's instance IDs."""
    return f"{DOMAIN}.{entry_id}.iids"


class ExtendedIIDStorage(AccessoryIIDStorage):
    """Core HomeKit's stable instance-ID storage, kept under our own key.

    A fresh store hands out IDs in the order services are added, exactly like
    HAP-python's default, so accessories paired before IDs were stored keep
    the ones Apple Home already knows.
    """

    async def async_initialize(self) -> None:
        """Load stored IDs from this integration's storage file."""
        self.store = IIDStorage(
            self.hass, IID_MANAGER_STORAGE_VERSION, iid_storage_key(self.entry_id)
        )
        if raw := await self.store.async_load():
            self.allocations = raw.get("allocations", {})
            for aid, allocations in self.allocations.items():
                self.allocated_iids[aid] = sorted(allocations.values())


# pyhap names the service "<name> <last 6 of the accessory id>._hap._tcp.local."
ID_SUFFIX_RE = re.compile(r" [0-9A-Fa-f]{6}(\._hap\._tcp\.local\.)$")


class PlainNameAdvertiser:
    """Wrap the zeroconf instance to advertise "<name>" instead of "<name> 8D111D".

    pyhap always adds part of the accessory id to the advertised name so two
    accessories never clash. This drops it, and quietly falls back to the
    suffixed name if something else on the network already uses the plain one.
    """

    def __init__(self, inner: Any) -> None:
        """Wrap an AsyncZeroconf instance."""
        self._inner = inner
        self._use_plain = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _info(self, info: ServiceInfo) -> ServiceInfo:
        if not self._use_plain or not ID_SUFFIX_RE.search(info.name):
            return info
        return ServiceInfo(
            info.type,
            ID_SUFFIX_RE.sub(r"\1", info.name),
            port=info.port,
            weight=info.weight,
            priority=info.priority,
            properties=info.properties,
            server=info.server,
            addresses=info.addresses_by_version(IPVersion.All),
        )

    async def async_register_service(self, info: ServiceInfo, **kwargs: Any) -> Any:
        """Register under the plain name, falling back to pyhap's name."""
        if self._use_plain:
            try:
                task = await self._inner.async_register_service(
                    self._info(info), **kwargs
                )
                await task
            except NonUniqueNameException:
                _LOGGER.warning(
                    "'%s' is already advertised on the network; using '%s' instead",
                    self._info(info).name,
                    info.name,
                )
                self._use_plain = False
            else:
                return task
        return await self._inner.async_register_service(info, **kwargs)

    async def async_update_service(self, info: ServiceInfo, **kwargs: Any) -> Any:
        """Update using whichever name was registered."""
        return await self._inner.async_update_service(self._info(info), **kwargs)

    async def async_unregister_service(self, info: ServiceInfo, **kwargs: Any) -> Any:
        """Unregister using whichever name was registered."""
        return await self._inner.async_unregister_service(self._info(info), **kwargs)


class ExtendedDriver(AccessoryDriver):
    """AccessoryDriver that reports when a controller pairs or unpairs."""

    def __init__(
        self, *args: Any, on_pairing_changed: Callable[[], None], **kwargs: Any
    ) -> None:
        """Initialize the driver."""
        super().__init__(*args, **kwargs)
        self._on_pairing_changed = on_pairing_changed

    def finish_pair(self) -> None:
        """Run after every pair and unpair."""
        super().finish_pair()
        self._on_pairing_changed()


class HomeKitAccessoryServer:
    """Publish one standalone HomeKit accessory on its own port and pairing."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        on_pairing_changed: Callable[[], None] = lambda: None,
    ) -> None:
        """Initialize the server."""
        self.hass = hass
        self.entry = entry
        self.accessory_type = ACCESSORY_TYPES[entry.data[CONF_ACCESSORY_TYPE]]
        self._on_pairing_changed = on_pairing_changed
        self.driver: ExtendedDriver | None = None
        self.accessory: Accessory | None = None
        self.iid_storage: ExtendedIIDStorage | None = None
        self._published = self._snapshot()

    def _snapshot(self) -> tuple[str, dict[str, Any]]:
        return self.entry.title, {**self.entry.data, **self.entry.options}

    def async_apply_in_place(self) -> bool:
        """Apply an entry update without re-publishing, when HomeKit allows it.

        Accessory info, run times and enabled zones are just characteristic
        values, so they update live; anything else changes the structure.
        """
        from .accessories.base import IN_PLACE_KEYS

        title, config = self._snapshot()
        old_title, old_config = self._published

        def structural(values: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in values.items() if k not in IN_PLACE_KEYS}

        if title != old_title or structural(config) != structural(old_config):
            return False
        if config != old_config:
            apply = getattr(self.accessory, "apply_in_place", None)
            if apply is None:
                return False
            apply(config)
        self._published = (title, config)
        return True

    @property
    def port(self) -> int:
        """Return the configured HAP port."""
        return int({**self.entry.data, **self.entry.options}[CONF_PORT])

    @property
    def pin(self) -> str:
        """Return the configured setup code."""
        return str({**self.entry.data, **self.entry.options}[CONF_PIN])

    @property
    def plain_name(self) -> bool:
        """Return true to advertise without pyhap's ID suffix."""
        return bool({**self.entry.data, **self.entry.options}.get(CONF_PLAIN_NAME))

    @property
    def paired(self) -> bool:
        """Return true if at least one Apple Home controller is paired."""
        return bool(self.driver and self.driver.state.paired)

    @property
    def setup_uri(self) -> str | None:
        """Return the X-HM:// payload encoded in the pairing QR code."""
        return self.accessory.xhm_uri() if self.accessory is not None else None

    async def async_start(self) -> None:
        """Create the driver and accessory, then start advertising."""
        async_zeroconf = await async_get_async_instance(self.hass)
        advertised = await network.async_get_announce_addresses(self.hass)
        persist_file = persist_path(self.hass, self.entry)
        self.iid_storage = ExtendedIIDStorage(self.hass, self.entry.entry_id)
        await self.iid_storage.async_initialize()

        def _changed() -> None:
            self.hass.loop.call_soon_threadsafe(self._on_pairing_changed)

        def _create_driver() -> ExtendedDriver:
            # The driver reads (or first writes) its persist file while initializing.
            persist_file.parent.mkdir(parents=True, exist_ok=True)
            driver = ExtendedDriver(
                address=LISTEN_ADDRESS,
                advertised_address=advertised or None,
                port=self.port,
                persist_file=str(persist_file),
                pincode=self.pin.encode(),
                loop=self.hass.loop,
                async_zeroconf_instance=async_zeroconf,
                on_pairing_changed=_changed,
            )
            if self.plain_name:
                driver.advertiser = PlainNameAdvertiser(async_zeroconf)
            return driver

        self.driver = await self.hass.async_add_executor_job(_create_driver)
        self.driver.iid_storage = self.iid_storage
        # Accessories subscribe to HA state events, so build them on the loop.
        self.accessory = self.accessory_type.factory(self.hass, self.driver, self.entry)
        await self.hass.async_add_executor_job(
            self.driver.add_accessory, self.accessory
        )
        try:
            await self.driver.async_start()
        except OSError:
            await self._async_abort_start()
            raise
        _LOGGER.info(
            "Publishing HomeKit accessory '%s' on port %s", self.entry.title, self.port
        )

    async def _async_abort_start(self) -> None:
        """Clean up after a failed start.

        The driver never registered its mDNS service, so its own async_stop
        would fail trying to unregister it and hide the original error.
        """
        accessory, self.accessory = self.accessory, None
        driver, self.driver = self.driver, None
        if accessory is not None:
            await accessory.async_stop()  # type: ignore[attr-defined]
        if (
            driver is not None
            and getattr(driver.http_server, "server", None) is not None
        ):
            driver.http_server.async_stop()

    async def async_stop(self) -> None:
        """Stop advertising and release resources."""
        accessory, self.accessory = self.accessory, None
        driver, self.driver = self.driver, None
        if accessory is not None:
            await accessory.async_stop()  # type: ignore[attr-defined]
        if driver is not None:
            await driver.async_stop()
        if self.iid_storage is not None:
            await self.iid_storage.async_save()
