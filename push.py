"""Handles push notifications from Fermax."""
import asyncio
import base64
import hashlib
import json
import logging
import time
from functools import partial
from threading import Thread
from typing import Any, Dict, List, Optional, Set

from aiohttp import ClientSession
from push_receiver import PushReceiver
from push_receiver.android_fcm_register import AndroidFCM

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .api import FermaxApi
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_EXPIRES_AT,
    CONF_FCM_ANDROID_PACKAGE_NAME,
    CONF_FCM_API_KEY,
    CONF_FCM_GCM_SENDER_ID,
    CONF_FCM_GMS_APP_ID,
    CONF_FCM_PROJECT_ID,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    API_REGISTER_FCM_URL,
    OAUTH_TOKEN_URL,
    SIGNAL_LISTENER_READY,
    SIGNAL_NOTIFICATION_RECEIVED,
    STORAGE_KEY_FCM,
    STORAGE_KEY_PERSISTENT_IDS,
    STORAGE_KEY_TOKEN,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

class FermaxPushListener:
    """Manages the connection and push notifications from Fermax."""

    def __init__(self, hass: HomeAssistant, session: ClientSession, entry: ConfigEntry):
        self.hass = hass
        self._session = session
        self._config = entry.data
        self._api = FermaxApi(session)
        self._oauth_token: Optional[Dict[str, Any]] = None
        self._fcm_credentials: Optional[Dict[str, Any]] = None
        self._processed_ids: Set[str] = set()
        self._device_id: Optional[str] = None
        self._listener_thread: Optional[Thread] = None
        self._receiver: Optional[PushReceiver] = None  # Store a reference to the receiver
        self.pairings: List[Dict[str, Any]] = []
        self.ready_event = asyncio.Event()

        self._token_store = Store(hass, STORAGE_VERSION, STORAGE_KEY_TOKEN)
        self._fcm_store = Store(hass, STORAGE_VERSION, STORAGE_KEY_FCM)
        self._id_store = Store(hass, STORAGE_VERSION, STORAGE_KEY_PERSISTENT_IDS)

    # --- Persistence Methods ---
    async def _save_token(self, token_data: dict):
        token_data[CONF_EXPIRES_AT] = time.time() + token_data.get("expires_in", 3600)
        self._oauth_token = token_data
        await self._token_store.async_save(token_data)

    async def _load_token(self):
        self._oauth_token = await self._token_store.async_load()

    async def _load_persistent_ids(self):
        ids = await self._id_store.async_load()
        if isinstance(ids, list):
            self._processed_ids = set(ids)

    async def _add_and_save_persistent_id(self, persistent_id: str):
        self._processed_ids.add(persistent_id)
        await self._id_store.async_save(list(self._processed_ids))

    # --- Authentication and API Methods ---
    def _get_auth_header(self) -> str:
        creds = f'{self._config[CONF_CLIENT_ID]}:{self._config[CONF_CLIENT_SECRET]}'
        return base64.b64encode(creds.encode("utf-8")).decode("utf-8")

    async def _authenticate_with_password(self):
        headers = {"Authorization": f"Basic {self._get_auth_header()}", "Content-Type": "application/x-www-form-urlencoded"}
        payload = {"grant_type": "password", "username": self._config[CONF_USERNAME], "password": self._config[CONF_PASSWORD]}
        _LOGGER.info("Authenticating with username and password...")
        async with self._session.post(OAUTH_TOKEN_URL, headers=headers, data=payload) as resp:
            resp.raise_for_status()
            await self._save_token(await resp.json())
            _LOGGER.info("Initial authentication successful.")

    async def _refresh_token(self):
        refresh_token = self._oauth_token.get(CONF_REFRESH_TOKEN)
        if not refresh_token: raise RuntimeError("No refresh token available.")
        _LOGGER.info("Access token expired. Refreshing...")
        headers = {"Authorization": f"Basic {self._get_auth_header()}", "Content-Type": "application/x-www-form-urlencoded"}
        payload = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        async with self._session.post(OAUTH_TOKEN_URL, headers=headers, data=payload) as resp:
            resp.raise_for_status()
            await self._save_token(await resp.json())
            _LOGGER.info("Token refreshed successfully.")

    async def _ensure_valid_token(self):
        if not self._oauth_token: await self._load_token()
        if not self._oauth_token:
            await self._authenticate_with_password()
            return
        if time.time() >= self._oauth_token.get(CONF_EXPIRES_AT, 0):
            try:
                await self._refresh_token()
            except Exception as e:
                _LOGGER.error("Failed to refresh token: %s. Re-authenticating.", e)
                await self._authenticate_with_password()

    async def _register_fcm_token(self, active: bool):
        """Registers or de-registers the FCM token with the Fermax server."""
        await self._ensure_valid_token()
        headers = {"Authorization": f"Bearer {self._oauth_token[CONF_ACCESS_TOKEN]}"}
        payload = {
            "token": self._device_id,
            "appVersion": "3.3.2",
            "locale": "en",
            "os": "Android",
            "osVersion": "Android 13",
            "active": active,
        }
        _LOGGER.info("Setting FCM token status to %s", "active" if active else "inactive")
        async with self._session.post(API_REGISTER_FCM_URL, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            _LOGGER.info("FCM token status updated successfully.")

    def _build_package_cert(self) -> str:
        sha = hashlib.sha512()
        sha.update(str(self._config[CONF_FCM_GCM_SENDER_ID]).encode("utf-8"))
        sha.update(self._config[CONF_FCM_GMS_APP_ID].encode("utf-8"))
        sha.update(self._config[CONF_FCM_API_KEY].encode("utf-8"))
        sha.update(self._config[CONF_FCM_PROJECT_ID].encode("utf-8"))
        sha.update(self._config[CONF_FCM_ANDROID_PACKAGE_NAME].encode("utf-8"))
        return sha.hexdigest()

    async def _get_or_register_fcm_credentials(self):
        self._fcm_credentials = await self._fcm_store.async_load()
        if not self._fcm_credentials:
            _LOGGER.info("No FCM credentials found. Registering with Google FCM...")
            fcm_config = {
                "api_key": self._config[CONF_FCM_API_KEY],
                "project_id": self._config[CONF_FCM_PROJECT_ID],
                "gcm_sender_id": self._config[CONF_FCM_GCM_SENDER_ID],
                "gms_app_id": self._config[CONF_FCM_GMS_APP_ID],
                "android_package_name": self._config[CONF_FCM_ANDROID_PACKAGE_NAME],
                "android_package_cert": self._build_package_cert(),
            }
            try:
                register_func = partial(AndroidFCM.register, **fcm_config)
                self._fcm_credentials = await self.hass.async_add_executor_job(register_func)
                await self._fcm_store.async_save(self._fcm_credentials)
            except Exception as e:
                if "PERMISSION_DENIED" in str(e):
                    _LOGGER.error(
                        "FCM registration failed: PERMISSION_DENIED. "
                        "Please check that your FCM API Key is correct and has the "
                        "'firebaseinstallations.installations.create' permission in your Google Cloud project."
                    )
                else:
                    _LOGGER.error("An unexpected error occurred during FCM registration: %s", e)
                raise

        self._device_id = self._fcm_credentials["fcm"]["token"]
        _LOGGER.info("FCM credentials ready. Device Token: %s...", self._device_id[:15])

    def _start_listener_thread_entry(self):
        """Entry point for the listener thread."""
        try:
            future = asyncio.run_coroutine_threadsafe(self._async_listener_setup(), self.hass.loop)
            future.result()

            # Store the receiver as an instance variable to prevent garbage collection
            self._receiver = PushReceiver(self._fcm_credentials, list(self._processed_ids))
            _LOGGER.info("Listening for incoming notifications...")
            self._receiver.listen(self._on_notification)
        except Exception:
            _LOGGER.exception("An error occurred in the listener thread")

    async def _async_listener_setup(self):
        """Asynchronous part of the listener setup."""
        await self._get_or_register_fcm_credentials()
        await self._load_persistent_ids()
        
        await self._ensure_valid_token()
        self.pairings = await self._api.async_get_pairings(self._oauth_token[CONF_ACCESS_TOKEN]) or []
        
        await self._register_fcm_token(True)
        self.ready_event.set()
        async_dispatcher_send(self.hass, SIGNAL_LISTENER_READY)
        _LOGGER.info("Fermax push listener is ready.")

    def _on_notification(self, _, notification: dict, data_message):
        """Callback for when a notification is received."""
        persistent_id = data_message.persistent_id
        
        _LOGGER.info("--- PUSH NOTIFICATION RECEIVED ---")
        _LOGGER.info("Persistent ID: %s", persistent_id)
        _LOGGER.info("Full Payload: %s", json.dumps(notification, indent=2))
        _LOGGER.info("------------------------------------")

        if persistent_id in self._processed_ids:
            _LOGGER.debug("Ignoring already processed notification ID: %s", persistent_id)
            return
        
        _LOGGER.info("Processing as a new notification.")
        
        if notification.get("SendAcknowledge") == "true":
            asyncio.run_coroutine_threadsafe(self._acknowledge_notification(persistent_id), self.hass.loop)

        asyncio.run_coroutine_threadsafe(self._dispatch_notification(notification), self.hass.loop)
        asyncio.run_coroutine_threadsafe(self._add_and_save_persistent_id(persistent_id), self.hass.loop)

    async def _acknowledge_notification(self, message_id: str):
        """Acknowledge a notification."""
        await self._ensure_valid_token()
        access_token = self._oauth_token["access_token"]
        await self._api.async_acknowledge_notification(access_token, message_id)

    async def _dispatch_notification(self, notification: dict):
        """Safely dispatch notification to HA event loop."""
        async_dispatcher_send(self.hass, SIGNAL_NOTIFICATION_RECEIVED, notification)

    async def start(self):
        """Starts the authentication and listening process."""
        await self._ensure_valid_token()
        self._listener_thread = Thread(target=self._start_listener_thread_entry, daemon=True)
        self._listener_thread.start()
        _LOGGER.info("Fermax push listener has been started.")

    async def stop(self):
        """Stops the listener gracefully."""
        _LOGGER.info("Stopping Fermax push listener...")
        if self._device_id:
            try:
                await self._register_fcm_token(False)
            except Exception as e:
                _LOGGER.error("Failed to de-register FCM token: %s", e)
