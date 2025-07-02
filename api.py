"""API for FERMAX DuoxMe."""
import base64
import json
import logging
from typing import Any, Dict, List, Optional

from aiohttp import ClientSession

from .const import (
    API_ACK_URL,
    API_LIST_URL,
    API_OPEN_DOOR_URL,
    API_PAIRINGS_URL,
    API_PHOTO_URL,
    OAUTH_TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

class FermaxApi:
    """A class for making authenticated requests to the Fermax API."""

    def __init__(self, session: ClientSession):
        """Initialize the API helper."""
        self._session = session

    async def authenticate_with_password(
        self, username: str, password: str, client_id: str, client_secret: str
    ):
        """Authenticates using username/password to get the initial token."""
        creds = f"{client_id}:{client_secret}"
        auth_header = base64.b64encode(creds.encode("utf-8")).decode("utf-8")

        headers = {
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        payload = {
            "grant_type": "password",
            "username": username,
            "password": password,
        }
        
        _LOGGER.debug("Attempting to authenticate for config flow validation.")
        async with self._session.post(OAUTH_TOKEN_URL, headers=headers, data=payload) as response:
            response.raise_for_status()
            _LOGGER.debug("Config flow authentication successful.")

    async def async_acknowledge_notification(self, access_token: str, message_id: str) -> bool:
        """Acknowledge a push notification."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        payload = {"attended": True, "fcmMessageId": message_id}

        _LOGGER.info("Acknowledging notification with message ID: %s", message_id)
        try:
            async with self._session.post(API_ACK_URL, headers=headers, json=payload) as response:
                response.raise_for_status()
                _LOGGER.debug("Notification acknowledged successfully.")
                return True
        except Exception as e:
            _LOGGER.error("Failed to acknowledge notification: %s", e)
            return False

    async def async_get_pairings(self, access_token: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch the user's device pairings."""
        headers = {"Authorization": f"Bearer {access_token}"}
        _LOGGER.debug("Fetching pairings.")
        try:
            async with self._session.get(API_PAIRINGS_URL, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug("Pairings received: %s", data)
                return data
        except Exception as e:
            _LOGGER.error("Failed to get pairings: %s", e)
            return None

    async def async_open_door(
        self, access_token: str, device_id: str, access_id: Dict[str, int]
    ) -> bool:
        """Send the command to open a specific door."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        url = API_OPEN_DOOR_URL.format(device_id=device_id)
        payload = json.dumps(access_id)
        
        _LOGGER.info("Sending open door command for device %s", device_id)
        try:
            async with self._session.post(url, headers=headers, data=payload) as response:
                response.raise_for_status()
                _LOGGER.info("Open door command successful.")
                return True
        except Exception as e:
            _LOGGER.error("Failed to open door: %s", e)
            return False

    async def async_get_photo_list(self, access_token: str, device_id: str) -> Optional[List]:
        """Fetch the list of recent photos/calls."""
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"appToken": device_id, "callRegistryType": "all"}
        
        _LOGGER.debug("Fetching photo list.")
        try:
            async with self._session.get(API_LIST_URL, headers=headers, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                _LOGGER.debug("Photo list received: %s", data)
                return data
        except Exception as e:
            _LOGGER.error("Failed to get photo list: %s", e)
            return None

    async def get_photo(self, access_token: str, photo_id: str) -> Optional[bytes]:
        """Fetch the image data for a given photo ID."""
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"photoId": photo_id}
        
        try:
            async with self._session.get(API_PHOTO_URL, headers=headers, params=params) as response:
                response.raise_for_status()
                data = await response.json()

            if "url" not in data:
                _LOGGER.error("Image URL not found in photocall response")
                return None

            image_url = data["url"]
            
            async with self._session.get(image_url) as image_response:
                image_response.raise_for_status()
                return await image_response.read()

        except Exception as e:
            _LOGGER.error("Failed to get photo for id %s: %s", e)
            return None
