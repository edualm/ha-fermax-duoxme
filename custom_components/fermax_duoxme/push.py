"""Handles push notifications from Fermax."""
import asyncio
import base64
from collections import deque
import hashlib
import json
import logging
import threading
import time
from functools import partial
from typing import Any, Callable, Deque, Dict, List, Optional
from urllib.parse import quote

from aiohttp import ClientSession
from push_receiver import PushReceiver
from push_receiver.android_fcm_register import AndroidFCM

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    EVENT_CALL,
    API_REGISTER_FCM_URL,
    API_UNREGISTER_FCM_URL,
    OAUTH_TOKEN_URL,
    SIGNAL_LISTENER_READY,
    SIGNAL_NOTIFICATION_RECEIVED,
    SIGNAL_PUSH_CONNECTION_CHANGED,
    NOTIFICATION_DEVICE_ID_KEY,
    NOTIFICATION_TYPE_CALL,
    NOTIFICATION_TYPE_CALL_ATTENDED,
    NOTIFICATION_TYPE_CALL_ENDED,
    NOTIFICATION_TYPE_KEY,
    STORAGE_KEY_FCM,
    STORAGE_KEY_PERSISTENT_IDS,
    STORAGE_KEY_TOKEN,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# FIX (token): renew the access token this many seconds before it expires, so
# a request never goes out with a token that expires on the way.
TOKEN_EXPIRY_MARGIN_SECS = 60

# FIX (processed IDs): only remember the most recent notification IDs. They are
# only needed to ignore re-deliveries of recent messages after a reconnect.
MAX_PERSISTENT_IDS = 100

# FIX (2): back-off between reconnection attempts of the push listener.
RECONNECT_MIN_DELAY_SECS = 5
RECONNECT_MAX_DELAY_SECS = 300
# A connection that lasted at least this long resets the back-off.
RECONNECT_STABLE_SECS = 300

# FIX (1): how long to wait for the listener thread to finish on stop.
THREAD_JOIN_TIMEOUT_SECS = 10


class FermaxAuthError(Exception):
    """Raised when FERMAX rejects the account credentials."""


class FermaxFcmRegistrationError(Exception):
    """Raised when FERMAX permanently rejects an FCM registration."""


class _ReceiverStopped(Exception):
    """Raised inside the receiver thread when the listener is being stopped."""


class _StoppablePushReceiver(PushReceiver):
    """PushReceiver that can be stopped from another thread.

    FIX (1): the library has no stop(): listen() loops forever, reconnects on
    its own (sleeping up to 5 minutes with time.sleep) and starts a
    non-daemon timer thread every hour. Its private methods are name-mangled,
    so they are overridden here as ``_PushReceiver__<name>``.
    """

    def __init__(
        self,
        credentials,
        received_persistent_ids,
        stop_event: threading.Event,
        on_connection_change: Callable[[bool], None],
    ):
        super().__init__(credentials, received_persistent_ids)
        self._stop_event = stop_event
        self._on_connection_change = on_connection_change
        self.socket = None
        self.checkin_thread: Optional[threading.Timer] = None

    def __del__(self):
        # The base __del__ fails if the receiver never connected.
        self.close()

    def _PushReceiver__login(self):
        if self._stop_event.is_set():
            raise _ReceiverStopped
        super()._PushReceiver__login()
        self._on_connection_change(True)

    def _PushReceiver__close_socket(self):
        try:
            super()._PushReceiver__close_socket()
        finally:
            self._on_connection_change(False)

    def _PushReceiver__reset(self):
        # Same as the library, but the wait between reconnections can be
        # interrupted by stop() instead of blocking the thread for minutes.
        wait = self.MIN_RESET_INTERVAL_SECS - (time.time() - self.last_reset)
        if wait > 0 and self._stop_event.wait(wait):
            raise _ReceiverStopped
        if self._stop_event.is_set():
            raise _ReceiverStopped
        self.last_reset = time.time()
        _LOGGER.debug("Re-establishing FCM connection")
        self._PushReceiver__close_socket()
        self._PushReceiver__login()

    def _PushReceiver__status_check(self):
        # Same as the library, but with a daemon timer that is not rescheduled
        # once the listener is stopped.
        if self._stop_event.is_set():
            return
        silent_for = time.time() - self.time_last_message_received
        if silent_for > self.MAX_SILENT_INTERVAL_SECS:
            _LOGGER.info("No FCM traffic for %.0fs. Resetting connection.", silent_for)
            self._PushReceiver__close_socket()
            silent_for = 0
        self.checkin_thread = threading.Timer(
            1 + self.MAX_SILENT_INTERVAL_SECS - silent_for,
            self._PushReceiver__status_check,
        )
        self.checkin_thread.daemon = True
        self.checkin_thread.start()

    def close(self) -> None:
        """Cancel the timer and close the socket (unblocks listen())."""
        if self.checkin_thread is not None:
            self.checkin_thread.cancel()
        if self.socket is not None:
            self._PushReceiver__close_socket()
        else:
            self._on_connection_change(False)


