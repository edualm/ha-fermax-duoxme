"""Config flow for FERMAX DuoxMe integration."""
import logging
from typing import Any, Dict

import voluptuous as vol
from aiohttp import ClientResponseError

from homeassistant import config_entries
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_FCM_API_KEY,
    CONF_FCM_PROJECT_ID,
    CONF_FCM_GCM_SENDER_ID,
    CONF_FCM_GMS_APP_ID,
    CONF_FCM_ANDROID_PACKAGE_NAME,
)
from .api import FermaxApi

_LOGGER = logging.getLogger(__name__)

class FermaxDuoxmeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for FERMAX DuoxMe."""

    VERSION = 1
    
    def __init__(self) -> None:
        """Initialize the config flow."""
        self.fermax_data: Dict[str, Any] = {}

    async def async_step_user(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step (Fermax credentials)."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = FermaxApi(session)

            try:
                # Test credentials by logging in
                await api.authenticate_with_password(
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    user_input[CONF_CLIENT_ID],
                    user_input[CONF_CLIENT_SECRET],
                )
            except ClientResponseError as e:
                _LOGGER.error("Authentication failed: %s", e)
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("An unknown error occurred during authentication.")
                errors["base"] = "unknown"
            else:
                # Store the valid credentials and move to the next step
                self.fermax_data = user_input
                return await self.async_step_fcm()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_CLIENT_ID): str,
                    vol.Required(CONF_CLIENT_SECRET): str,
                }
            ),
            errors=errors,
        )

    async def async_step_fcm(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the FCM configuration step."""
        if user_input is not None:
            # Combine Fermax creds with FCM creds and create the entry
            config_data = {**self.fermax_data, **user_input}
            
            await self.async_set_unique_id(config_data[CONF_USERNAME].lower())
            self._abort_if_unique_id_configured()
            
            return self.async_create_entry(
                title=config_data[CONF_USERNAME], data=config_data
            )

        return self.async_show_form(
            step_id="fcm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FCM_API_KEY): str,
                    vol.Required(CONF_FCM_PROJECT_ID): str,
                    vol.Required(CONF_FCM_GCM_SENDER_ID): str,
                    vol.Required(CONF_FCM_GMS_APP_ID): str,
                    vol.Required(CONF_FCM_ANDROID_PACKAGE_NAME): str,
                }
            ),
        )
