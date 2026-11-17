"""Config flow for FERMAX DuoxMe integration."""
from collections.abc import Mapping
import logging
from typing import Any, Dict

import voluptuous as vol
from aiohttp import ClientResponseError

from homeassistant import config_entries
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    DOMAIN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENABLE_LIVE_SNAPSHOT,
    CONF_ENABLE_PUSH_NOTIFICATIONS,
    CONF_LIVE_SNAPSHOT_BACKEND,
    CONF_UNLOCK_DURATION,
    DEFAULT_UNLOCK_DURATION,
    DEFAULT_LIVE_SNAPSHOT_BACKEND,
    MAX_UNLOCK_DURATION,
    MIN_UNLOCK_DURATION,
    LIVE_SNAPSHOT_BACKENDS,
    CONF_FCM_ANDROID_PACKAGE_NAME,
    CONF_FCM_API_KEY,
    CONF_FCM_GCM_SENDER_ID,
    CONF_FCM_GMS_APP_ID,
    CONF_FCM_PROJECT_ID,
)
from .api import FermaxApi

_LOGGER = logging.getLogger(__name__)

class FermaxDuoxmeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for FERMAX DuoxMe."""

    VERSION = 1
    
    def __init__(self) -> None:
        """Initialize the config flow."""
        self.fermax_data: Dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "OptionsFlowHandler":
        """Get the options flow for this handler."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step (Fermax credentials)."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = FermaxApi(session)

            try:
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
            description_placeholders={
                "docs_link": "https://github.com/edualm/ha-fermax-duoxme/blob/main/README.md"
            }
        )

    async def async_step_fcm(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the FCM configuration step."""
        if user_input is not None:
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
            description_placeholders={
                "docs_link": "https://github.com/edualm/ha-fermax-duoxme/blob/main/README.md"
            }
        )

    # FIX (6): re-authentication flow. Setup raises ConfigEntryAuthFailed when
    # FERMAX rejects the stored credentials (e.g. the password was changed);
    # Home Assistant then shows a notification that opens this step.
    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> FlowResult:
        """Start re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the new FERMAX password and validate it."""
        entry = self._get_reauth_entry()
        errors: Dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            api = FermaxApi(async_get_clientsession(self.hass))
            try:
                await api.authenticate_with_password(
                    data[CONF_USERNAME],
                    data[CONF_PASSWORD],
                    data[CONF_CLIENT_ID],
                    data[CONF_CLIENT_SECRET],
                )
            except ClientResponseError as e:
                _LOGGER.error("Re-authentication failed: %s", e)
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("An unknown error occurred during re-authentication.")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME]},
        )

class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle an options flow for FERMAX DuoxMe."""

    async def async_step_init(
        self, user_input: Dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            # Update the config entry with new options and trigger a reload
            return self.async_create_entry(title="", data=user_input)

        # Get the current value of the option, defaulting to True if not set
        enable_push = self.config_entry.options.get(CONF_ENABLE_PUSH_NOTIFICATIONS, True)
        enable_live_snapshot = self.config_entry.options.get(
            CONF_ENABLE_LIVE_SNAPSHOT, False
        )
        live_snapshot_backend = self.config_entry.options.get(
            CONF_LIVE_SNAPSHOT_BACKEND, DEFAULT_LIVE_SNAPSHOT_BACKEND
        )
        if live_snapshot_backend not in LIVE_SNAPSHOT_BACKENDS:
            live_snapshot_backend = DEFAULT_LIVE_SNAPSHOT_BACKEND
        unlock_duration = self.config_entry.options.get(
            CONF_UNLOCK_DURATION, DEFAULT_UNLOCK_DURATION
        )
        if (
            not isinstance(unlock_duration, int)
            or not MIN_UNLOCK_DURATION <= unlock_duration <= MAX_UNLOCK_DURATION
        ):
            unlock_duration = DEFAULT_UNLOCK_DURATION

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLE_PUSH_NOTIFICATIONS, default=enable_push
                    ): bool,
                    vol.Required(
                        CONF_ENABLE_LIVE_SNAPSHOT, default=enable_live_snapshot
                    ): bool,
                    vol.Required(
                        CONF_LIVE_SNAPSHOT_BACKEND, default=live_snapshot_backend
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=LIVE_SNAPSHOT_BACKENDS,
                            mode=SelectSelectorMode.LIST,
                            translation_key=CONF_LIVE_SNAPSHOT_BACKEND,
                        )
                    ),
                    vol.Required(
                        CONF_UNLOCK_DURATION, default=unlock_duration
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=MIN_UNLOCK_DURATION, max=MAX_UNLOCK_DURATION
                        ),
                    ),
                }
            ),
        )
