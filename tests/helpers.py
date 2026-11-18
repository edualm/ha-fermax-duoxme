"""Minimal external-boundary mocks; integration logic is imported unchanged."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from aiohttp import ClientResponseError


COMPONENT_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components" / "fermax_duoxme"
)


class HomeAssistantError(Exception):
    """Stand-in for Home Assistant's exception boundary."""


class ConfigEntryAuthFailed(HomeAssistantError):
    """Credentials were rejected."""


class ConfigEntryError(HomeAssistantError):
    """Setup cannot continue."""


class ConfigEntryNotReady(HomeAssistantError):
    """Setup should be retried."""


@contextmanager
def load_components():
    """Import the production modules with HA and FCM transport APIs mocked."""
    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    class PushReceiver:
        def __init__(self, *args):
            pass

    names = (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.components.binary_sensor",
        "homeassistant.components.camera",
        "homeassistant.components.lock",
        "homeassistant.components.sensor",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.core",
        "homeassistant.data_entry_flow",
        "homeassistant.exceptions",
        "homeassistant.helpers",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.dispatcher",
        "homeassistant.helpers.entity_platform",
        "homeassistant.helpers.selector",
        "homeassistant.helpers.storage",
        "homeassistant.requirements",
        "homeassistant.util",
        "homeassistant.util.dt",
        "push_receiver",
        "push_receiver.android_fcm_register",
        "fermax_test",
    )
    modules = {name: ModuleType(name) for name in names}
    modules["homeassistant.config_entries"].ConfigFlow = ConfigFlow
    modules["homeassistant.config_entries"].OptionsFlow = object
    modules["homeassistant.config_entries"].ConfigEntry = SimpleNamespace
    modules["homeassistant.const"].CONF_USERNAME = "username"
    modules["homeassistant.const"].CONF_PASSWORD = "password"
    modules["homeassistant.core"].callback = lambda function: function
    modules["homeassistant.core"].HomeAssistant = object
    modules["homeassistant.data_entry_flow"].FlowResult = dict
    exceptions = modules["homeassistant.exceptions"]
    for exception in (
        HomeAssistantError, ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
    ):
        setattr(exceptions, exception.__name__, exception)
    modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = Mock()
    selector = modules["homeassistant.helpers.selector"]
    selector.SelectSelector = Mock(return_value=str)
    selector.SelectSelectorConfig = lambda **kwargs: kwargs
    selector.SelectSelectorMode = SimpleNamespace(LIST="list")
    modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = Mock
    dispatcher = modules["homeassistant.helpers.dispatcher"]
    dispatcher.async_dispatcher_connect = Mock()
    dispatcher.async_dispatcher_send = Mock()
    modules["homeassistant.helpers.storage"].Store = Mock(
        side_effect=lambda *args: SimpleNamespace(
            async_load=AsyncMock(return_value=None),
            async_save=AsyncMock(),
            async_remove=AsyncMock(),
        )
    )
    modules["homeassistant.requirements"].async_process_requirements = AsyncMock()
    dt = modules["homeassistant.util.dt"]
    dt.utcnow = Mock(return_value=datetime(2026, 1, 1, tzinfo=timezone.utc))
    dt.parse_datetime = Mock()
    dt.as_utc = Mock()
    for component, entity in (
        ("binary_sensor", "BinarySensorEntity"),
        ("camera", "Camera"),
        ("lock", "LockEntity"),
        ("sensor", "SensorEntity"),
    ):
        setattr(modules[f"homeassistant.components.{component}"], entity, type(entity, (), {}))
    modules["homeassistant.components.binary_sensor"].BinarySensorDeviceClass = (
        SimpleNamespace(OCCUPANCY="occupancy", CONNECTIVITY="connectivity")
    )
    modules["homeassistant.components.sensor"].SensorDeviceClass = SimpleNamespace(
        TIMESTAMP="timestamp"
    )
    modules["push_receiver"].PushReceiver = PushReceiver
    modules["push_receiver.android_fcm_register"].AndroidFCM = Mock()
    package = modules["fermax_test"]
    package.__path__ = [str(COMPONENT_PATH)]
    with patch.dict(sys.modules, modules):
        components = {
            name: importlib.import_module(f"fermax_test.{name}")
            for name in (
                "api", "const", "push", "config_flow", "camera", "sensor",
                "binary_sensor", "lock", "webrtc",
            )
        }
        # Execute setup functions in the same package as their relative imports.
        spec = importlib.util.spec_from_file_location(
            "fermax_test", COMPONENT_PATH / "__init__.py"
        )
        assert spec is not None and spec.loader is not None
        integration = importlib.util.module_from_spec(spec)
        sys.modules["fermax_test"] = integration
        spec.loader.exec_module(integration)
        yield SimpleNamespace(integration=integration, **components)


def response(status=200, payload=None, body=b"", content_type="application/json"):
    """Build an aiohttp response/context manager with real HTTP exceptions."""
    result = MagicMock()
    result.status = status
    result.content_type = content_type
    result.json = AsyncMock(return_value=payload)
    result.read = AsyncMock(return_value=body)
    result.__aenter__.return_value = result
    if status >= 400:
        result.raise_for_status.side_effect = ClientResponseError(
            SimpleNamespace(real_url="https://fermax.invalid"), (), status=status
        )
    return result


class IntegrationTestCase(unittest.IsolatedAsyncioTestCase):
    """Provide fresh production modules and tracked asynchronous tasks."""

    def setUp(self):
        self.modules = self.enterContext(load_components())
        self.tasks = []

        def create_task(coro, *args, **kwargs):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

        async def executor(function, *args):
            return function(*args)

        self.hass = SimpleNamespace(
            data={},
            loop=asyncio.get_event_loop(),
            bus=SimpleNamespace(async_fire=Mock()),
            async_create_task=Mock(side_effect=create_task),
            async_add_executor_job=AsyncMock(side_effect=executor),
            config_entries=SimpleNamespace(
                async_unload=AsyncMock(return_value=True),
                async_unload_platforms=AsyncMock(return_value=True),
                async_forward_entry_setups=AsyncMock(),
                async_reload=AsyncMock(),
                async_schedule_reload=Mock(),
            ),
        )
        self.entry = SimpleNamespace(
            entry_id="existing-entry",
            unique_id="user@example.invalid",
            title="user@example.invalid",
            data={
                "username": "user@example.invalid",
                "password": "test-password",
                "client_id": "test-client",
                "client_secret": "test-secret",
                "fcm_api_key": "test-api-key",
                "fcm_project_id": "test-project",
                "fcm_gcm_sender_id": "test-sender",
                "fcm_gms_app_id": "test-app-id",
                "fcm_android_package_name": "com.example.fermax",
            },
            options={},
            async_on_unload=Mock(),
            add_update_listener=Mock(),
        )
        self.session = Mock()

    def make_listener(self):
        """Create the actual listener without starting network activity."""
        listener = self.modules.push.FermaxPushListener(
            self.hass, self.session, self.entry
        )
        listener._oauth_token = {"access_token": "test-access", "expires_at": 10**12}
        listener._device_id = "test-device"
        return listener

    def attach_entity(self, entity):
        """Mock only the HA entity lifecycle hooks used by the integration."""
        entity.hass = self.hass
        entity.async_write_ha_state = Mock()
        entity.async_on_remove = Mock()
        return entity

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)
        errors = [
            result for result in results
            if isinstance(result, BaseException)
            and not isinstance(result, asyncio.CancelledError)
        ]
        self.assertEqual(errors, [], "Unexpected background task failure")
