"""The FERMAX DuoxMe integration."""
import asyncio
import logging

from aiohttp import ClientError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, PLATFORMS, CONF_ENABLE_PUSH_NOTIFICATIONS
from .push import (
    FermaxAuthError,
    FermaxFcmRegistrationError,
    FermaxPushListener,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FERMAX DuoxMe from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)

    listener = FermaxPushListener(hass, session, entry)

    enable_push = entry.options.get(CONF_ENABLE_PUSH_NOTIFICATIONS, True)

    # FIX (6): errors while connecting are mapped to Home Assistant's setup
    # exceptions. Network problems -> ConfigEntryNotReady (HA retries setup
    # automatically). Rejected credentials -> ConfigEntryAuthFailed (HA shows
    # a "re-authenticate" notification). Before, any error made setup fail
    # for good until a manual reload.
    try:
        if enable_push:
            _LOGGER.info("Push notifications enabled. Setting up all platforms.")
            await listener.async_setup()
        else:
            _LOGGER.info("Push notifications disabled. Setting up lock platform only.")
            # Fetch the necessary data for the lock platform to initialize
            await listener.async_fetch_pairings()
    except FermaxAuthError as err:
        await listener.stop()
        raise ConfigEntryAuthFailed(str(err)) from err
    except FermaxFcmRegistrationError as err:
        await listener.stop()
        raise ConfigEntryError(str(err)) from err
    except (ClientError, asyncio.TimeoutError, ConnectionError) as err:
        await listener.stop()
        raise ConfigEntryNotReady(f"Could not connect to FERMAX: {err}") from err

    if enable_push:
        listener.start()

    # FIX (3): remember which platforms were really loaded; unload uses this.
    listener.loaded_platforms = list(PLATFORMS) if enable_push else ["lock"]
    hass.data[DOMAIN][entry.entry_id] = listener
    await hass.config_entries.async_forward_entry_setups(entry, listener.loaded_platforms)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading Fermax integration.")

    listener: FermaxPushListener = hass.data[DOMAIN][entry.entry_id]

    # FIX (3): before, the platforms to unload were taken from the options. When
    # an option change triggers this reload the options are already the NEW
    # ones, so switching notifications off left the camera and ring sensor
    # loaded (and switching them on tried to unload platforms never loaded).
    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, listener.loaded_platforms
    )

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        # stop() only does work for what was actually started.
        await listener.stop()

    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    _LOGGER.info("Configuration options updated. Reloading integration.")
    await hass.config_entries.async_reload(entry.entry_id)
