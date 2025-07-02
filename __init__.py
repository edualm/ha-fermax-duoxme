"""The FERMAX DuoxMe integration."""
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PLATFORMS
from .push import FermaxPushListener

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FERMAX DuoxMe from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    
    # Create and start the push notification listener
    listener = FermaxPushListener(hass, session, entry)
    await listener.start()
    
    hass.data[DOMAIN][entry.entry_id] = listener

    # Set up the platforms (camera, binary_sensor)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        # Stop the listener and clean up
        listener: FermaxPushListener = hass.data[DOMAIN].pop(entry.entry_id)
        await listener.stop()
        
    return unload_ok
