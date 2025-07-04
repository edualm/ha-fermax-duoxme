"""Binary sensor platform for FERMAX DuoxMe."""
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
    NOTIFICATION_TYPE_KEY,
    SIGNAL_CALL_INITIATED_WITH_IMAGE,
    SIGNAL_NOTIFICATION_RECEIVED,
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the FERMAX DuoxMe binary sensor platform."""
    async_add_entities([FermaxCallSensor(entry)])


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
        # This listener turns the sensor ON, but only after the camera has a live image.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_CALL_INITIATED_WITH_IMAGE, self._turn_on
            )
        )
        # This listener turns the sensor OFF for any non-call notification.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_NOTIFICATION_RECEIVED, self._handle_end_call
            )
        )

    @callback
    def _turn_on(self) -> None:
        """Turn the sensor on."""
        _LOGGER.info("Ring sensor turned ON.")
        self._attr_is_on = True
        self.async_write_ha_state()

    @callback
    def _handle_end_call(self, notification: dict) -> None:
        """Turn the sensor off if it's not a call notification."""
        notification_type = notification.get(NOTIFICATION_TYPE_KEY)
        if notification_type != NOTIFICATION_TYPE_CALL:
            _LOGGER.info("Ring sensor turned OFF.")
            self._attr_is_on = False
            self.async_write_ha_state()
