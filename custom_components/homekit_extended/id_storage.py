"""Stable HomeKit instance and accessory IDs.

Apple Home stores rooms, names and automations against these numbers, so a
paired accessory must keep them across restarts. They live in this
integration's own files, never in core HomeKit's:

- ``.storage/homekit_extended.<entry_id>.iids`` (storage version 2)
- ``.storage/homekit_extended.<entry_id>.aids`` (storage version 1)

The on-disk document is the one written since IDs were first stored: a Home
Assistant storage file whose ``data.allocations`` map is read back as-is.
Loading that file is what keeps an accessory paired before 3.0.0 from needing
to be paired again. New IDs are handed out the same way those files were
filled, so a file that is missing a key still grows the way it used to.
"""

from __future__ import annotations

import random
from typing import Any
from uuid import UUID

from pyhap.characteristic import Characteristic
from pyhap.iid_manager import IIDManager
from pyhap.service import Service
from pyhap.util import uuid_to_hap_type

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN

# Same versions as the files already on disk. Raising either one would make
# Home Assistant treat the file as needing a migration.
IID_STORAGE_VERSION = 2
AID_STORAGE_VERSION = 1
_SAVE_DELAY = 2

_ALLOCATIONS = "allocations"
# HAP type of Accessory Information. Its service is always instance 1.
_ACCESSORY_INFORMATION = "3E"

# Accessory id 0 is invalid and 1 is the bridge or the standalone accessory.
_INVALID_AIDS = (0, 1)
_AID_MIN = 2
_AID_MAX = 18446744073709551615


def iid_storage_key(entry_id: str) -> str:
    """Storage key holding an entry's instance IDs."""
    return f"{DOMAIN}.{entry_id}.iids"


def aid_storage_key(entry_id: str) -> str:
    """Storage key holding the accessory IDs of an entry's zone accessories."""
    return f"{DOMAIN}.{entry_id}.aids"


def _fnv1a_32(text: str) -> int:
    """32-bit FNV-1a of a string.

    This is the hash the previous accessory-ID store used for a new zone, so
    a zone whose file has no row yet gets the same id it would have then.
    """
    value = 2166136261
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 16777619) & 0xFFFFFFFF
    return value


def _allocation_key(
    service_uuid: UUID,
    service_unique_id: str | None,
    char_uuid: UUID | None,
    char_unique_id: str | None,
) -> str:
    """Key under which an instance ID is stored.

    The shape is ``<service hap type>_<service unique id>_<char hap type>_<char
    unique id>``, which is what existing ``.iids`` files contain.
    """
    service_type = uuid_to_hap_type(service_uuid)
    char_type = uuid_to_hap_type(char_uuid) if char_uuid else ""
    return (
        f"{service_type}_{service_unique_id or ''}_{char_type}_{char_unique_id or ''}"
    )


