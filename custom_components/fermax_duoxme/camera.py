"""Camera platform for FERMAX DuoxMe."""
import asyncio
import logging
from typing import Optional

from aiohttp import web

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.network import get_url

from .api import FermaxApi
from .const import (
    DOMAIN,
    INTEGRATION_NAME,
    NOTIFICATION_PHOTO_ID_KEY,
    NOTIFICATION_ROOM_ID_KEY,
    NOTIFICATION_SOCKET_URL_KEY,
    NOTIFICATION_TYPE_CALL,
    NOTIFICATION_TYPE_KEY,
    SIGNAL_CALL_INITIATED_WITH_IMAGE,
    SIGNAL_LISTENER_READY,
    SIGNAL_NOTIFICATION_RECEIVED,
)
from .push import FermaxPushListener
from .webrtc import async_get_webrtc_frame

_LOGGER = logging.getLogger(__name__)

FRAME_BOUNDARY = "frame"

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the FERMAX DuoxMe camera platform."""
    listener = hass.data[DOMAIN][entry.entry_id]
    camera_entity = FermaxDoorbellCamera(listener, entry)
    hass.http.register_view(FermaxDoorbellStreamView(camera_entity))
    async_add_entities([camera_entity])

class FermaxDoorbellStreamView(HomeAssistantView):
    """View to stream MJPEG from the Fermax doorbell camera."""
    url = f"/api/{DOMAIN}/stream/{{entity_id}}"
    name = f"api:{DOMAIN}:stream"
    requires_auth = False

    def __init__(self, camera_entity: "FermaxDoorbellCamera"):
        self.camera_entity = camera_entity

    async def get(self, request: web.Request, entity_id: str) -> web.StreamResponse:
        if entity_id != self.camera_entity.entity_id:
            raise web.HTTPNotFound()
        return await self.camera_entity.handle_async_mjpeg_stream(request)

class FermaxDoorbellCamera(Camera):
    """Representation of the FERMAX Doorbell camera."""
    _attr_has_entity_name = True
    _attr_name = "Doorbell"
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, listener: FermaxPushListener, entry: ConfigEntry):
        """Initialize the camera."""
        super().__init__()
        self._listener = listener
        self.entry = entry
        self._api: Optional[FermaxApi] = None
        self._image: Optional[bytes] = None
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
        _LOGGER.debug("Camera received notification: %s", notification)
        try:
            notification_type = notification.get(NOTIFICATION_TYPE_KEY)
            if notification_type == NOTIFICATION_TYPE_CALL:
                self.hass.async_create_task(self._handle_incoming_call(notification))
            else:
                self.hass.async_create_task(self._delayed_fetch_latest_image())
        except Exception:
            _LOGGER.exception("Error handling notification in camera.")

    async def _handle_incoming_call(self, notification: dict):
        """Fetch a live image via WebRTC and then trigger the ring sensor."""
        _LOGGER.info("Incoming call detected. Attempting to capture live image via WebRTC.")
        
        room_id = notification.get(NOTIFICATION_ROOM_ID_KEY)
        socket_url = notification.get(NOTIFICATION_SOCKET_URL_KEY)
        
        await self._listener._ensure_valid_token()
        auth_token = self._listener._oauth_token["access_token"]
        app_token = self._listener._device_id

        if not all([room_id, socket_url, auth_token, app_token]):
            _LOGGER.error("Missing required data for WebRTC call.")
            return

        image_bytes = await async_get_webrtc_frame(room_id, socket_url, auth_token, app_token)
        
        if image_bytes:
            self._image = image_bytes
            self.async_write_ha_state()
            _LOGGER.info("Live image captured. Firing call signal.")
            # Now that the image is set, signal the binary_sensor to turn on.
            async_dispatcher_send(self.hass, SIGNAL_CALL_INITIATED_WITH_IMAGE)
        else:
            _LOGGER.warning("Failed to capture live image. Falling back to latest snapshot.")
            await self._fetch_latest_image()
            # Still fire the signal so the user is notified of the call.
            async_dispatcher_send(self.hass, SIGNAL_CALL_INITIATED_WITH_IMAGE)

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

    async def stream_source(self) -> str | None:
        """Return the source of the stream."""
        if not self.entity_id: return None
        base_url = get_url(self.hass, allow_internal=True)
        return f"{base_url}/api/{DOMAIN}/stream/{self.entity_id}"

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse:
        """Generate an MJPEG stream."""
        response = web.StreamResponse(status=200, reason="OK", headers={"Content-Type": f"multipart/x-mixed-replace; boundary={FRAME_BOUNDARY}"})
        await response.prepare(request)
        try:
            while True:
                image = await self.async_camera_image()
                if not image:
                    await asyncio.sleep(1)
                    continue
                await response.write((f"--{FRAME_BOUNDARY}\r\n" f"Content-Type: image/jpeg\r\n" f"Content-Length: {len(image)}\r\n\r\n").encode() + image + b"\r\n")
                await asyncio.sleep(1)
        except (asyncio.CancelledError, ConnectionResetError):
            _LOGGER.debug("MJPEG stream disconnected.")
        except Exception:
            _LOGGER.exception("Error in MJPEG stream.")
        finally:
            return response
