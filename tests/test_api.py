"""Verify FERMAX HTTP contracts without contacting the service."""

import base64
import json

from aiohttp import ClientConnectionError, ClientResponseError

from tests.helpers import IntegrationTestCase, response


class ApiTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.api = self.modules.api.FermaxApi(self.session)

    async def test_password_authentication_uses_basic_auth_and_form_data(self):
        self.session.post.return_value = response()
        await self.api.authenticate_with_password("user", "password", "client", "secret")

        args, kwargs = self.session.post.call_args
        self.assertEqual(args, (self.modules.const.OAUTH_TOKEN_URL,))
        self.assertEqual(
            kwargs["headers"]["Authorization"],
            "Basic " + base64.b64encode(b"client:secret").decode(),
        )
        self.assertEqual(
            kwargs["data"],
            {"grant_type": "password", "username": "user", "password": "password"},
        )

    async def test_rejected_login_propagates_the_http_error(self):
        self.session.post.return_value = response(status=401)
        with self.assertRaises(ClientResponseError):
            await self.api.authenticate_with_password("user", "wrong", "client", "secret")

    async def test_pairings_request_and_unavailable_service(self):
        pairings = [{"deviceId": "doorbell"}]
        self.session.get.return_value = response(payload=pairings)
        self.assertEqual(await self.api.async_get_pairings("access"), pairings)
        self.session.get.assert_called_once_with(
            self.modules.const.API_PAIRINGS_URL,
            headers={"Authorization": "Bearer access"},
        )
        self.session.get.side_effect = ClientConnectionError("offline")
        with self.assertLogs(self.modules.api.__name__, level="ERROR"):
            self.assertIsNone(await self.api.async_get_pairings("access"))

    async def test_unlock_targets_device_and_serializes_access_id(self):
        access_id = {"block": 200, "subblock": -1, "number": 0}
        self.session.post.return_value = response()
        self.assertTrue(await self.api.async_open_door("access", "doorbell", access_id))
        args, kwargs = self.session.post.call_args
        self.assertEqual(
            args[0],
            self.modules.const.API_OPEN_DOOR_URL.format(device_id="doorbell"),
        )
        self.assertEqual(json.loads(kwargs["data"]), access_id)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer access")

        self.session.post.return_value = response(status=500)
        with self.assertLogs(self.modules.api.__name__, level="ERROR"):
            self.assertFalse(await self.api.async_open_door("access", "doorbell", access_id))

    async def test_acknowledgement_payload_and_failure(self):
        self.session.post.return_value = response()
        self.assertTrue(await self.api.async_acknowledge_notification("access", "message"))
        self.assertEqual(
            self.session.post.call_args.kwargs["json"],
            {"attended": True, "fcmMessageId": "message"},
        )
        self.session.post.return_value = response(status=503)
        with self.assertLogs(self.modules.api.__name__, level="ERROR"):
            self.assertFalse(await self.api.async_acknowledge_notification("access", "message"))

    async def test_call_list_uses_app_token_and_all_call_types(self):
        calls = [{"photoId": "photo"}]
        self.session.get.return_value = response(payload=calls)
        self.assertEqual(await self.api.async_get_photo_list("access", "device"), calls)
        self.assertEqual(
            self.session.get.call_args.kwargs["params"],
            {"appToken": "device", "callRegistryType": "all"},
        )
        self.session.get.return_value = response(status=500)
        with self.assertLogs(self.modules.api.__name__, level="ERROR"):
            self.assertIsNone(await self.api.async_get_photo_list("access", "device"))

    async def test_photo_download_does_not_forward_oauth_to_image_host(self):
        self.session.get.side_effect = [
            response(payload={"url": "https://images.invalid/photo"}),
            response(body=b"jpeg"),
        ]
        self.assertEqual(await self.api.get_photo("access", "photo"), b"jpeg")
        first, second = self.session.get.call_args_list
        self.assertEqual(first.kwargs["params"], {"photoId": "photo"})
        self.assertEqual(first.kwargs["headers"]["Authorization"], "Bearer access")
        self.assertEqual(second.args, ("https://images.invalid/photo",))
        self.assertEqual(second.kwargs, {})

    async def test_missing_photo_url_or_http_error_does_not_download_an_image(self):
        for result in (response(payload={}), response(status=404)):
            with self.subTest(status=result.status):
                self.session.get.reset_mock()
                self.session.get.return_value = result
                with self.assertLogs(self.modules.api.__name__, level="ERROR"):
                    self.assertIsNone(await self.api.get_photo("access", "photo"))
                self.assertEqual(self.session.get.call_count, 1)
