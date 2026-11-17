"""Verify sensor state transitions, call metadata and door commands."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from tests.helpers import IntegrationTestCase


class SensorTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.listener = self.make_listener()
        self.listener._ensure_valid_token = AsyncMock()
        self.listener._api.async_get_photo_list = AsyncMock(return_value=[])
        self.sensor = self.attach_entity(
            self.modules.sensor.FermaxLastCallSensor(self.listener, self.entry)
        )

    async def test_ring_changes_only_for_call_lifecycle_notifications(self):
        sensor = self.attach_entity(
            self.modules.binary_sensor.FermaxCallSensor(self.entry)
        )
        for kind, expected in (
            ("Call", True),
            ("DeviceInfo", True),
            ("Call", True),
            ("CallAttend", False),
            ("Call", True),
            ("CallEnd", False),
        ):
            sensor._handle_notification({"FermaxNotificationType": kind})
            self.assertEqual(sensor._attr_is_on, expected)
        self.assertEqual(sensor.async_write_ha_state.call_count, 4)

    async def test_push_connection_sensor_ignores_duplicate_states(self):
        self.listener._push_connected.set()
        sensor = self.attach_entity(
            self.modules.binary_sensor.FermaxPushConnectionSensor(self.listener, self.entry)
        )
        self.assertTrue(sensor._attr_is_on)
        sensor._handle_connection_changed(True)
        sensor.async_write_ha_state.assert_not_called()
        sensor._handle_connection_changed(False)
        self.assertFalse(sensor._attr_is_on)
        sensor.async_write_ha_state.assert_called_once()

    async def test_new_call_updates_immediately_and_discards_previous_metadata(self):
        self.sensor._attr_extra_state_attributes = {"photo_id": "previous-photo"}
        self.sensor._schedule_refresh = Mock()
        now = self.modules.sensor.dt_util.utcnow.return_value
        self.sensor._handle_notification(
            {"FermaxNotificationType": "Call", "DeviceId": "new-device"}
        )

        self.assertEqual(self.sensor._attr_native_value, now)
        self.assertEqual(
            self.sensor._attr_extra_state_attributes, {"device_id": "new-device"}
        )
        self.sensor.async_write_ha_state.assert_called_once()
        self.sensor._schedule_refresh.assert_called_once_with(5)

    async def test_non_call_notifications_do_not_refresh_last_call(self):
        self.sensor._schedule_refresh = Mock()
        self.sensor._handle_notification({"FermaxNotificationType": "DeviceInfo"})
        self.sensor._schedule_refresh.assert_not_called()

        for kind in ("CallAttend", "CallEnd"):
            self.sensor._handle_notification({"FermaxNotificationType": kind})
        self.assertEqual(self.sensor._schedule_refresh.call_count, 2)
        self.sensor.async_write_ha_state.assert_not_called()

    async def test_call_list_normalizes_naive_timestamp_and_filters_attributes(self):
        naive = datetime(2026, 1, 1, 12)
        utc = naive.replace(tzinfo=timezone.utc)
        dt = self.modules.sensor.dt_util
        dt.parse_datetime.return_value = naive
        dt.as_utc.return_value = utc
        self.listener._api.async_get_photo_list.return_value = [
            {
                "callInstant": "2026-01-01T12:00:00",
                "deviceId": "device",
                "name": "Entrance",
                "photoId": "photo",
                "private": "do-not-expose",
            }
        ]
        await self.sensor._async_refresh_latest_call(0)

        dt.as_utc.assert_called_once_with(utc)
        self.assertEqual(self.sensor._attr_native_value, utc)
        self.assertEqual(
            self.sensor._attr_extra_state_attributes,
            {"device_id": "device", "device_name": "Entrance", "photo_id": "photo"},
        )

    async def test_stale_call_log_does_not_replace_new_push_timestamp_or_photo(self):
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.sensor._attr_native_value = now
        self.sensor._attr_extra_state_attributes = {"device_id": "new-device"}
        self.listener._api.async_get_photo_list.return_value = [
            {"callInstant": "old-time", "photoId": "old-photo"}
        ]
        dt = self.modules.sensor.dt_util
        dt.parse_datetime.return_value = now - timedelta(seconds=11)
        dt.as_utc.side_effect = lambda value: value
        await self.sensor._async_refresh_latest_call(0)

        self.assertEqual(self.sensor._attr_native_value, now)
        self.assertEqual(self.sensor._attr_extra_state_attributes, {"device_id": "new-device"})
        self.sensor.async_write_ha_state.assert_not_called()

    async def test_exact_staleness_boundary_and_invalid_timestamps(self):
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        dt = self.modules.sensor.dt_util
        dt.as_utc.side_effect = lambda value: value
        self.sensor._attr_native_value = now
        self.listener._api.async_get_photo_list.return_value = [
            {"timestamp": "ten-seconds-ago", "photoId": "photo"}
        ]
        dt.parse_datetime.return_value = now - timedelta(seconds=10)
        await self.sensor._async_refresh_latest_call(0)
        self.assertEqual(self.sensor._attr_native_value, now - timedelta(seconds=10))

        dt.parse_datetime.return_value = None
        await self.sensor._async_refresh_latest_call(0)
        self.assertEqual(self.sensor._attr_native_value, now - timedelta(seconds=10))

    async def test_missing_session_device_or_valid_call_list_does_not_write_state(self):
        for calls in (None, [], {}, ["invalid-item"]):
            with self.subTest(calls=calls):
                self.listener._api.async_get_photo_list.return_value = calls
                await self.sensor._async_refresh_latest_call(0)
        self.listener._device_id = None
        await self.sensor._async_refresh_latest_call(0)
        self.listener._oauth_token = None
        await self.sensor._async_refresh_latest_call(0)
        self.sensor.async_write_ha_state.assert_not_called()

    async def test_refresh_replaces_pending_task_and_remove_cancels_the_replacement(self):
        started = asyncio.Event()

        async def refresh(delay):
            started.set()
            await asyncio.Event().wait()

        self.sensor._async_refresh_latest_call = refresh
        self.sensor._schedule_refresh(5)
        first = self.sensor._refresh_task
        await started.wait()
        self.sensor._schedule_refresh(5)
        second = self.sensor._refresh_task
        self.assertIsNot(first, second)
        await asyncio.gather(first, return_exceptions=True)
        self.assertTrue(first.cancelled())
        self.sensor._cancel_refresh_task()
        await asyncio.gather(second, return_exceptions=True)
        self.assertTrue(second.cancelled())
        self.assertIsNone(self.sensor._refresh_task)

    async def test_ready_sensor_subscribes_and_requests_initial_metadata(self):
        self.listener.ready_event.set()
        self.sensor._schedule_refresh = Mock()
        await self.sensor.async_added_to_hass()
        self.sensor._schedule_refresh.assert_called_once_with(0)
        self.assertEqual(self.sensor.async_on_remove.call_count, 3)


class LockTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.listener = self.make_listener()
        self.listener._ensure_valid_token = AsyncMock()
        self.listener._api.async_open_door = AsyncMock(return_value=True)
        self.access = {"block": 200, "subblock": -1, "number": 0}
        self.door = self.attach_entity(
            self.modules.lock.FermaxDoorLock(
                self.listener, self.entry, "device", "ZERO",
                {"title": "Door", "accessId": self.access},
            )
        )
        self.door.name = "Door"

    async def test_unlock_uses_default_or_selected_state_duration(self):
        for options, duration in (({}, 2), ({"unlock_duration": 7}, 7)):
            with self.subTest(duration=duration):
                self.entry.options = options
                states = []
                self.door.async_write_ha_state.side_effect = lambda: states.append(
                    self.door._attr_is_locked
                )
                with patch.object(self.modules.lock.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                    await self.door.async_unlock()
                sleep.assert_awaited_once_with(duration)
                self.assertEqual(states, [False, True])
        self.listener._api.async_open_door.assert_awaited_with(
            "test-access", "device", self.access
        )

    async def test_rejected_unlock_preserves_locked_state(self):
        self.listener._api.async_open_door.return_value = False
        with (
            patch.object(self.modules.lock.asyncio, "sleep", new_callable=AsyncMock) as sleep,
            self.assertLogs(self.modules.lock.__name__, level="ERROR"),
        ):
            await self.door.async_unlock()
        sleep.assert_not_awaited()
        self.door.async_write_ha_state.assert_not_called()
        self.assertTrue(self.door._attr_is_locked)

    async def test_setup_creates_only_visible_access_doors_with_device_id(self):
        self.listener.ready_event.set()
        self.listener.pairings = [
            {"accessDoorMap": {"ZERO": {"visible": True}}},
            {
                "deviceId": "device",
                "accessDoorMap": {
                    "ZERO": {"visible": True, "accessId": self.access},
                    "ONE": {"visible": False, "accessId": self.access},
                },
            },
        ]
        self.hass.data["fermax_duoxme"] = {self.entry.entry_id: self.listener}
        add_entities = Mock()
        await self.modules.lock.async_setup_entry(self.hass, self.entry, add_entities)

        doors = add_entities.call_args.args[0]
        self.assertEqual(len(doors), 1)
        self.assertEqual(doors[0]._access_id, self.access)

    async def test_lock_is_not_sent_as_an_unsupported_device_command(self):
        with self.assertLogs(self.modules.lock.__name__, level="WARNING"):
            await self.door.async_lock()
        self.listener._api.async_open_door.assert_not_awaited()
