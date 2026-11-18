"""Binary sensor platform for FERMAX DuoxMe."""
import asyncio
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    INTEGRATION_NAME,
    NOTIFICATION_TYPE_CALL,
    NOTIFICATION_TYPE_CALL_ATTENDED,
    NOTIFICATION_TYPE_CALL_ENDED,
    NOTIFICATION_TYPE_KEY,
    SIGNAL_NOTIFICATION_RECEIVED,
    SIGNAL_PUSH_CONNECTION_CHANGED,
)
from .push import FermaxPushListener

_LOGGER = logging.getLogger(__name__)

PUSH_DISCONNECT_DELAY_SECS = 30

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the FERMAX DuoxMe binary sensor platform."""
    listener: FermaxPushListener = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [FermaxCallSensor(entry), FermaxPushConnectionSensor(listener, entry)]
    )


class FermaxCallSensor(BinarySensorEntity):
    """Representation of a sensor that detects Fermax calls."""

    _attr_has_entity_name = True
    _attr_name = "Ring"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_is_on = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the binary sensor."""
        self._attr_unique_id = f"{entry.entry_id}_ring_sensor"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": INTEGRATION_NAME,
            "manufacturer": "FERMAX",
        }

    async def async_added_to_hass(self) -> None:
        """Register for dispatcher calls."""
        # FIX (ring sensor): the sensor reacts to the notification itself.
        # Before, it waited for the camera to finish capturing the live image
        # (up to ~15-25 s with WebRTC), delaying any automation on the ring.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_NOTIFICATION_RECEIVED, self._handle_notification
            )
        )

    @callback
    def _handle_notification(self, notification: dict) -> None:
        """Track call start and end without reacting to unrelated messages."""
        notification_type = notification.get(NOTIFICATION_TYPE_KEY)
        if notification_type == NOTIFICATION_TYPE_CALL:
            is_on = True
        elif notification_type in (
            NOTIFICATION_TYPE_CALL_ATTENDED,
            NOTIFICATION_TYPE_CALL_ENDED,
        ):
            is_on = False
        else:
            # FERMAX also sends informational and device notifications; they
            # must not clear an active ring.
            return

        if is_on == self._attr_is_on:
            return
        _LOGGER.info("Ring sensor turned %s.", "ON" if is_on else "OFF")
        self._attr_is_on = is_on
        self.async_write_ha_state()


class FermaxPushConnectionSensor(BinarySensorEntity):
    """Report FCM connectivity without recording brief reconnections."""

    _attr_has_entity_name = True
    _attr_name = "Push connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, listener: FermaxPushListener, entry: ConfigEntry) -> None:
        """Initialize the connection sensor."""
        self._listener = listener
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._attr_is_on = listener.push_connected
        self._attr_unique_id = f"{entry.entry_id}_push_connection"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": INTEGRATION_NAME,
            "manufacturer": "FERMAX",
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to FCM socket state changes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_PUSH_CONNECTION_CHANGED,
                self._handle_connection_changed,
            )
        )
        self.async_on_remove(self._cancel_pending_disconnect)
        self._handle_connection_changed(self._listener.push_connected)

    @callback
    def _handle_connection_changed(self, connected: bool) -> None:
        """Report connections immediately, but allow a grace period for outages."""
        if connected:
            self._cancel_pending_disconnect()
            if not self._attr_is_on:
                self._attr_is_on = True
                self.async_write_ha_state()
            return
        if self._attr_is_on and self._disconnect_timer is None:
            self._disconnect_timer = self.hass.loop.call_later(
                PUSH_DISCONNECT_DELAY_SECS, self._mark_disconnected
            )

    @callback
    def _mark_disconnected(self) -> None:
        """Report an outage only if the socket is still disconnected."""
        self._disconnect_timer = None
        if self._listener.push_connected or not self._attr_is_on:
            return
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _cancel_pending_disconnect(self) -> None:
        """Cancel delayed state updates on reconnection or entity removal."""
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None
