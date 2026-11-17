"""Camera platform for FERMAX DuoxMe."""
import asyncio
import logging
from typing import Optional

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.requirements import async_process_requirements

from .api import FermaxApi
from .const import (
    CONF_ENABLE_LIVE_SNAPSHOT,
    CONF_LIVE_SNAPSHOT_BACKEND,
    DEFAULT_LIVE_SNAPSHOT_BACKEND,
    DOMAIN,
    LIVE_SNAPSHOT_BACKEND_AIORTC,
    INTEGRATION_NAME,
    NOTIFICATION_PHOTO_ID_KEY,
    NOTIFICATION_ROOM_ID_KEY,
    NOTIFICATION_SOCKET_URL_KEY,
    NOTIFICATION_TYPE_CALL,
    NOTIFICATION_TYPE_KEY,
    SIGNAL_LISTENER_READY,
    SIGNAL_NOTIFICATION_RECEIVED,
)
from .push import FermaxPushListener

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the FERMAX DuoxMe camera platform."""
    listener = hass.data[DOMAIN][entry.entry_id]
    # FIX (security): the custom MJPEG view registered here had
    # requires_auth = False, so anyone reaching Home Assistant could see the
    # doorbell image without logging in. It has been removed; Home Assistant's
    # own authenticated camera endpoints (camera_proxy / camera_proxy_stream)
    # already serve the image and an MJPEG stream built from it.
    async_add_entities([FermaxDoorbellCamera(listener, entry)])

class FermaxDoorbellCamera(Camera):
    """Representation of the FERMAX Doorbell camera."""
    _attr_has_entity_name = True
    _attr_name = "Doorbell"
    # FIX (stream): CameraEntityFeature.STREAM removed. The "stream" was only
    # the last still image repeated over MJPEG, yet HA (and go2rtc) tried to
    # play it as a video stream. Without it HA shows the still image.
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, listener: FermaxPushListener, entry: ConfigEntry):
        """Initialize the camera."""
        super().__init__()
        self._listener = listener
        self.entry = entry
        self._api: Optional[FermaxApi] = None
        self._image: Optional[bytes] = None
        self._webrtc_available: Optional[bool] = None
        self._attr_unique_id = f"{entry.entry_id}_camera"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": INTEGRATION_NAME,
            "manufacturer": "FERMAX",
        }

    async def async_added_to_hass(self) -> None:
        """Handle entity addition."""
        self._api = self._listener._api
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_NOTIFICATION_RECEIVED, self._handle_notification
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_LISTENER_READY, self._fetch_latest_image
            )
        )
        if self._listener.ready_event.is_set():
            await self._fetch_latest_image()

    @callback
    def _handle_notification(self, notification: dict) -> None:
        """Handle an incoming notification."""
        try:
            notification_type = notification.get(NOTIFICATION_TYPE_KEY)
            if notification_type == NOTIFICATION_TYPE_CALL:
                self.hass.async_create_task(self._handle_incoming_call(notification))
            else:
                self.hass.async_create_task(self._delayed_fetch_latest_image())
        except Exception:
            _LOGGER.exception("Error handling notification in camera.")

    async def _handle_incoming_call(self, notification: dict):
        """Fetch a live image for an incoming call (the ring sensor is already on)."""
        if not self.entry.options.get(CONF_ENABLE_LIVE_SNAPSHOT, False):
            _LOGGER.info(
                "Incoming call detected. Live capture disabled; using latest snapshot."
            )
            await self._fetch_latest_image()
            return

        backend = self.entry.options.get(
            CONF_LIVE_SNAPSHOT_BACKEND, DEFAULT_LIVE_SNAPSHOT_BACKEND
        )
        _LOGGER.info(
            "Incoming call detected. Attempting to capture live image via WebRTC (%s).",
            backend,
        )

        room_id = notification.get(NOTIFICATION_ROOM_ID_KEY)
        socket_url = notification.get(NOTIFICATION_SOCKET_URL_KEY)

        await self._listener._ensure_valid_token()
        auth_token = self._listener._oauth_token["access_token"]
        app_token = self._listener._device_id

        image_bytes = None
        if not all([room_id, socket_url, auth_token, app_token]):
            _LOGGER.error("Missing required data for WebRTC call.")
        elif backend == LIVE_SNAPSHOT_BACKEND_AIORTC:
            image_bytes = await self._capture_with_aiortc(
                room_id, socket_url, auth_token, app_token
            )
        else:
            from .webrtc import async_get_go2rtc_frame

            image_bytes = await async_get_go2rtc_frame(
                self.hass, room_id, socket_url, auth_token, app_token
            )

        if image_bytes:
            self._image = image_bytes
            self.async_write_ha_state()
            _LOGGER.info("Live image captured.")
        else:
            _LOGGER.warning(
                "Failed to capture live image (%s). Falling back to latest snapshot.",
                backend,
            )
            await self._fetch_latest_image()
        # FIX (ring sensor): no signal is sent from here any more; the ring
        # sensor turns on as soon as the call notification arrives, before
        # this capture starts.

    async def _capture_with_aiortc(
        self, room_id: str, socket_url: str, auth_token: str, app_token: str
    ) -> Optional[bytes]:
        """Capture a frame with aiortc, installing it on first use."""
        if self._webrtc_available is False:
            return None
        try:
            if self._webrtc_available is None:
                # pillow is needed by aiortc to turn the frame into a JPEG; it is
                # only installed here, not for every user (see manifest.json).
                await async_process_requirements(
                    self.hass, DOMAIN, ["aiortc", "pillow"]
                )
            from .webrtc import async_get_aiortc_frame

            self._webrtc_available = True
            return await async_get_aiortc_frame(
                room_id, socket_url, auth_token, app_token
            )
        except (HomeAssistantError, ImportError) as err:
            self._webrtc_available = False
            _LOGGER.warning("aiortc is unavailable for live frame capture: %s", err)
            return None

    async def _delayed_fetch_latest_image(self):
        """Wait a few seconds then fetch the latest image."""
        await asyncio.sleep(5)
        await self._fetch_latest_image()

    async def _fetch_image(self, photo_id: str):
        """Fetch a specific image from the API."""
        _LOGGER.debug("Fetching image for photoId: %s", photo_id)
        await self._listener._ensure_valid_token()
        access_token = self._listener._oauth_token["access_token"]
        image = await self._api.get_photo(access_token, photo_id)
        if image:
            self._image = image
            self.async_write_ha_state()

    async def _fetch_latest_image(self, *args):
        """Fetch the very latest image available from the call list."""
        _LOGGER.info("Fetching latest image...")
        try:
            await self._listener._ensure_valid_token()
            access_token = self._listener._oauth_token["access_token"]
            device_id = self._listener._device_id
            if not device_id: return
            photo_list = await self._api.async_get_photo_list(access_token, device_id)
            if photo_list and isinstance(photo_list, list) and photo_list[0].get(NOTIFICATION_PHOTO_ID_KEY):
                await self._fetch_image(photo_list[0][NOTIFICATION_PHOTO_ID_KEY])
        except Exception:
            _LOGGER.exception("Error fetching latest image.")

    async def async_camera_image(self, width: Optional[int] = None, height: Optional[int] = None) -> Optional[bytes]:
        """Return the current image."""
        return self._image