class FermaxPushListener:
    """Manages the connection and push notifications from Fermax."""

    def __init__(self, hass: HomeAssistant, session: ClientSession, entry: ConfigEntry):
        self.hass = hass
        self._session = session
        self._config = entry.data
        self._api = FermaxApi(session)
        self._oauth_token: Optional[Dict[str, Any]] = None
        self._fcm_credentials: Optional[Dict[str, Any]] = None
        # FIX (processed IDs): bounded, ordered history instead of an
        # ever-growing set.
        self._processed_ids: Deque[str] = deque(maxlen=MAX_PERSISTENT_IDS)
        self._device_id: Optional[str] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._receiver: Optional[_StoppablePushReceiver] = None
        self._stop_event = threading.Event()
        self._push_connected = threading.Event()
        # FIX (token): serialise token refreshes between camera, lock and ACK.
        self._token_lock = asyncio.Lock()
        self._fcm_registered = False
        self.pairings: List[Dict[str, Any]] = []
        self.ready_event = asyncio.Event()
        # FIX (3): platforms actually set up, so unload uses these and not
        # the (possibly just changed) options.
        self.loaded_platforms: List[str] = []

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
            # Keeps only the most recent MAX_PERSISTENT_IDS (also trims files
            # written by older versions that stored every ID).
            self._processed_ids = deque(ids, maxlen=MAX_PERSISTENT_IDS)

    async def _add_and_save_persistent_id(self, persistent_id: str):
        self._processed_ids.append(persistent_id)
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
            # FIX (6): a rejected login is reported as FermaxAuthError so setup
            # can start a re-authentication flow instead of failing silently.
            if resp.status in (400, 401, 403):
                raise FermaxAuthError(f"FERMAX rejected the credentials (HTTP {resp.status})")
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
        # FIX (7): the lock stops two callers from refreshing at the same time
        # (the second one finds the fresh token), and the margin renews the
        # token shortly before it expires instead of exactly at expiry.
        async with self._token_lock:
            if not self._oauth_token: await self._load_token()
            if not self._oauth_token:
                await self._authenticate_with_password()
                return
            expires_at = self._oauth_token.get(CONF_EXPIRES_AT, 0)
            if time.time() >= expires_at - TOKEN_EXPIRY_MARGIN_SECS:
                try:
                    await self._refresh_token()
                except Exception as e:
                    _LOGGER.warning("Failed to refresh token: %s. Re-authenticating.", e)
                    await self._authenticate_with_password()

    async def _async_fetch_pairings(self) -> None:
        """Fetch pairings. Raises if FERMAX could not be reached."""
        await self._ensure_valid_token()
        pairings = await self._api.async_get_pairings(self._oauth_token[CONF_ACCESS_TOKEN])
        if pairings is None:
            # FIX (6): previously this became an empty list, so setup "worked"
            # without any lock. Now setup is retried later.
            raise ConnectionError("Could not fetch pairings from FERMAX.")
        self.pairings = pairings

    async def async_fetch_pairings(self):
        """Ensures auth and fetches pairings for lock-only mode."""
        _LOGGER.debug("Fetching pairings for lock-only mode.")
        await self._async_fetch_pairings()
        self.ready_event.set() # Signal that pairings are ready

    async def _register_fcm_token(self, active: bool):
        """Register or unregister the FCM token with the Fermax server."""
        await self._ensure_valid_token()
        headers = {"Authorization": f"Bearer {self._oauth_token[CONF_ACCESS_TOKEN]}"}
        registration_id = self._fcm_credentials["fid"]
        payload = {
            "id": registration_id,
            "token": self._device_id,
            "appVersion": "4.5.0",
            "locale": "en",
            "os": "ANDROID",
            "osVersion": "13",
            "appBuild": "769",
            "phoneMobile": "Home Assistant",
            "active": active,
            "force": False,
        }
        if active:
            method = "POST"
            url = API_REGISTER_FCM_URL
        else:
            # The v2 API identifies a registration by its Firebase Installation ID.
            method = "DELETE"
            url = API_UNREGISTER_FCM_URL.format(
                registration_id=quote(registration_id, safe="")
            )

        _LOGGER.info("Setting FCM token status to %s", "active" if active else "inactive")
        async with self._session.request(
            method, url, json=payload, headers=headers
        ) as resp:
            if resp.status in (400, 409):
                error_details = await self._async_fcm_error_details(
                    resp, registration_id
                )
                message = (
                    f"FERMAX rejected FCM token registration (HTTP {resp.status})"
                )
                if error_details:
                    message = f"{message}: {error_details}"
                raise FermaxFcmRegistrationError(message)
            resp.raise_for_status()
            _LOGGER.info("FCM token status updated successfully.")

    async def _async_fcm_error_details(
        self, response: Any, registration_id: str
    ) -> Optional[str]:
        """Return selected, redacted error fields without logging the raw body."""
        try:
            response_data = await response.json(content_type=None)
        except json.JSONDecodeError:
            return None
        if not isinstance(response_data, dict):
            return None

        details = {}
        for key in ("code", "error", "message", "detail", "maxMobileTokens"):
            value = response_data.get(key)
            if not isinstance(value, (str, int, float, bool)):
                continue
            safe_value = str(value)[:200]
            for sensitive_value in (
                registration_id,
                self._device_id,
                self._config.get(CONF_USERNAME),
                (self._oauth_token or {}).get(CONF_ACCESS_TOKEN),
            ):
                if isinstance(sensitive_value, str) and sensitive_value:
                    safe_value = safe_value.replace(sensitive_value, "[redacted]")
            details[key] = safe_value

        return json.dumps(details, ensure_ascii=True) if details else None

    def _build_package_cert(self) -> str:
        sha = hashlib.sha512()
        sha.update(str(self._config[CONF_FCM_GCM_SENDER_ID]).encode("utf-8"))
        sha.update(self._config[CONF_FCM_GMS_APP_ID].encode("utf-8"))
        sha.update(self._config[CONF_FCM_API_KEY].encode("utf-8"))
        sha.update(self._config[CONF_FCM_PROJECT_ID].encode("utf-8"))
        sha.update(self._config[CONF_FCM_ANDROID_PACKAGE_NAME].encode("utf-8"))
        return sha.hexdigest()

    async def _register_fcm_device(
        self, fcm_config: Dict[str, str]
    ) -> Dict[str, Any]:
        """Register FCM while retaining the Firebase Installation ID it uses."""
        installation_id = AndroidFCM.generate_firebase_fid()
        installation_url = (
            "https://firebaseinstallations.googleapis.com/v1/projects/"
            f"{fcm_config['project_id']}/installations"
        )
        installation_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Android-Package": fcm_config["android_package_name"],
            "X-Android-Cert": fcm_config["android_package_cert"],
            "x-firebase-client": (
                "android-min-sdk/23 fire-core/20.0.0 device-name/a21snnxx "
                "device-brand/samsung device-model/a21s "
                "android-installer/com.android.vending fire-android/30 "
                "fire-installations/17.0.0 fire-fcm/22.0.0 "
                "android-platform/ kotlin/1.9.23 android-target-sdk/34"
            ),
            "x-firebase-client-log-type": "3",
            "x-goog-api-key": fcm_config["api_key"],
            "User-Agent": (
                "Dalvik/2.1.0 (Linux; U; Android 11; "
                "SM-A217F Build/RP1A.200720.012)"
            ),
        }
        installation_payload = {
            "fid": installation_id,
            "appId": fcm_config["gms_app_id"],
            "authVersion": "FIS_v2",
            "sdkVersion": "a:17.0.0",
        }

        async with self._session.post(
            installation_url,
            headers=installation_headers,
            json=installation_payload,
        ) as response:
            response.raise_for_status()
            installation = await response.json()

        # Use the same FID for FERMAX registration and later DELETE; the FCM
        # library's convenience method otherwise generates and discards it.
        registration_id = installation.get("fid")
        installation_auth_token = installation.get("authToken", {}).get("token")
        if not registration_id or not installation_auth_token:
            raise RuntimeError("Firebase Installations returned incomplete credentials.")

        check_in = await self.hass.async_add_executor_job(AndroidFCM.gcm_check_in)
        if not check_in or not check_in.get("androidId") or not check_in.get("securityToken"):
            raise RuntimeError("Google FCM check-in returned incomplete credentials.")

        register_func = partial(
            AndroidFCM.register_request,
            check_in["androidId"],
            check_in["securityToken"],
            installation_auth_token,
            fcm_config["api_key"],
            fcm_config["gcm_sender_id"],
            fcm_config["gms_app_id"],
            fcm_config["android_package_name"],
            fcm_config["android_package_cert"],
        )
        fcm_token = await self.hass.async_add_executor_job(register_func)
        if not fcm_token:
            raise RuntimeError("Google FCM returned an empty registration token.")

        return {
            "fid": registration_id,
            "gcm": {
                "androidId": check_in["androidId"],
                "securityToken": check_in["securityToken"],
            },
            "fcm": {"token": fcm_token},
        }

    async def _get_or_register_fcm_credentials(self):
        self._fcm_credentials = await self._fcm_store.async_load()
        if not self._fcm_credentials or not self._fcm_credentials.get("fid"):
            _LOGGER.info(
                "No compatible FCM registration found. Registering with Google FCM..."
            )
            fcm_config = {
                "api_key": self._config[CONF_FCM_API_KEY],
                "project_id": self._config[CONF_FCM_PROJECT_ID],
                "gcm_sender_id": self._config[CONF_FCM_GCM_SENDER_ID],
                "gms_app_id": self._config[CONF_FCM_GMS_APP_ID],
                "android_package_name": self._config[CONF_FCM_ANDROID_PACKAGE_NAME],
                "android_package_cert": self._build_package_cert(),
            }
            try:
                # Older stored credentials do not contain the FID required by
                # FERMAX's v2 unregister endpoint, so renew them once.
                self._fcm_credentials = await self._register_fcm_device(fcm_config)
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
        _LOGGER.info("FCM credentials ready.")

    # --- Listener lifecycle ---
    async def async_setup(self) -> None:
        """Authenticate, register with FCM/FERMAX and fetch pairings.

        FIX (4): this used to run inside the listener thread. If it failed,
        ready_event was never set and the lock platform waited forever. It now
        runs in the event loop during setup, so any error makes the whole
        setup fail (and be retried) before platforms are loaded.
        """
        await self._ensure_valid_token()
        await self._get_or_register_fcm_credentials()
        await self._load_persistent_ids()
        await self._async_fetch_pairings()
        await self._register_fcm_token(True)
        self._fcm_registered = True
        self.ready_event.set()
        async_dispatcher_send(self.hass, SIGNAL_LISTENER_READY)
        _LOGGER.info("Fermax push listener is ready.")

    def start(self) -> None:
        """Start the background thread that listens for FCM messages."""
        self._stop_event.clear()
        self._listener_thread = threading.Thread(
            target=self._listen_forever, name="fermax_duoxme_push", daemon=True
        )
        self._listener_thread.start()
        _LOGGER.info("Fermax push listener has been started.")

    def _listen_forever(self) -> None:
        """Thread body: keep listening, reconnecting with back-off on errors.

        FIX (2): before, any exception from listen() (no network at startup, a
        dropped connection during re-login, ...) ended the thread for good and
        notifications silently stopped until Home Assistant was restarted.
        """
        delay = RECONNECT_MIN_DELAY_SECS
        while not self._stop_event.is_set():
            receiver = _StoppablePushReceiver(
                self._fcm_credentials,
                list(self._processed_ids),
                self._stop_event,
                self._set_push_connection,
            )
            self._receiver = receiver
            started = time.monotonic()
            try:
                _LOGGER.info("Listening for incoming notifications...")
                receiver.listen(self._on_notification)
            except _ReceiverStopped:
                break
            except Exception as err:  # noqa: BLE001 - reconnect on any error
                if self._stop_event.is_set():
                    break
                _LOGGER.warning(
                    "FCM listener stopped (%r). Reconnecting in %ss.", err, delay
                )
            finally:
                receiver.close()

            if time.monotonic() - started >= RECONNECT_STABLE_SECS:
                delay = RECONNECT_MIN_DELAY_SECS
            if self._stop_event.wait(delay):
                break
            delay = min(delay * 2, RECONNECT_MAX_DELAY_SECS)
        _LOGGER.debug("FCM listener thread finished.")

    @property
    def push_connected(self) -> bool:
        """Return whether the FCM socket is currently connected."""
        return self._push_connected.is_set()

    def _set_push_connection(self, connected: bool) -> None:
        """Track socket login/close transitions from the receiver thread."""
        if self._push_connected.is_set() == connected:
            return
        if connected:
            self._push_connected.set()
        else:
            self._push_connected.clear()

        if not self.hass.loop.is_closed():
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send,
                self.hass,
                SIGNAL_PUSH_CONNECTION_CHANGED,
                connected,
            )

    def _on_notification(self, _, notification: dict, data_message):
        """Callback for when a notification is received (listener thread)."""
        persistent_id = data_message.persistent_id

        # Do not log the payload: it contains private room and socket details.
        _LOGGER.debug("Push notification received. Persistent ID: %s", persistent_id)

        # FIX (processed IDs): the library keeps appending every received ID to
        # its own list (and sends it on each re-login); keep only the last ones.
        if self._receiver is not None:
            del self._receiver.persistent_ids[:-MAX_PERSISTENT_IDS]

        # Everything else runs in the event loop, so the ID history is only
        # touched from one thread.
        self.hass.loop.call_soon_threadsafe(
            self._handle_notification, persistent_id, notification
        )

    def _handle_notification(self, persistent_id: str, notification: dict) -> None:
        """Process a notification in the event loop."""
        if persistent_id in self._processed_ids:
            _LOGGER.debug("Ignoring already processed notification ID: %s", persistent_id)
            return

        _LOGGER.info(
            "Processing new notification (type: %s).",
            notification.get("FermaxNotificationType"),
        )
        self._processed_ids.append(persistent_id)

        if notification.get("SendAcknowledge") == "true":
            self.hass.async_create_task(self._acknowledge_notification(persistent_id))

        async_dispatcher_send(self.hass, SIGNAL_NOTIFICATION_RECEIVED, notification)
        call_state = {
            NOTIFICATION_TYPE_CALL: "started",
            NOTIFICATION_TYPE_CALL_ATTENDED: "attended",
            NOTIFICATION_TYPE_CALL_ENDED: "ended",
        }.get(notification.get(NOTIFICATION_TYPE_KEY))
        if call_state:
            event_data = {"state": call_state}
            device_id = notification.get(NOTIFICATION_DEVICE_ID_KEY)
            if isinstance(device_id, str) and device_id:
                event_data["device_id"] = device_id
            self.hass.bus.async_fire(EVENT_CALL, event_data)
        self.hass.async_create_task(
            self._id_store.async_save(list(self._processed_ids))
        )

    async def _acknowledge_notification(self, message_id: str):
        """Acknowledge a notification."""
        await self._ensure_valid_token()
        access_token = self._oauth_token["access_token"]
        await self._api.async_acknowledge_notification(access_token, message_id)

    async def stop(self):
        """Stops the listener gracefully."""
        _LOGGER.info("Stopping Fermax push listener...")
        # FIX (1): really stop the listener thread. Before, only the FCM token
        # was de-registered and the old thread kept running after every reload
        # (e.g. each time the options were changed), duplicating listeners.
        self._stop_event.set()
        if self._receiver is not None:
            self._receiver.close()
        if self._listener_thread is not None:
            await self.hass.async_add_executor_job(
                self._listener_thread.join, THREAD_JOIN_TIMEOUT_SECS
            )
            if self._listener_thread.is_alive():
                _LOGGER.warning("FCM listener thread did not stop in time.")
            self._listener_thread = None

        if self._device_id and self._fcm_registered:
            try:
                await self._register_fcm_token(False)
            except Exception as e:
                _LOGGER.error("Failed to de-register FCM token: %s", e)
            self._fcm_registered = False
