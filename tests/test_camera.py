"""Verify snapshot selection, optional dependencies and fallback behavior."""

from unittest.mock import AsyncMock, patch

from tests.helpers import HomeAssistantError, IntegrationTestCase


class CameraTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.listener = self.make_listener()
        self.listener._ensure_valid_token = AsyncMock()
        self.listener._api.get_photo = AsyncMock(return_value=b"photo")
        self.listener._api.async_get_photo_list = AsyncMock(
            return_value=[{"photoId": "latest"}]
        )
        self.camera = self.attach_entity(
            self.modules.camera.FermaxDoorbellCamera(self.listener, self.entry)
        )
        self.camera._api = self.listener._api
        self.notification = {"RoomId": "room", "SocketUrl": "https://signal.invalid"}
        self.capture = self.enterContext(
            patch.object(
                self.modules.webrtc, "async_get_go2rtc_frame",
                new_callable=AsyncMock, return_value=b"live",
            )
        )

    async def test_default_uses_fermax_photo_without_starting_webrtc(self):
        await self.camera._handle_incoming_call(self.notification)

        self.assertEqual(self.camera._image, b"photo")
        self.capture.assert_not_awaited()
        self.modules.camera.async_process_requirements.assert_not_awaited()

    async def test_successful_live_capture_replaces_image_without_fetching_photo(self):
        self.entry.options["enable_live_snapshot"] = True
        await self.camera._handle_incoming_call(self.notification)

        self.capture.assert_awaited_once_with(
            self.hass, "room", "https://signal.invalid", "test-access", "test-device"
        )
        self.assertEqual(self.camera._image, b"live")
        self.listener._api.async_get_photo_list.assert_not_awaited()
        self.camera.async_write_ha_state.assert_called_once()

    async def test_live_failure_or_missing_signaling_falls_back_to_photo(self):
        self.entry.options["enable_live_snapshot"] = True
        self.capture.return_value = None
        with self.assertLogs(self.modules.camera.__name__, level="WARNING"):
            await self.camera._handle_incoming_call(self.notification)
        self.assertEqual(self.camera._image, b"photo")
        self.capture.reset_mock()
        with self.assertLogs(self.modules.camera.__name__, level="ERROR"):
            await self.camera._handle_incoming_call({})
        self.capture.assert_not_awaited()

    async def test_aiortc_selection_uses_requested_backend(self):
        self.entry.options.update(
            {"enable_live_snapshot": True, "live_snapshot_backend": "aiortc"}
        )
        self.camera._capture_with_aiortc = AsyncMock(return_value=b"aiortc-frame")
        await self.camera._handle_incoming_call(self.notification)

        self.capture.assert_not_awaited()
        self.assertEqual(self.camera._image, b"aiortc-frame")

    async def test_failed_photo_refresh_does_not_erase_live_image(self):
        self.camera._image = b"previous-live-frame"
        self.listener._api.get_photo.return_value = None
        await self.camera._fetch_latest_image()

        self.listener._api.get_photo.assert_awaited_once_with("test-access", "latest")
        self.assertEqual(self.camera._image, b"previous-live-frame")
        self.camera.async_write_ha_state.assert_not_called()

    async def test_unavailable_aiortc_is_reported_once_and_not_reinstalled(self):
        self.modules.camera.async_process_requirements.side_effect = HomeAssistantError(
            "package unavailable"
        )
        with self.assertLogs(self.modules.camera.__name__, level="WARNING"):
            self.assertIsNone(
                await self.camera._capture_with_aiortc("room", "url", "auth", "app")
            )
        self.assertIsNone(
            await self.camera._capture_with_aiortc("room", "url", "auth", "app")
        )
        self.modules.camera.async_process_requirements.assert_awaited_once()

    async def test_aiortc_installs_requirements_only_on_first_use(self):
        with patch.object(
            self.modules.webrtc, "async_get_aiortc_frame",
            new_callable=AsyncMock, return_value=b"frame",
        ) as capture:
            for _ in range(2):
                self.assertEqual(
                    await self.camera._capture_with_aiortc("room", "url", "auth", "app"),
                    b"frame",
                )
            self.assertEqual(capture.await_count, 2)
        self.modules.camera.async_process_requirements.assert_awaited_once_with(
            self.hass, "fermax_duoxme", ["aiortc", "pillow"]
        )

    async def test_notifications_route_calls_and_other_updates_separately(self):
        self.camera._handle_incoming_call = AsyncMock()
        self.camera._delayed_fetch_latest_image = AsyncMock()
        notification = {"FermaxNotificationType": "Call"}
        self.camera._handle_notification(notification)
        self.camera._handle_notification({"FermaxNotificationType": "CallEnd"})
        await self.tasks[0]
        await self.tasks[1]

        self.camera._handle_incoming_call.assert_awaited_once_with(notification)
        self.camera._delayed_fetch_latest_image.assert_awaited_once()

    async def test_delayed_refresh_waits_before_querying_api(self):
        self.camera._fetch_latest_image = AsyncMock()
        with patch.object(
            self.modules.camera.asyncio, "sleep", new_callable=AsyncMock
        ) as sleep:
            await self.camera._delayed_fetch_latest_image()
        sleep.assert_awaited_once_with(5)
        self.camera._fetch_latest_image.assert_awaited_once()
