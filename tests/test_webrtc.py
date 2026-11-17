"""Test WebRTC signalling, SDP and backend cleanup without media services."""

import asyncio
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from aiohttp import ClientConnectionError

from tests.helpers import response


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


def video_parameters():
    """Representative mediasoup transport and consumer responses."""
    transport = {
        "id": "transport",
        "iceParameters": {"usernameFragment": "user", "password": "password"},
        "dtlsParameters": {"fingerprints": [{"algorithm": "sha-256", "value": "aa:bb"}]},
        "iceCandidates": [
            {"foundation": "1", "protocol": "udp", "priority": 100,
             "ip": "192.0.2.1", "port": 4000, "type": "host"},
            {"foundation": "2", "protocol": "tcp", "priority": 90,
             "ip": "192.0.2.1", "port": 4001, "type": "host", "tcpType": "passive"},
        ],
    }
    consumer = {
        "result": {
            "id": "consumer",
            "rtpParameters": {
                "codecs": [{"payloadType": 101}],
                "headerExtensions": [{"id": 4, "uri": "urn:example:extension"}],
                "encodings": [{"ssrc": 1234}],
                "rtcp": {"cname": "camera"},
            },
        }
    }
    return transport, consumer


class SignallingTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_joins_with_app_protocol_and_hangs_up_after_body_error(self):
        handlers = {}
        sio = Mock(
            connected=True, connect=AsyncMock(), disconnect=AsyncMock(),
            call=AsyncMock(return_value={"result": {"server": "info"}}),
        )
        sio.event.side_effect = lambda fn: handlers.setdefault(fn.__name__, fn)
        with patch.object(webrtc.socketio, "AsyncClient", return_value=sio):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                async with webrtc._fermax_call("room", "url", "auth", "app") as (
                    actual_sio, info, ended
                ):
                    self.assertIs(actual_sio, sio)
                    self.assertEqual(info, {"server": "info"})
                    await handlers["end_up"]({})
                    self.assertTrue(ended.is_set())
                    raise RuntimeError("capture failed")

        sio.connect.assert_awaited_once_with(
            "url", transports=["polling"], socketio_path="/socket.io"
        )
        self.assertEqual(
            sio.call.await_args_list,
            [
                call("join_call", {"appToken": "app", "roomId": "room",
                     "fermaxOauthToken": "auth", "protocolVersion": "0.8.2"}, timeout=10),
                call("hang_up", "{}", timeout=5),
            ],
        )
        sio.disconnect.assert_awaited_once()

    async def test_hangup_timeout_still_disconnects_socket(self):
        sio = Mock(
            connected=True, connect=AsyncMock(), disconnect=AsyncMock(),
            call=AsyncMock(side_effect=[{}, webrtc.SocketIOTimeoutError()]),
        )
        with (
            patch.object(webrtc.socketio, "AsyncClient", return_value=sio),
            self.assertLogs(webrtc.__name__, level="WARNING"),
        ):
            async with webrtc._fermax_call("room", "url", "auth", "app"):
                pass
        sio.disconnect.assert_awaited_once()

    async def test_consumer_and_resume_use_video_transport_and_dtls_answer(self):
        transport, consumer = video_parameters()
        sio = Mock(call=AsyncMock(return_value=consumer), emit=AsyncMock())
        actual = await webrtc._consume_video(
            sio, {"recvTransportVideo": transport, "producerIdVideo": "producer"}
        )
        self.assertEqual(actual, (transport, consumer))
        self.assertEqual(sio.call.await_args.args[1]["producerId"], "producer")
        self.assertEqual(sio.call.await_args.args[1]["transportId"], "transport")
        self.assertIn("codecs", sio.call.await_args.args[1]["rtpCapabilities"])

        dtls = {"role": "client", "fingerprints": []}
        await webrtc._connect_and_resume(sio, transport, consumer, dtls)
        self.assertEqual(
            sio.emit.await_args_list,
            [
                call("transport_connect", {"transportId": "transport", "dtlsParameters": dtls}),
                call("consumer_resume", {"consumerId": "consumer"}),
            ],
        )

    async def test_sdp_announces_ice_lite_candidates_codec_direction_and_ssrc(self):
        transport, consumer = video_parameters()
        sdp = webrtc._build_sdp_video_only(transport, consumer, direction="sendonly")
        for line in (
            "a=ice-lite", "a=ice-ufrag:user", "a=ice-pwd:password",
            "a=fingerprint:sha-256 AA:BB", "a=sendonly",
            "m=video 9 UDP/TLS/RTP/SAVPF 101", "a=rtpmap:101 H264/90000",
            "a=candidate:1 1 udp 100 192.0.2.1 4000 typ host",
            "a=candidate:2 1 tcp 90 192.0.2.1 4001 typ host tcptype passive",
            "a=ssrc:1234 cname:camera", "a=extmap:4 urn:example:extension",
        ):
            self.assertIn(line + "\r\n", sdp)
        self.assertNotIn("m=audio", sdp)
        transport["iceParameters"]["iceLite"] = False
        self.assertNotIn("a=ice-lite", webrtc._build_sdp_video_only(transport, consumer))

    async def test_dtls_answer_roles_and_multiple_fingerprints(self):
        fingerprints = (
            "a=fingerprint:SHA-256 AA:BB\r\n"
            "a=fingerprint:sha-512 CC:DD\r\n"
        )
        for setup, role in (("active", "client"), ("passive", "server"), ("actpass", "auto"), ("", "auto")):
            with self.subTest(setup=setup):
                result = webrtc._dtls_parameters_from_answer(
                    fingerprints + (f"a=setup:{setup}\r\n" if setup else "")
                )
                self.assertEqual(
                    result,
                    {"role": role, "fingerprints": [
                        {"algorithm": "sha-256", "value": "AA:BB"},
                        {"algorithm": "sha-512", "value": "CC:DD"},
                    ]},
                )
        with self.assertRaisesRegex(ValueError, "no DTLS fingerprint"):
            webrtc._dtls_parameters_from_answer("v=0\r\na=setup:active\r\n")


