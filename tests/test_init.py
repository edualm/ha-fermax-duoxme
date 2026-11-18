"""Exercise integration setup, error mapping and unload behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import ClientConnectionError

from tests.helpers import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    IntegrationTestCase,
)


class SetupTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.integration = self.modules.integration
        self.listener = SimpleNamespace(
            async_setup=AsyncMock(),
            async_fetch_pairings=AsyncMock(),
            start=Mock(),
            stop=AsyncMock(),
            loaded_platforms=[],
        )
        self.enterContext(
            patch.object(self.integration, "FermaxPushListener", return_value=self.listener)
        )

    async def test_push_setup_exposes_all_platforms_after_listener_is_ready(self):
        order = []
        self.listener.async_setup.side_effect = lambda: order.append("setup")
        self.listener.start.side_effect = lambda: order.append("start")
        manager = self.hass.config_entries
        manager.async_forward_entry_setups.side_effect = lambda *args: order.append("platforms")

        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))
        self.assertEqual(order, ["setup", "start", "platforms"])
        self.assertEqual(
            self.listener.loaded_platforms, ["binary_sensor", "camera", "lock", "sensor"]
        )
        self.assertIs(self.hass.data["fermax_duoxme"][self.entry.entry_id], self.listener)
        self.entry.add_update_listener.assert_called_once_with(self.integration.update_listener)

    async def test_lock_only_setup_does_not_register_or_start_push(self):
        self.entry.options["enable_push_notifications"] = False
        self.assertTrue(await self.integration.async_setup_entry(self.hass, self.entry))

        self.listener.async_fetch_pairings.assert_awaited_once()
        self.listener.async_setup.assert_not_awaited()
        self.listener.start.assert_not_called()
        self.assertEqual(self.listener.loaded_platforms, ["lock"])

    async def test_setup_errors_clean_up_and_map_to_home_assistant_exceptions(self):
        cases = (
            (self.modules.push.FermaxAuthError("rejected"), ConfigEntryAuthFailed),
            (self.modules.push.FermaxFcmRegistrationError("rejected"), ConfigEntryError),
            (ClientConnectionError("offline"), ConfigEntryNotReady),
            (TimeoutError("timeout"), ConfigEntryNotReady),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.listener.stop.reset_mock()
                self.listener.async_setup.side_effect = error
                with self.assertRaises(expected):
                    await self.integration.async_setup_entry(self.hass, self.entry)
                self.listener.stop.assert_awaited_once()
                self.listener.start.assert_not_called()
                self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()

    async def test_unload_uses_loaded_platforms_not_changed_options(self):
        self.listener.loaded_platforms = ["binary_sensor", "camera", "lock", "sensor"]
        self.entry.options["enable_push_notifications"] = False
        self.hass.data["fermax_duoxme"] = {self.entry.entry_id: self.listener}

        self.assertTrue(await self.integration.async_unload_entry(self.hass, self.entry))
        self.hass.config_entries.async_unload_platforms.assert_awaited_once_with(
            self.entry, self.listener.loaded_platforms
        )
        self.listener.stop.assert_awaited_once()
        self.assertNotIn(self.entry.entry_id, self.hass.data["fermax_duoxme"])

    async def test_failed_unload_keeps_listener_and_entry_data(self):
        self.hass.data["fermax_duoxme"] = {self.entry.entry_id: self.listener}
        self.hass.config_entries.async_unload_platforms.return_value = False

        self.assertFalse(await self.integration.async_unload_entry(self.hass, self.entry))
        self.listener.stop.assert_not_awaited()
        self.assertIn(self.entry.entry_id, self.hass.data["fermax_duoxme"])

    async def test_option_update_requests_entry_reload(self):
        await self.integration.update_listener(self.hass, self.entry)
        self.hass.config_entries.async_reload.assert_awaited_once_with(self.entry.entry_id)
