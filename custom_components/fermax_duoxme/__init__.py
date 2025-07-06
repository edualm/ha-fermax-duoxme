"""The FERMAX DuoxMe integration."""
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PLATFORMS, CONF_ENABLE_PUSH_NOTIFICATIONS
from .push import FermaxPushListener

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FERMAX DuoxMe from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    
    listener = FermaxPushListener(hass, session, entry)
    hass.data[DOMAIN][entry.entry_id] = listener

    enable_push = entry.options.get(CONF_ENABLE_PUSH_NOTIFICATIONS, True)

    if enable_push:
        _LOGGER.info("Push notifications enabled. Starting listener and setting up all platforms.")
        await listener.start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    else:
        _LOGGER.info("Push notifications disabled. Setting up lock platform only.")
        # Fetch the necessary data for the lock platform to initialize
        await listener.async_fetch_pairings()
        await hass.config_entries.async_forward_entry_setups(entry, ["lock"])

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading Fermax integration.")
    
    enable_push = entry.options.get(CONF_ENABLE_PUSH_NOTIFICATIONS, True)
    platforms_to_unload = PLATFORMS if enable_push else ["lock"]
    
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, platforms_to_unload
    )
    
    if unload_ok:
        listener: FermaxPushListener = hass.data[DOMAIN].pop(entry.entry_id)
        # Only stop the listener if it was started
        if enable_push:
            await listener.stop()
        
    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.info("Configuration options updated. Reloading integration.")
    await hass.config_entries.async_reload(entry.entry_id)
