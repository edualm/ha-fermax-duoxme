"""Camera platform for FERMAX DuoxMe."""
import asyncio
import json
import logging
from typing import Optional

from aiohttp import web

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.network import get_url

from .api import FermaxApi
from .const import (
    DOMAIN,
    INTEGRATION_NAME,
    NOTIFICATION_PHOTO_ID_KEY,
    NOTIFICATION_TYPE_CALL,
    NOTIFICATION_TYPE_KEY,
    SIGNAL_LISTENER_READY,
    SIGNAL_NOTIFICATION_RECEIVED,
)
from .push import FermaxPushListener

_LOGGER = logging.getLogger(__name__)

FRAME_BOUNDARY = "frame"
LOCAL_IMAGE_FILENAME = "call_image.jpg"

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
        self._api: Optional[FermaxApi] = None  # Initialize as None
        self._image: Optional[bytes] = None
        self._local_image_path = None # Will be set in async_added_to_hass
        self._attr_unique_id = f"{entry.entry_id}_camera"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": INTEGRATION_NAME,
            "manufacturer": "FERMAX",
        }

    async def async_added_to_hass(self) -> None:
        """Handle entity addition."""
        # Initialize API and paths now that hass is available
        self._api = FermaxApi(async_get_clientsession(self.hass))
        self._local_image_path = self.hass.config.path(
            "custom_components", DOMAIN, LOCAL_IMAGE_FILENAME
        )

        # Listen for new notifications to update the image
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_NOTIFICATION_RECEIVED, self._handle_notification
            )
        )
        
        # Listen for the signal that the push listener is ready to fetch initial image
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_LISTENER_READY, self._fetch_latest_image
            )
        )
        
        # If the listener is already ready when we add the entity, fetch immediately.
        if self._listener.ready_event.is_set():
            await self._fetch_latest_image()

    @callback
    def _handle_notification(self, notification: dict) -> None:
        """Handle an incoming notification."""
        _LOGGER.debug("Camera received notification: %s", notification)
        try:
            notification_type = notification.get(NOTIFICATION_TYPE_KEY)

            if notification_type == NOTIFICATION_TYPE_CALL:
                self.hass.async_create_task(self._load_local_image())
            else:
                # Any other notification type signifies the call has ended.
                # Schedule a fetch of the latest image after a short delay.
                _LOGGER.info("Non-call notification received. Scheduling fetch of latest image.")
                self.hass.async_create_task(self._delayed_fetch_latest_image())

        except Exception:
            _LOGGER.exception("Error handling notification in camera.")

    async def _delayed_fetch_latest_image(self):
        """Wait for a few seconds then fetch the latest image."""
        await asyncio.sleep(5)  # Wait 5 seconds for the server to process the image
        _LOGGER.info("Delay finished. Fetching latest image from server.")
        await self._fetch_latest_image()

    def _read_local_image_bytes(self) -> Optional[bytes]:
        """Read the local image file from disk."""
        try:
            with open(self._local_image_path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            _LOGGER.error("'%s' not found. Please place it in the integration directory.", LOCAL_IMAGE_FILENAME)
            return None

    async def _load_local_image(self):
        """Load the placeholder image for a call asynchronously."""
        _LOGGER.info("Incoming call detected. Displaying local 'call' image.")
        # Run the blocking file I/O in a separate thread
        self._image = await self.hass.async_add_executor_job(self._read_local_image_bytes)
        self.async_write_ha_state()

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

            if not device_id:
                _LOGGER.error("Device ID not available. Cannot fetch latest image.")
                return

            photo_list = await self._api.async_get_photo_list(access_token, device_id)
            
            if photo_list and isinstance(photo_list, list) and photo_list[0].get(NOTIFICATION_PHOTO_ID_KEY):
                latest_photo_id = photo_list[0][NOTIFICATION_PHOTO_ID_KEY]
                _LOGGER.debug("Latest photoId found: %s", latest_photo_id)
                await self._fetch_image(latest_photo_id)
            else:
                _LOGGER.info("No photos found in the call history.")
        except Exception:
            _LOGGER.exception("Error fetching latest image.")


    async def async_camera_image(
        self, width: Optional[int] = None, height: Optional[int] = None
    ) -> Optional[bytes]:
        """Return the current image."""
        return self._image

    async def stream_source(self) -> str | None:
        """Return the source of the stream."""
        if not self.entity_id:
            return None
        base_url = get_url(self.hass, allow_internal=True)
        return f"{base_url}/api/{DOMAIN}/stream/{self.entity_id}"

    async def handle_async_mjpeg_stream(self, request: web.Request) -> web.StreamResponse:
        """Generate an MJPEG stream."""
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={"Content-Type": f"multipart/x-mixed-replace; boundary={FRAME_BOUNDARY}"},
        )
        await response.prepare(request)
        try:
            while True:
                image = await self.async_camera_image()
                if not image:
                    await asyncio.sleep(1)
                    continue
                
                await response.write(
                    (
                        f"--{FRAME_BOUNDARY}\r\n"
                        "Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(image)}\r\n\r\n"
                    ).encode()
                    + image
                    + b"\r\n"
                )
                await asyncio.sleep(1)
        except (asyncio.CancelledError, ConnectionResetError):
            _LOGGER.debug("MJPEG stream disconnected.")
        except Exception:
            _LOGGER.exception("Error in MJPEG stream.")
        finally:
            return response
