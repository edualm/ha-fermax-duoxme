"""Verify authentication, registration and push lifecycle without real threads."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from aiohttp import ClientResponseError

from tests.helpers import IntegrationTestCase, response


class PushTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.push = self.modules.push
        self.listener = self.make_listener()

    async def test_concurrent_requests_refresh_once_at_the_expiry_margin(self):
        self.listener._oauth_token = {
            "access_token": "old", "refresh_token": "refresh", "expires_at": 1060
        }

        async def token_payload():
            await asyncio.sleep(0)
            return {"access_token": "new", "refresh_token": "next", "expires_in": 3600}

        result = response()
        result.json.side_effect = token_payload
        self.session.post.return_value = result
        with patch.object(self.push.time, "time", return_value=1000):
            await asyncio.gather(
                self.listener._ensure_valid_token(),
                self.listener._ensure_valid_token(),
            )

        self.session.post.assert_called_once()
        self.assertEqual(
            self.session.post.call_args.kwargs["data"],
            {"grant_type": "refresh_token", "refresh_token": "refresh"},
        )
        self.assertEqual(self.listener._oauth_token["expires_at"], 4600)
        self.listener._token_store.async_save.assert_awaited_once_with(
            self.listener._oauth_token
        )

    async def test_failed_refresh_falls_back_to_password_and_persists_new_token(self):
        self.listener._oauth_token = {"access_token": "old", "refresh_token": "refresh"}
        self.session.post.side_effect = [
            response(status=401),
            response(payload={"access_token": "new", "expires_in": 3600}),
        ]
        with self.assertLogs(self.push.__name__, level="WARNING"):
            await self.listener._ensure_valid_token()

        self.assertEqual(
            [item.kwargs["data"]["grant_type"] for item in self.session.post.call_args_list],
            ["refresh_token", "password"],
        )
        self.assertEqual(self.listener._oauth_token["access_token"], "new")
        self.listener._token_store.async_save.assert_awaited_once()

    async def test_startup_reuses_valid_stored_token_or_logs_in_if_absent(self):
        token = self.listener._oauth_token
        self.listener._oauth_token = None
        self.listener._token_store.async_load.return_value = token
        await self.listener._ensure_valid_token()
        self.session.post.assert_not_called()

        self.listener._oauth_token = None
        self.listener._token_store.async_load.return_value = None
        self.session.post.return_value = response(payload={"access_token": "new"})
        await self.listener._ensure_valid_token()
        self.assertEqual(self.listener._oauth_token["access_token"], "new")
        self.listener._token_store.async_save.assert_awaited_once()

    async def test_rejected_password_is_distinct_from_temporary_server_failure(self):
        for status in (400, 401, 403, 503):
            with self.subTest(status=status):
                self.session.post.return_value = response(status=status)
                expected = self.push.FermaxAuthError if status < 500 else ClientResponseError
                with self.assertRaises(expected):
                    await self.listener._authenticate_with_password()
        self.listener._token_store.async_save.assert_not_awaited()

    async def test_pairing_failure_does_not_masquerade_as_empty_account(self):
        self.listener._api.async_get_pairings = AsyncMock(return_value=None)
        with self.assertRaises(ConnectionError):
            await self.listener.async_fetch_pairings()
        self.assertFalse(self.listener.ready_event.is_set())

        self.listener._api.async_get_pairings.return_value = []
        await self.listener.async_fetch_pairings()
        self.assertTrue(self.listener.ready_event.is_set())
        self.assertEqual(self.listener.pairings, [])

    async def test_fcm_registration_and_removal_use_same_installation_id(self):
        self.listener._fcm_credentials = {"fid": "installation/with space"}
        self.session.request.return_value = response()
        await self.listener._register_fcm_token(True)
        await self.listener._register_fcm_token(False)

        registration, removal = self.session.request.call_args_list
        self.assertEqual(
            registration.args, ("POST", self.modules.const.API_REGISTER_FCM_URL)
        )
        self.assertEqual(
            removal.args,
            (
                "DELETE",
                self.modules.const.API_UNREGISTER_FCM_URL.format(
                    registration_id="installation%2Fwith%20space"
                ),
            ),
        )
        self.assertEqual(registration.kwargs["json"]["id"], "installation/with space")
        self.assertEqual(registration.kwargs["json"]["token"], "test-device")
        self.assertTrue(registration.kwargs["json"]["active"])
        self.assertFalse(removal.kwargs["json"]["active"])
        self.assertEqual(removal.kwargs["headers"], {"Authorization": "Bearer test-access"})

    async def test_permanent_fcm_rejections_include_only_redacted_whitelisted_details(self):
        self.listener._fcm_credentials = {"fid": "installation"}
        private_values = (
            "installation", "test-device", self.entry.data["username"], "test-access"
        )
        payload = {
            "message": " ".join(private_values),
            "code": "LIMIT",
            "maxMobileTokens": 5,
            "detail": {"nested": "not-a-public-scalar"},
            "unknown": "must-not-appear",
        }
        for status in (400, 409):
            with self.subTest(status=status):
                self.session.request.return_value = response(status=status, payload=payload)
                with self.assertRaises(self.push.FermaxFcmRegistrationError) as caught:
                    await self.listener._register_fcm_token(True)
                message = str(caught.exception)
                for value in private_values + ("must-not-appear", "not-a-public-scalar"):
                    self.assertNotIn(value, message)
                self.assertIn("LIMIT", message)
                self.assertIn("[redacted]", message)

    async def test_fcm_error_details_handle_invalid_json_and_limit_message_length(self):
        result = response()
        result.json.side_effect = json.JSONDecodeError("invalid", "", 0)
        self.assertIsNone(await self.listener._async_fcm_error_details(result, "fid"))
        result.json.side_effect = None
        result.json.return_value = ["not-an-object"]
        self.assertIsNone(await self.listener._async_fcm_error_details(result, "fid"))
        result.json.return_value = {"message": "x" * 250}
        details = await self.listener._async_fcm_error_details(result, "fid")
        self.assertEqual(json.loads(details), {"message": "x" * 200})

    def firebase_config(self):
        return {
            "api_key": "test-key",
            "project_id": "test-project",
            "gcm_sender_id": "test-sender",
            "gms_app_id": "test-app",
            "android_package_name": "com.example.fermax",
            "android_package_cert": "test-cert",
        }

    async def test_firebase_registration_preserves_server_fid_and_installation_auth(self):
        android = self.push.AndroidFCM
        android.generate_firebase_fid.return_value = "local-fid"
        android.gcm_check_in.return_value = {"androidId": 123, "securityToken": 456}
        android.register_request.return_value = "fcm-token"
        self.session.post.return_value = response(
            payload={"fid": "server-fid", "authToken": {"token": "installation-auth"}}
        )
        credentials = await self.listener._register_fcm_device(self.firebase_config())

        self.assertEqual(
            credentials,
            {
                "fid": "server-fid",
                "gcm": {"androidId": 123, "securityToken": 456},
                "fcm": {"token": "fcm-token"},
            },
        )
        self.assertEqual(self.session.post.call_args.kwargs["json"]["fid"], "local-fid")
        android.register_request.assert_called_once_with(
            123, 456, "installation-auth", "test-key", "test-sender",
            "test-app", "com.example.fermax", "test-cert",
        )

    async def test_incomplete_google_responses_fail_instead_of_saving_credentials(self):
        valid_installation = {"fid": "fid", "authToken": {"token": "auth"}}
        valid_checkin = {"androidId": 123, "securityToken": 456}
        for installation, checkin, token in (
            ({}, valid_checkin, "token"),
            (valid_installation, {}, "token"),
            (valid_installation, valid_checkin, ""),
        ):
            with self.subTest(installation=installation, checkin=checkin, token=token):
                self.session.post.return_value = response(payload=installation)
                self.push.AndroidFCM.gcm_check_in.return_value = checkin
                self.push.AndroidFCM.register_request.return_value = token
                with self.assertRaises(RuntimeError):
                    await self.listener._register_fcm_device(self.firebase_config())
        self.listener._fcm_store.async_save.assert_not_awaited()

    async def test_only_legacy_fcm_cache_without_fid_is_renewed(self):
        credentials = {"fid": "fid", "fcm": {"token": "new-token"}}
        self.listener._register_fcm_device = AsyncMock(return_value=credentials)
        for cached, renew in (
            (None, True), ({"fcm": {"token": "old-token"}}, True), (credentials, False)
        ):
            with self.subTest(cached=cached):
                self.listener._register_fcm_device.reset_mock()
                self.listener._fcm_store.async_save.reset_mock()
                self.listener._fcm_store.async_load.return_value = cached
                await self.listener._get_or_register_fcm_credentials()
                self.assertEqual(self.listener._register_fcm_device.await_count, int(renew))
                self.assertEqual(self.listener._fcm_store.async_save.await_count, int(renew))
                self.assertEqual(self.listener._device_id, "new-token")

    async def test_notification_deduplication_acknowledgement_and_public_event_data(self):
        self.listener._api.async_acknowledge_notification = AsyncMock()
        notifications = (
            ("Call", "true", "device", {"state": "started", "device_id": "device"}),
            ("CallAttend", "false", "", {"state": "attended"}),
            ("CallEnd", None, 123, {"state": "ended"}),
        )
        for index, (kind, acknowledge, device, expected) in enumerate(notifications):
            notification = {
                "FermaxNotificationType": kind,
                "SendAcknowledge": acknowledge,
                "DeviceId": device,
                "RoomId": "private-room",
                "SocketUrl": "private-url",
            }
            self.listener._handle_notification(str(index), notification)
            self.listener._handle_notification(str(index), notification)
            self.hass.bus.async_fire.assert_called_with("fermax_duoxme_call", expected)

        self.listener._handle_notification("info", {"FermaxNotificationType": "DeviceInfo"})
        await asyncio.gather(*self.tasks)
        self.assertEqual(self.hass.bus.async_fire.call_count, 3)
        self.listener._api.async_acknowledge_notification.assert_awaited_once_with(
            "test-access", "0"
        )
        self.assertEqual(self.push.async_dispatcher_send.call_count, 4)
        self.assertEqual(
            self.listener._id_store.async_save.await_args.args[0], ["0", "1", "2", "info"]
        )

    async def test_old_persistent_id_files_and_receiver_history_are_bounded(self):
        self.listener._id_store.async_load.return_value = list(range(150))
        await self.listener._load_persistent_ids()
        self.assertEqual(list(self.listener._processed_ids), list(range(50, 150)))
        await self.listener._add_and_save_persistent_id(150)
        self.listener._id_store.async_save.assert_awaited_once_with(list(range(51, 151)))

        self.listener._receiver = SimpleNamespace(persistent_ids=list(range(150)))
        self.hass.loop = Mock()
        self.listener._on_notification(None, {"kind": "test"}, SimpleNamespace(persistent_id="id"))
        self.assertEqual(self.listener._receiver.persistent_ids, list(range(50, 150)))
        self.hass.loop.call_soon_threadsafe.assert_called_once_with(
            self.listener._handle_notification, "id", {"kind": "test"}
        )

    async def test_connection_transitions_are_dispatched_once_and_not_to_closed_loop(self):
        self.hass.loop = Mock()
        self.hass.loop.is_closed.return_value = False
        for state in (True, True, False, False):
            self.listener._set_push_connection(state)
        self.assertEqual(
            [args.args[-1] for args in self.hass.loop.call_soon_threadsafe.call_args_list],
            [True, False],
        )
        self.hass.loop.is_closed.return_value = True
        self.listener._set_push_connection(True)
        self.assertTrue(self.listener.push_connected)
        self.assertEqual(self.hass.loop.call_soon_threadsafe.call_count, 2)

    async def test_setup_marks_ready_only_after_successful_registration(self):
        steps = (
            "_ensure_valid_token", "_get_or_register_fcm_credentials",
            "_load_persistent_ids", "_async_fetch_pairings", "_register_fcm_token",
        )
        order = Mock()
        for step in steps:
            mocked = AsyncMock()
            setattr(self.listener, step, mocked)
            order.attach_mock(mocked, step)
        self.listener._register_fcm_token.side_effect = RuntimeError("registration failed")
        with self.assertRaises(RuntimeError):
            await self.listener.async_setup()
        self.assertFalse(self.listener.ready_event.is_set())
        self.assertFalse(self.listener._fcm_registered)
        self.push.async_dispatcher_send.assert_not_called()

        order.reset_mock()
        self.listener._register_fcm_token.side_effect = None
        await self.listener.async_setup()
        self.assertEqual(
            order.mock_calls,
            [call._ensure_valid_token(), call._get_or_register_fcm_credentials(),
             call._load_persistent_ids(), call._async_fetch_pairings(),
             call._register_fcm_token(True)],
        )
        self.assertTrue(self.listener.ready_event.is_set())
        self.assertTrue(self.listener._fcm_registered)
        self.push.async_dispatcher_send.assert_called_once_with(
            self.hass, self.modules.const.SIGNAL_LISTENER_READY
        )

    async def test_stop_closes_receiver_joins_thread_then_unregisters_only_once(self):
        order = []
        self.listener._receiver = Mock()
        self.listener._receiver.close.side_effect = lambda: order.append("close")
        thread = self.listener._listener_thread = Mock()
        thread.join.side_effect = lambda timeout: order.append("join")
        thread.is_alive.return_value = False
        self.listener._fcm_registered = True
        self.listener._register_fcm_token = AsyncMock(
            side_effect=lambda active: order.append("unregister")
        )
        await self.listener.stop()

        self.assertTrue(self.listener._stop_event.is_set())
        self.assertEqual(order, ["close", "join", "unregister"])
        thread.join.assert_called_once_with(10)
        self.assertIsNone(self.listener._listener_thread)
        await self.listener.stop()
        self.listener._register_fcm_token.assert_awaited_once_with(False)

    async def test_stop_reports_thread_timeout_and_failed_deregistration(self):
        self.listener._listener_thread = Mock()
        self.listener._listener_thread.is_alive.return_value = True
        self.listener._fcm_registered = True
        self.listener._register_fcm_token = AsyncMock(side_effect=RuntimeError("offline"))
        with self.assertLogs(self.push.__name__, level="WARNING") as logs:
            await self.listener.stop()
        self.assertTrue(any("did not stop" in message for message in logs.output))
        self.assertTrue(any("de-register" in message for message in logs.output))
        self.assertFalse(self.listener._fcm_registered)

    async def test_reconnect_backoff_is_capped_and_stable_connection_resets_it(self):
        for delays, clock in (
            ([5, 10, 20, 40, 80, 160, 300, 300], [0, 1] * 8),
            ([5, 5, 10], [0, 1, 1, 302, 302, 303]),
        ):
            with self.subTest(delays=delays):
                stop = self.listener._stop_event = Mock()
                stop.is_set.return_value = False
                stop.wait.side_effect = [False] * (len(delays) - 1) + [True]
                receivers = [
                    Mock(listen=Mock(side_effect=ConnectionError("offline")))
                    for _ in delays
                ]
                with (
                    patch.object(self.push, "_StoppablePushReceiver", side_effect=receivers),
                    patch.object(self.push.time, "monotonic", side_effect=clock),
                    self.assertLogs(self.push.__name__, level="WARNING"),
                ):
                    self.listener._listen_forever()
                self.assertEqual(stop.wait.call_args_list, [call(delay) for delay in delays])
                for receiver in receivers:
                    receiver.close.assert_called_once()

    async def test_receiver_stop_interrupts_login_reset_and_timer_rescheduling(self):
        stop = Mock()
        stop.is_set.return_value = False
        receiver = self.push._StoppablePushReceiver({}, [], stop, Mock())
        receiver.MIN_RESET_INTERVAL_SECS = 300
        receiver.last_reset = 100
        stop.wait.return_value = True
        with patch.object(self.push.time, "time", return_value=101):
            with self.assertRaises(self.push._ReceiverStopped):
                receiver._PushReceiver__reset()
        stop.wait.assert_called_once_with(299)

        receiver.MAX_SILENT_INTERVAL_SECS = 300
        receiver.time_last_message_received = 100
        with (
            patch.object(self.push.time, "time", return_value=150),
            patch.object(self.push.threading, "Timer") as timer,
        ):
            receiver._PushReceiver__status_check()
            self.assertTrue(timer.return_value.daemon)
            timer.return_value.start.assert_called_once()
            stop.is_set.return_value = True
            receiver._PushReceiver__status_check()
            timer.assert_called_once()
            with self.assertRaises(self.push._ReceiverStopped):
                receiver._PushReceiver__login()
            receiver.close()
            timer.return_value.cancel.assert_called_once()
