"""Lock platform for FERMAX DuoxMe."""
import asyncio
import logging
from typing import Any, Dict, List

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, INTEGRATION_NAME
from .push import FermaxPushListener

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the FERMAX DuoxMe lock platform."""
    listener: FermaxPushListener = hass.data[DOMAIN][entry.entry_id]
    
    # Wait for the listener to be ready and have pairings
    await listener.ready_event.wait()
    
    locks: List[FermaxDoorLock] = []
    for pairing in listener.pairings:
        device_id = pairing.get("deviceId")
        if not device_id:
            continue
            
        for door_key, door_info in pairing.get("accessDoorMap", {}).items():
            if door_info.get("visible"):
                locks.append(
                    FermaxDoorLock(
                        listener=listener,
                        entry=entry,
                        device_id=device_id,
                        door_key=door_key,
                        door_info=door_info,
                    )
                )
    
    async_add_entities(locks)


class FermaxDoorLock(LockEntity):
    """Representation of a Fermax door lock."""

    _attr_is_locked = True

    def __init__(
        self,
        listener: FermaxPushListener,
        entry: ConfigEntry,
        device_id: str,
        door_key: str,
        door_info: Dict[str, Any],
    ):
        """Initialize the lock."""
        self._listener = listener
        self._api = listener._api
        self._device_id = device_id
        self._access_id = door_info.get("accessId")
        
        self._attr_name = door_info.get("title", f"Door {door_key}")
        self._attr_unique_id = f"{device_id}_{door_key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": INTEGRATION_NAME,
            "manufacturer": "FERMAX",
        }

    @property
    def is_locking(self) -> bool | None:
        """Return true if lock is locking."""
        return False

    @property
    def is_unlocking(self) -> bool | None:
        """Return true if lock is unlocking."""
        return False

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the door."""
        _LOGGER.debug("Unlocking door: %s", self.name)
        
        await self._listener._ensure_valid_token()
        access_token = self._listener._oauth_token["access_token"]
        
        success = await self._api.async_open_door(
            access_token, self._device_id, self._access_id
        )
        
        if success:
            self._attr_is_locked = False
            self.async_write_ha_state()
            
            # After 2 seconds, lock it again
            await asyncio.sleep(2)
            
            self._attr_is_locked = True
            self.async_write_ha_state()
        else:
            _LOGGER.error("Failed to unlock door: %s", self.name)

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the door."""
        # The Fermax API only supports unlocking
        _LOGGER.warning("Locking is not supported for Fermax doors.")