class Go2rtcRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_reads_bytes_and_honors_timeout_override(self):
        session = Mock()
        session.request.return_value = response(body=b"frame", content_type="image/jpeg")
        self.assertEqual(
            await webrtc._go2rtc_request(session, "GET", "url"),
            (200, "image/jpeg", b"frame"),
        )
        self.assertEqual(session.request.call_args.kwargs["timeout"].total, 10)
        timeout = webrtc.ClientTimeout(total=2)
        await webrtc._go2rtc_request(session, "GET", "url", timeout=timeout)
        self.assertIs(session.request.call_args.kwargs["timeout"], timeout)

    async def test_polling_retries_connection_error_wrong_content_and_empty_image(self):
        request = AsyncMock(side_effect=[
            ClientConnectionError("offline"),
            (200, "application/json", b"not-an-image"),
            (200, "image/jpeg", b""),
            (200, "image/jpeg", b"jpeg"),
        ])
        with (
            patch.object(webrtc, "_go2rtc_request", request),
            patch.object(webrtc.time, "monotonic", return_value=0),
            patch.object(webrtc.asyncio, "sleep", new_callable=AsyncMock) as sleep,
        ):
            result = await webrtc._go2rtc_wait_frame(object(), "url", "stream", asyncio.Event())
        self.assertEqual(result, b"jpeg")
        self.assertEqual(request.await_count, 4)
        self.assertEqual(sleep.await_count, 3)

    async def test_polling_stops_at_deadline_or_when_call_has_ended(self):
        for ended in (False, True):
            with self.subTest(ended=ended):
                event = asyncio.Event()
                if ended:
                    event.set()
                with (
                    patch.object(webrtc, "_go2rtc_request", new_callable=AsyncMock) as request,
                    patch.object(webrtc.time, "monotonic", side_effect=[0, 0, 16]),
                    patch.object(webrtc.asyncio, "sleep", new_callable=AsyncMock),
                    self.assertLogs(webrtc.__name__, level="WARNING"),
                ):
                    request.return_value = (503, "text/plain", b"not-ready")
                    self.assertIsNone(
                        await webrtc._go2rtc_wait_frame(object(), "url", "stream", event)
                    )
                self.assertEqual(request.await_count, 0 if ended else 1)


class Go2rtcCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.exited = False
        self.hass = SimpleNamespace(
            data={"go2rtc": SimpleNamespace(url="http://go2rtc/", session=object())}
        )

        @asynccontextmanager
        async def joined(*args):
            try:
                yield object(), {}, asyncio.Event()
            finally:
                self.exited = True

        self.request = self.enterContext(
            patch.object(webrtc, "_go2rtc_request", new_callable=AsyncMock)
        )
        self.request.side_effect = [
            (200, "application/json", b"{}"),
            (201, "application/sdp", b"a=fingerprint:sha-256 AA:BB\r\na=setup:active\r\n"),
            (200, "text/plain", b""),
        ]
        self.enterContext(patch.object(webrtc, "_fermax_call", joined))
        self.enterContext(patch.object(
            webrtc, "_consume_video", new_callable=AsyncMock, return_value=({}, {})
        ))
        self.enterContext(patch.object(webrtc, "_build_sdp_video_only", return_value="offer"))
        self.resume = self.enterContext(
            patch.object(webrtc, "_connect_and_resume", new_callable=AsyncMock)
        )
        self.frame = self.enterContext(patch.object(
            webrtc, "_go2rtc_wait_frame", new_callable=AsyncMock, return_value=b"frame"
        ))

    async def capture(self):
        return await webrtc.async_get_go2rtc_frame(self.hass, "room", "url", "auth", "app")

    async def test_unavailable_go2rtc_does_not_start_capture(self):
        self.hass.data.clear()
        with self.assertLogs(webrtc.__name__, level="WARNING"):
            self.assertIsNone(await self.capture())
        self.request.assert_not_awaited()

    async def test_failed_registration_does_not_delete_a_stream_it_did_not_create(self):
        self.request.side_effect = [(500, "text/plain", b"failed")]
        with self.assertLogs(webrtc.__name__, level="ERROR"):
            self.assertIsNone(await self.capture())
        self.assertEqual(self.request.await_count, 1)
        self.assertTrue(self.exited)

    async def test_invalid_sdp_deletes_stream_without_resuming_consumer(self):
        self.request.side_effect = [
            (200, "application/json", b"{}"),
            (201, "application/sdp", b"missing fingerprint"),
            (200, "text/plain", b""),
        ]
        with self.assertLogs(webrtc.__name__, level="ERROR"):
            self.assertIsNone(await self.capture())
        self.assertEqual(
            [args.args[1] for args in self.request.await_args_list],
            ["PATCH", "POST", "DELETE"],
        )
        self.resume.assert_not_awaited()

    async def test_cancellation_propagates_after_hangup_and_stream_deletion(self):
        self.frame.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.capture()
        self.assertTrue(self.exited)
        self.assertEqual(self.request.await_args.args[1], "DELETE")

    async def test_cleanup_network_failure_does_not_discard_captured_frame(self):
        self.request.side_effect = [
            (200, "application/json", b"{}"),
            (201, "application/sdp", b"a=fingerprint:sha-256 AA:BB\r\n"),
            ClientConnectionError("offline"),
        ]
        self.assertEqual(await self.capture(), b"frame")
        self.assertTrue(self.exited)


class AiortcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handlers = {}
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.ended = asyncio.Event()
        self.sio = Mock(emit=AsyncMock())
        self.peer = Mock(
            connectionState="connected", setRemoteDescription=AsyncMock(),
            setLocalDescription=AsyncMock(), createAnswer=AsyncMock(), close=AsyncMock(),
        )

        def on(event):
            return lambda fn: self.handlers.setdefault(event, fn)

        self.peer.on.side_effect = on
        dtls = Mock()
        dtls.getLocalParameters.return_value.fingerprints = [
            SimpleNamespace(algorithm="sha-256", value="AA:BB")
        ]
        self.peer.getTransceivers.return_value = [
            SimpleNamespace(receiver=SimpleNamespace(transport=dtls))
        ]
        self.track = Mock(kind="video", recv=AsyncMock(return_value=Mock()))
        frame = self.track.recv.return_value
        frame.to_image.return_value.save.side_effect = (
            lambda output, format: output.write(b"jpeg")
        )

        async def remote_description(description):
            self.handlers["track"](SimpleNamespace(kind="audio"))
            self.handlers["track"](self.track)
            self.handlers["track"](self.track)

        self.peer.setRemoteDescription.side_effect = remote_description
        aiortc = ModuleType("aiortc")
        aiortc.RTCPeerConnection = Mock(return_value=self.peer)
        aiortc.RTCSessionDescription = lambda **kwargs: SimpleNamespace(**kwargs)
        self.enterContext(patch.dict(sys.modules, {"aiortc": aiortc}))

        @asynccontextmanager
        async def joined(*args):
            yield self.sio, {}, self.ended

        self.enterContext(patch.object(webrtc, "_fermax_call", joined))
        self.enterContext(patch.object(
            webrtc, "_consume_video", new_callable=AsyncMock, return_value=video_parameters()
        ))

    async def capture(self):
        return await webrtc.async_get_aiortc_frame("room", "url", "auth", "app")

    async def waiting_frame(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()

    async def test_video_frame_is_encoded_once_and_peer_is_closed(self):
        self.assertEqual(await self.capture(), b"jpeg")
        self.track.recv.assert_awaited_once()
        self.peer.close.assert_awaited_once()
        dtls = self.sio.emit.await_args_list[0].args[1]["dtlsParameters"]
        self.assertEqual(
            dtls, {"role": "client", "fingerprints": [{"algorithm": "sha-256", "value": "AA:BB"}]}
        )

    async def test_call_end_cancels_pending_frame_receive_and_closes_peer(self):
        self.track.recv.side_effect = self.waiting_frame
        self.ended.set()
        with self.assertLogs(webrtc.__name__, level="WARNING"):
            self.assertIsNone(await self.capture())
        self.assertTrue(self.cancelled.is_set())
        self.peer.close.assert_awaited_once()

    async def test_negotiation_error_returns_none_and_closes_peer(self):
        self.peer.setRemoteDescription.side_effect = RuntimeError("invalid offer")
        with self.assertLogs(webrtc.__name__, level="ERROR"):
            self.assertIsNone(await self.capture())
        self.peer.close.assert_awaited_once()

    async def test_external_cancellation_cancels_frame_task_and_closes_peer(self):
        self.track.recv.side_effect = self.waiting_frame
        task = asyncio.create_task(self.capture())
        try:
            await asyncio.wait_for(self.started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.cancelled.is_set())
            self.peer.close.assert_awaited_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
