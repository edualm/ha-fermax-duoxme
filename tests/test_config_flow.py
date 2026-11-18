"""Test credential changes with Home Assistant boundaries mocked."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientConnectionError, ClientResponseError
import voluptuous as vol

from tests.helpers import IntegrationTestCase


class CredentialFlowTests(IntegrationTestCase):
    """Verify credential validation and session replacement ordering."""

    def setUp(self):
        super().setUp()
        config_flow = self.modules.config_flow
        self.entry = SimpleNamespace(
            entry_id="existing-entry",
            unique_id="old@example.invalid",
            title="old@example.invalid",
            data={
                "username": "old@example.invalid",
                "password": "old-password",
                "client_id": "client",
                "client_secret": "secret",
                "fcm_api_key": "existing-fcm-key",
            },
            options={"enable_push_notifications": False, "unlock_duration": 5},
        )
        self.flow = config_flow.FermaxDuoxmeConfigFlow()
        self.flow.hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_unload=AsyncMock(return_value=True),
                async_schedule_reload=Mock(),
            )
        )
        self.flow._get_reconfigure_entry = Mock(return_value=self.entry)
        self.flow._get_reauth_entry = Mock(return_value=self.entry)
        self.flow.async_set_unique_id = AsyncMock(return_value=None)
        self.flow.async_show_form = Mock(
            side_effect=lambda **kwargs: {"type": "form", **kwargs}
        )
        self.flow.async_abort = Mock(
            side_effect=lambda **kwargs: {"type": "abort", **kwargs}
        )
        self.flow.async_update_reload_and_abort = Mock(
            return_value={"type": "abort", "reason": "success"}
        )
        self.api = SimpleNamespace(authenticate_with_password=AsyncMock())
        self.remove_token = AsyncMock()
        self.addCleanup(patch.stopall)
        patch.object(config_flow, "FermaxApi", return_value=self.api).start()
        self.store = patch.object(
            config_flow,
            "Store",
            return_value=SimpleNamespace(async_remove=self.remove_token),
        ).start()
        self.credentials = {
            "username": " New@Example.Invalid ",
            "password": "new-password",
        }

    async def test_form_prefills_username_but_not_password(self):
        result = await self.flow.async_step_reconfigure()

        schema = result["data_schema"]
        self.assertEqual(
            schema({"password": "replacement"})["username"], self.entry.data["username"]
        )
        with self.assertRaises(vol.MultipleInvalid):
            schema({})
        self.api.authenticate_with_password.assert_not_awaited()
        self.flow.hass.config_entries.async_unload.assert_not_awaited()

    async def test_valid_login_unloads_then_clears_token_before_updating(self):
        operations = []

        async def unload(entry_id):
            operations.append("unload")
            return True

        async def remove():
            operations.append("remove-token")

        def update(entry, **kwargs):
            operations.append("update-reload")
            return {"type": "abort", "reason": kwargs["reason"]}

        self.flow.hass.config_entries.async_unload.side_effect = unload
        self.remove_token.side_effect = remove
        self.flow.async_update_reload_and_abort.side_effect = update
        result = await self.flow.async_step_reconfigure(self.credentials)

        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(operations, ["unload", "remove-token", "update-reload"])
        self.api.authenticate_with_password.assert_awaited_once_with(
            "New@Example.Invalid", "new-password", "client", "secret"
        )
        self.flow.async_set_unique_id.assert_awaited_once_with("new@example.invalid")
        self.store.assert_called_once_with(
            self.flow.hass,
            self.modules.const.STORAGE_VERSION,
            self.modules.const.STORAGE_KEY_TOKEN,
        )
        self.flow.async_update_reload_and_abort.assert_called_once_with(
            self.entry,
            data={**self.entry.data, "username": "New@Example.Invalid", "password": "new-password"},
            unique_id="new@example.invalid",
            title="New@Example.Invalid",
            reason="reconfigure_successful",
        )
        self.assertEqual(self.entry.options["unlock_duration"], 5)

    async def test_password_change_keeps_custom_title_and_allows_own_account(self):
        self.entry.title = "My doorbell"
        self.flow.async_set_unique_id.return_value = self.entry
        await self.flow.async_step_reconfigure(
            {"username": self.entry.data["username"], "password": "replacement"}
        )

        self.assertEqual(
            self.flow.async_update_reload_and_abort.call_args.kwargs["title"],
            "My doorbell",
        )

    async def test_existing_other_account_does_not_change_session(self):
        self.flow.async_set_unique_id.return_value = SimpleNamespace(entry_id="other")
        result = await self.flow.async_step_reconfigure(self.credentials)

        self.assertEqual(result["reason"], "already_configured")
        self.api.authenticate_with_password.assert_not_awaited()
        self.flow.hass.config_entries.async_unload.assert_not_awaited()
        self.remove_token.assert_not_awaited()
        self.flow.async_update_reload_and_abort.assert_not_called()

    async def test_validation_errors_leave_saved_credentials_untouched(self):
        cases = (
            (ClientResponseError(None, (), status=401), "invalid_auth"),
            (ClientResponseError(None, (), status=503), "cannot_connect"),
            (ClientConnectionError("offline"), "cannot_connect"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__, expected=expected):
                self.api.authenticate_with_password.side_effect = error
                with self.assertLogs(self.modules.config_flow.__name__):
                    result = await self.flow.async_step_reconfigure(self.credentials)
                self.assertEqual(result["errors"], {"base": expected})

        self.assertEqual(self.entry.data["password"], "old-password")
        self.flow.hass.config_entries.async_unload.assert_not_awaited()
        self.remove_token.assert_not_awaited()
        self.flow.async_update_reload_and_abort.assert_not_called()

    async def test_unload_failure_preserves_cached_token_and_credentials(self):
        self.flow.hass.config_entries.async_unload.return_value = False
        with self.assertLogs(self.modules.config_flow.__name__, level="ERROR"):
            result = await self.flow.async_step_reconfigure(self.credentials)

        self.assertEqual(result["errors"], {"base": "cannot_update"})
        self.remove_token.assert_not_awaited()
        self.flow.async_update_reload_and_abort.assert_not_called()

    async def test_token_removal_failure_reloads_unchanged_entry(self):
        self.remove_token.side_effect = OSError("storage unavailable")
        with self.assertLogs(self.modules.config_flow.__name__, level="ERROR"):
            result = await self.flow.async_step_reconfigure(self.credentials)

        self.assertEqual(result["errors"], {"base": "cannot_update"})
        self.flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
            self.entry.entry_id
        )
        self.flow.async_update_reload_and_abort.assert_not_called()

    async def test_reauthentication_also_discards_previous_token(self):
        await self.flow.async_step_reauth_confirm({"password": "replacement"})

        self.api.authenticate_with_password.assert_awaited_once_with(
            self.entry.data["username"], "replacement", "client", "secret"
        )
        self.remove_token.assert_awaited_once()
        self.flow.async_update_reload_and_abort.assert_called_once_with(
            self.entry, data={**self.entry.data, "password": "replacement"}
        )

    async def test_initial_setup_validates_login_before_showing_fcm_form(self):
        result = await self.flow.async_step_user(self.entry.data)

        self.assertEqual(result["step_id"], "fcm")
        self.assertEqual(self.flow.fermax_data, self.entry.data)
        self.api.authenticate_with_password.assert_awaited_once_with(
            "old@example.invalid", "old-password", "client", "secret"
        )

    async def test_initial_setup_rejected_login_stays_on_user_form(self):
        self.api.authenticate_with_password.side_effect = ClientResponseError(
            SimpleNamespace(real_url="https://fermax.invalid"), (), status=401
        )
        with self.assertLogs(self.modules.config_flow.__name__, level="ERROR"):
            result = await self.flow.async_step_user(self.entry.data)

        self.assertEqual(result["step_id"], "user")
        self.assertEqual(result["errors"], {"base": "invalid_auth"})
        self.assertEqual(self.flow.fermax_data, {})

    async def test_fcm_step_preserves_login_data_and_checks_duplicates(self):
        self.flow.fermax_data = self.entry.data
        self.flow._abort_if_unique_id_configured = Mock()
        self.flow.async_create_entry = Mock(side_effect=lambda **kwargs: kwargs)

        result = await self.flow.async_step_fcm({"fcm_project_id": "project"})

        self.flow.async_set_unique_id.assert_awaited_once_with("old@example.invalid")
        self.flow._abort_if_unique_id_configured.assert_called_once()
        self.assertEqual(result["data"], {**self.entry.data, "fcm_project_id": "project"})

    async def test_reauthentication_starts_with_password_only_form(self):
        result = await self.flow.async_step_reauth(self.entry.data)

        self.assertEqual(result["step_id"], "reauth_confirm")
        self.assertEqual(
            result["data_schema"]({"password": "new"}), {"password": "new"}
        )
        self.remove_token.assert_not_awaited()

    async def test_options_defaults_and_unlock_duration_limits(self):
        handler = self.modules.config_flow.OptionsFlowHandler()
        handler.config_entry = self.entry
        handler.async_show_form = Mock(side_effect=lambda **kwargs: kwargs)
        self.entry.options = {
            "enable_push_notifications": False,
            "live_snapshot_backend": "invalid-backend",
            "unlock_duration": 99,
        }
        result = await handler.async_step_init()
        schema = result["data_schema"]

        self.assertEqual(
            schema({}),
            {
                "enable_push_notifications": False,
                "enable_live_snapshot": False,
                "live_snapshot_backend": "go2rtc",
                "unlock_duration": 2,
            },
        )
        for duration in (1, 30):
            self.assertEqual(schema({"unlock_duration": duration})["unlock_duration"], duration)
        for duration in (0, 31):
            with self.subTest(duration=duration), self.assertRaises(vol.MultipleInvalid):
                schema({"unlock_duration": duration})

        handler.async_create_entry = Mock(side_effect=lambda **kwargs: kwargs)
        options = schema({"unlock_duration": 5})
        self.assertEqual((await handler.async_step_init(options))["data"], options)
