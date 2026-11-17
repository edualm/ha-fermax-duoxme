"""Sensor platform for FERMAX DuoxMe."""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    INTEGRATION_NAME,
    NOTIFICATION_DEVICE_ID_KEY,
    NOTIFICATION_PHOTO_ID_KEY,
    NOTIFICATION_TYPE_CALL,
    NOTIFICATION_TYPE_CALL_ATTENDED,
    NOTIFICATION_TYPE_CALL_ENDED,
    NOTIFICATION_TYPE_KEY,
    SIGNAL_LISTENER_READY,
    SIGNAL_NOTIFICATION_RECEIVED,
)
from .push import FermaxPushListener

CALL_LOG_REFRESH_DELAY_SECS = 5
CALL_LOG_MAX_STALENESS_SECS = 10


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the FERMAX DuoxMe sensor platform."""
    listener: FermaxPushListener = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([FermaxLastCallSensor(listener, entry)])


class FermaxLastCallSensor(SensorEntity):
    """Expose the timestamp and metadata of the latest call."""

    _attr_has_entity_name = True
    _attr_name = "Last call"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, listener: FermaxPushListener, entry: ConfigEntry) -> None:
        """Initialize the last-call sensor."""
        self._listener = listener
        self._refresh_task: Optional[asyncio.Task[None]] = None
        self._attr_native_value: Optional[datetime] = None
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._attr_unique_id = f"{entry.entry_id}_last_call"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": INTEGRATION_NAME,
            "manufacturer": "FERMAX",
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to call updates and load the most recent call."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_NOTIFICATION_RECEIVED, self._handle_notification
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_LISTENER_READY, self._handle_listener_ready
            )
        )
        self.async_on_remove(self._cancel_refresh_task)
        if self._listener.ready_event.is_set():
            self._schedule_refresh(0)

    @callback
    def _handle_listener_ready(self, *_: Any) -> None:
        """Load the call log once the listener has completed setup."""
        self._schedule_refresh(0)

    @callback
    def _handle_notification(self, notification: dict[str, Any]) -> None:
        """Update immediately on calls, then refresh metadata from the API."""
        notification_type = notification.get(NOTIFICATION_TYPE_KEY)
        if notification_type not in (
            NOTIFICATION_TYPE_CALL,
            NOTIFICATION_TYPE_CALL_ATTENDED,
            NOTIFICATION_TYPE_CALL_ENDED,
        ):
            return

        if notification_type == NOTIFICATION_TYPE_CALL:
            self._attr_native_value = dt_util.utcnow()
            device_id = notification.get(NOTIFICATION_DEVICE_ID_KEY)
            self._attr_extra_state_attributes = (
                {"device_id": device_id}
                if isinstance(device_id, str) and device_id
                else {}
            )
            self.async_write_ha_state()

        self._schedule_refresh(CALL_LOG_REFRESH_DELAY_SECS)

    @callback
    def _schedule_refresh(self, delay: int) -> None:
        """Schedule one refresh, replacing any earlier pending refresh."""
        self._cancel_refresh_task()
        self._refresh_task = self.hass.async_create_task(
            self._async_refresh_latest_call(delay)
        )

    @callback
    def _cancel_refresh_task(self) -> None:
        """Cancel a pending call-log refresh."""
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = None

    async def _async_refresh_latest_call(self, delay: int) -> None:
        """Fetch the newest call-log item and its photo metadata."""
        if delay:
            await asyncio.sleep(delay)

        await self._listener._ensure_valid_token()
        token_data = self._listener._oauth_token
        device_id = self._listener._device_id
        if not token_data or not device_id:
            return

        access_token = token_data.get("access_token")
        if not access_token:
            return
        calls = await self._listener._api.async_get_photo_list(
            access_token, device_id
        )
        if not calls or not isinstance(calls, list) or not isinstance(calls[0], dict):
            return

        latest_call = calls[0]
        call_instant = latest_call.get("callInstant") or latest_call.get("timestamp")
        parsed_instant = None
        if isinstance(call_instant, str):
            parsed_instant = dt_util.parse_datetime(call_instant)
            if parsed_instant is not None:
                if parsed_instant.tzinfo is None:
                    parsed_instant = parsed_instant.replace(tzinfo=timezone.utc)
                parsed_instant = dt_util.as_utc(parsed_instant)

        # A newly received call can reach Home Assistant before FERMAX adds it
        # to the call log; don't replace it with an older log entry.
        if (
            parsed_instant is not None
            and self._attr_native_value is not None
            and self._attr_native_value - parsed_instant
            > timedelta(seconds=CALL_LOG_MAX_STALENESS_SECS)
        ):
            return
        if parsed_instant is not None:
            self._attr_native_value = parsed_instant

        attributes = dict(self._attr_extra_state_attributes)
        for attr, key in (
            ("device_id", "deviceId"),
            ("device_name", "name"),
            ("photo_id", NOTIFICATION_PHOTO_ID_KEY),
        ):
            value = latest_call.get(key)
            if isinstance(value, str) and value:
                attributes[attr] = value
        self._attr_extra_state_attributes = attributes
        self.async_write_ha_state()