class IidAllocation:
    """Instance IDs for one entry, loaded from and saved to its ``.iids`` file."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Prepare an empty allocation. Call ``async_initialize`` before use."""
        self.hass = hass
        self._store = Store[dict[str, Any]](
            hass, IID_STORAGE_VERSION, iid_storage_key(entry_id)
        )
        # aid string -> allocation key -> iid
        self._allocations: dict[str, dict[str, int]] = {}
        # aid string -> iids already handed out, in ascending order
        self._used: dict[str, list[int]] = {}

    async def async_initialize(self) -> None:
        """Load the on-disk allocations. A missing file starts empty."""
        raw = await self._store.async_load()
        allocations = raw.get(_ALLOCATIONS, {}) if isinstance(raw, dict) else {}
        self._allocations = {
            str(aid): {str(key): int(iid) for key, iid in rows.items()}
            for aid, rows in allocations.items()
        }
        self._used = {
            aid: sorted(rows.values()) for aid, rows in self._allocations.items()
        }

    def get_or_allocate_iid(
        self,
        aid: int | None,
        service_uuid: UUID,
        service_unique_id: str | None,
        char_uuid: UUID | None,
        char_unique_id: str | None,
    ) -> int:
        """Return the stored instance ID, or the next one in add order.

        The Accessory Information service is always 1, and it is not written
        down: that is how the previous store treated it, and existing files
        rely on it. Everything else keeps the id it was given the first time
        the service or characteristic was added.
        """
        service_type = uuid_to_hap_type(service_uuid)
        if service_type == _ACCESSORY_INFORMATION and char_uuid is None:
            return 1
        aid_str = str(aid)
        key = _allocation_key(
            service_uuid, service_unique_id, char_uuid, char_unique_id
        )
        rows = self._allocations.setdefault(aid_str, {})
        if key in rows:
            return rows[key]
        used = self._used.setdefault(aid_str, [1])
        nxt = used[-1] + 1 if used else 2
        rows[key] = nxt
        used.append(nxt)
        self._async_schedule_save()
        return nxt

    @callback
    def _async_schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, _SAVE_DELAY)

    async def async_save(self) -> None:
        """Write the allocations now. Called when the accessory stops."""
        await self._store.async_save(self._data_to_save())

    @callback
    def _data_to_save(self) -> dict[str, dict[str, dict[str, int]]]:
        return {_ALLOCATIONS: self._allocations}


class AidAllocation:
    """Accessory IDs for one entry's zone accessories.

    Used when an irrigation system publishes each zone as its own accessory
    inside this entry's bridge. The key is the valve's entity id, which is
    what existing ``.aids`` files use.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Prepare an empty allocation. Call ``async_initialize`` before use."""
        self.hass = hass
        self._store = Store[dict[str, Any]](
            hass, AID_STORAGE_VERSION, aid_storage_key(entry_id)
        )
        self._allocations: dict[str, int] = {}

    async def async_initialize(self) -> None:
        """Load the on-disk allocations. A missing file starts empty."""
        raw = await self._store.async_load()
        allocations = raw.get(_ALLOCATIONS, {}) if isinstance(raw, dict) else {}
        self._allocations = {str(key): int(aid) for key, aid in allocations.items()}

    def aid_for(self, key: str) -> int:
        """Return the stored accessory ID for a zone, allocating one if needed."""
        if key in self._allocations:
            return self._allocations[key]
        used = set(self._allocations.values())
        for candidate in _aid_candidates(key):
            if candidate in _INVALID_AIDS or candidate in used:
                continue
            self._allocations[key] = candidate
            self._async_schedule_save()
            return candidate
        raise ValueError(f"Unable to allocate an accessory ID for {key}")

    @callback
    def _async_schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, _SAVE_DELAY)

    async def async_save(self) -> None:
        """Write the allocations now. Called when the accessory stops."""
        await self._store.async_save(self._data_to_save())

    @callback
    def _data_to_save(self) -> dict[str, dict[str, int]]:
        return {_ALLOCATIONS: self._allocations}


def _aid_candidates(key: str):
    """IDs to try for a new zone: the old hash, then a few random ones."""
    yield _fnv1a_32(key)
    for _ in range(5):
        yield random.randrange(_AID_MIN, _AID_MAX)


class StableIIDManager(IIDManager):
    """pyhap IID manager that reads and writes :class:`IidAllocation`."""

    def __init__(self, storage: IidAllocation) -> None:
        """Remember the entry's instance-ID store."""
        super().__init__()
        self._storage = storage

    def get_iid_for_obj(self, obj: Service | Characteristic) -> int:
        """Return the stable instance ID for a service or characteristic."""
        aid = obj.broker.aid
        if isinstance(obj, Characteristic):
            service = obj.service
            iid = self._storage.get_or_allocate_iid(
                aid, service.type_id, service.unique_id, obj.type_id, obj.unique_id
            )
        else:
            iid = self._storage.get_or_allocate_iid(
                aid, obj.type_id, obj.unique_id, None, None
            )
        if iid in self.objs:
            raise RuntimeError(
                f"Cannot assign IID {iid} to {obj} as it is already in use by:"
                f" {self.objs[iid]}"
            )
        return iid
