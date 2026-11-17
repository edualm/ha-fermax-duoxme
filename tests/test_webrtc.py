"""Regression tests for go2rtc frame capture without Home Assistant."""

import asyncio
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "fermax_duoxme"
    / "webrtc.py"
)
SPEC = importlib.util.spec_from_file_location("fermax_webrtc", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
webrtc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(webrtc)


class Go2rtcFrameTests(unittest.IsolatedAsyncioTestCase):
    """Exercise stream registration, ingest and cleanup."""

    async def _capture(self, ingest_status=201):
        streams = {}
        requests = []
        session = object()
        hass = SimpleNamespace(
            data={"go2rtc": SimpleNamespace(url="http://go2rtc", session=session)}
        )
        answer = (
            b"v=0\r\na=fingerprint:sha-256 AA:BB\r\na=setup:active\r\n"
        )

        @asynccontextmanager
        async def call(*args):
            yield object(), {}, asyncio.Event()

        async def request(actual_session, method, url, **kwargs):
            self.assertIs(actual_session, session)
            params = kwargs["params"]
            requests.append((method, url, params))
            if url.endswith("/api/streams"):
                if method in ("PUT", "PATCH"):
                    # go2rtc returns the stream list when src is missing,
                    # regardless of the request method.
                    if params.get("src"):
                        streams[params["name"]] = params["src"]
                    return 200, "application/json", b"{}"
                if method == "DELETE":
                    streams.pop(params["src"], None)
                    return 200, "text/plain", b""
            if url.endswith("/api/webrtc"):
                if params["dst"] not in streams:
                    return 404, "text/plain", b"stream not found\n"
                return ingest_status, "application/sdp", answer
            if url.endswith("/api/frame.jpeg"):
                self.assertIn(params["src"], streams)
                return 200, "image/jpeg", b"\xff\xd8frame\xff\xd9"
            self.fail(f"Unexpected request: {method} {url}")

        resume = AsyncMock()
        with (
            patch.object(webrtc, "_fermax_call", call),
            patch.object(
                webrtc, "_consume_video", AsyncMock(return_value=({}, {}))
            ),
            patch.object(webrtc, "_build_sdp_video_only", return_value="offer"),
            patch.object(webrtc, "_connect_and_resume", resume),
            patch.object(webrtc, "_go2rtc_request", request),
        ):
            result = await webrtc.async_get_go2rtc_frame(
                hass, "room", "https://signal.invalid", "auth", "app"
            )
        self.assertEqual(streams, {})
        return result, requests, resume

    async def test_registers_stream_before_ingest_and_removes_it_after_frame(self):
        result, requests, resume = await self._capture()

        self.assertEqual(result, b"\xff\xd8frame\xff\xd9")
        self.assertEqual(
            [method for method, _, _ in requests], ["PATCH", "POST", "GET", "DELETE"]
        )
        stream = requests[0][2]["name"]
        self.assertEqual(requests[0][2]["src"], f"ffmpeg:{stream}#video=mjpeg")
        self.assertEqual(requests[1][2], {"dst": stream})
        self.assertEqual(requests[-1][2], {"src": stream})
        resume.assert_awaited_once()

    async def test_removes_stream_when_ingest_is_rejected(self):
        with self.assertLogs(webrtc.__name__, level="ERROR") as logs:
            result, requests, resume = await self._capture(ingest_status=500)

        self.assertIsNone(result)
        self.assertEqual(
            [method for method, _, _ in requests], ["PATCH", "POST", "DELETE"]
        )
        self.assertIn("go2rtc rejected the SDP offer: HTTP 500", logs.output[0])
        resume.assert_not_awaited()
