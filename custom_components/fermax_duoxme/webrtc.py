"""WebRTC logic for capturing a frame from a Fermax call.

FERMAX calls are served by a mediasoup SFU with a custom socket.io signalling
protocol. The signalling is shared; the media side has two backends:

* aiortc: an in-process Python WebRTC peer decodes the first video frame.
* go2rtc: the go2rtc server bundled with Home Assistant receives the media
  (WHIP-style ingest on ``/api/webrtc?dst=``) and produces the JPEG through
  ``/api/frame.jpeg``. No extra Python packages are needed.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from io import BytesIO
import logging
import re
from secrets import token_hex
import time
from typing import TYPE_CHECKING, Any, Optional

from aiohttp import ClientError, ClientTimeout
import socketio
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

JOIN_TIMEOUT = 10
CONSUME_TIMEOUT = 10
HANGUP_TIMEOUT = 5
FRAME_TIMEOUT = 15
GO2RTC_REQUEST_TIMEOUT = 10

# Home Assistant stores either a legacy URL string or an object containing
# `url` and an already authenticated aiohttp `session`.
GO2RTC_DATA_KEY = "go2rtc"

# New, more detailed RTP capabilities from the updated script
RTP_CAPABILITIES = {
    "codecs": [
        {
            "kind": "audio",
            "mimeType": "audio/PCMA",
            "clockRate": 8000,
            "preferredPayloadType": 8,
            "channels": 1,
            "rtcpFeedback": [],
            "parameters": {},
        },
        {
            "kind": "video",
            "mimeType": "video/H264",
            "clockRate": 90000,
            "preferredPayloadType": 102,
            "rtcpFeedback": [
                {"type": "goog-remb", "parameter": ""},
                {"type": "transport-cc", "parameter": ""},
                {"type": "ccm", "parameter": "fir"},
                {"type": "nack", "parameter": ""},
                {"type": "nack", "parameter": "pli"},
            ],
            "parameters": {
                "packetization-mode": 1,
                "profile-level-id": "42e01f",
                "level-asymmetry-allowed": 1,
            },
        },
        {
            "kind": "video",
            "mimeType": "video/rtx",
            "clockRate": 90000,
            "preferredPayloadType": 103,
            "rtcpFeedback": [],
            "parameters": {"apt": 102},
        },
    ],
    "headerExtensions": [
        {"kind": "audio", "uri": "urn:ietf:params:rtp-hdrext:sdes:mid", "preferredId": 1, "direction": "sendrecv"},
        {"kind": "video", "uri": "urn:ietf:params:rtp-hdrext:sdes:mid", "preferredId": 1, "direction": "sendrecv"},
        {"kind": "video", "uri": "http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time", "preferredId": 4, "direction": "sendrecv"},
        {"kind": "video", "uri": "http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01", "preferredId": 5, "direction": "sendrecv"},
        {"kind": "audio", "uri": "urn:ietf:params:rtp-hdrext:ssrc-audio-level", "preferredId": 10, "direction": "sendrecv"},
        {"kind": "video", "uri": "urn:3gpp:video-orientation", "preferredId": 11, "direction": "sendrecv"},
        {"kind": "video", "uri": "urn:ietf:params:rtp-hdrext:toffset", "preferredId": 12, "direction": "sendrecv"},
    ],
}


# --------------------------------------------------------------------------
# Shared FERMAX signalling
# --------------------------------------------------------------------------


@asynccontextmanager
async def _fermax_call(
    room_id: str, socket_url: str, auth_token: str, app_token: str
) -> AsyncIterator[tuple[socketio.AsyncClient, dict[str, Any], asyncio.Event]]:
    """Join a FERMAX call and always hang up on exit.

    Yields the socket.io client, the join result (server info) and an event
    that is set when FERMAX reports that the call has ended.
    """
    sio = socketio.AsyncClient(logger=False, engineio_logger=False)
    call_ended = asyncio.Event()

    @sio.event
    async def end_up(data):
        _LOGGER.debug("Received end_up from Fermax: %s", data)
        call_ended.set()

    try:
        await sio.connect(socket_url, transports=["polling"], socketio_path="/socket.io")
        join_response = await sio.call(
            "join_call",
            {
                "appToken": app_token,
                "roomId": room_id,
                "fermaxOauthToken": auth_token,
                "protocolVersion": "0.8.2",
            },
            timeout=JOIN_TIMEOUT,
        )
        yield sio, (join_response or {}).get("result", {}), call_ended
    finally:
        if sio.connected:
            try:
                await sio.call("hang_up", "{}", timeout=HANGUP_TIMEOUT)
            except (SocketIOTimeoutError, asyncio.TimeoutError):
                _LOGGER.warning("Timed out waiting for hang_up ACK.")
            except Exception as err:  # noqa: BLE001 - best effort cleanup
                _LOGGER.debug("Error sending hang_up: %r", err)
            await sio.disconnect()


async def _consume_video(
    sio: socketio.AsyncClient, server_info: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask mediasoup for a video consumer. Returns (transport, consumer)."""
    transport = server_info["recvTransportVideo"]
    consumer = await sio.call(
        "transport_consume",
        {
            "transportId": transport["id"],
            "producerId": server_info["producerIdVideo"],
            "rtpCapabilities": RTP_CAPABILITIES,
        },
        timeout=CONSUME_TIMEOUT,
    )
    return transport, consumer


async def _connect_and_resume(
    sio: socketio.AsyncClient,
    transport: dict[str, Any],
    consumer: dict[str, Any],
    dtls_parameters: dict[str, Any],
) -> None:
    """Send our DTLS parameters to mediasoup and start the video consumer."""
    await sio.emit(
        "transport_connect",
        {"transportId": transport["id"], "dtlsParameters": dtls_parameters},
    )
    await sio.emit("consumer_resume", {"consumerId": consumer["result"]["id"]})


def _build_sdp_video_only(
    video_transport_info: dict[str, Any],
    video_consumer_params: dict[str, Any],
    direction: str = "sendrecv",
) -> str:
    """Construct the SDP offer that describes the mediasoup side of the call."""
    ice = video_transport_info["iceParameters"]
    dtls = video_transport_info["dtlsParameters"]

    sdp = "v=0\r\n"
    sdp += "o=- 5133548076695286524 2 IN IP4 127.0.0.1\r\n"
    sdp += "s=-\r\n"
    sdp += "t=0 0\r\n"
    # mediasoup transports are always ICE-Lite. Announcing it makes the local
    # peer take the controlling role and nominate the candidate pair; without
    # it both sides wait for the other to nominate and ICE never completes.
    if ice.get("iceLite", True):
        sdp += "a=ice-lite\r\n"
    sdp += "a=group:BUNDLE video\r\n"
    sdp += "a=msid-semantic: WMS\r\n"
    sdp += f"a=ice-ufrag:{ice['usernameFragment']}\r\n"
    sdp += f"a=ice-pwd:{ice['password']}\r\n"
    for fp in dtls["fingerprints"]:
        sdp += f"a=fingerprint:{fp['algorithm']} {fp['value'].upper()}\r\n"
    sdp += "a=setup:actpass\r\n"

    # Video Media Section
    vid_rtp = video_consumer_params["result"]["rtpParameters"]
    video_payload_type = vid_rtp["codecs"][0]["payloadType"]
    sdp += f"m=video 9 UDP/TLS/RTP/SAVPF {video_payload_type}\r\n"
    sdp += "c=IN IP4 0.0.0.0\r\n"
    sdp += "a=rtcp-mux\r\n"
    for candidate in video_transport_info.get("iceCandidates", []):
        line = (
            f"a=candidate:{candidate['foundation']} 1 {candidate['protocol']} "
            f"{candidate['priority']} {candidate['ip']} {candidate['port']} "
            f"typ {candidate['type']}"
        )
        if candidate.get("tcpType"):
            line += f" tcptype {candidate['tcpType']}"
        sdp += line + "\r\n"
    sdp += "a=end-of-candidates\r\n"
    sdp += "a=mid:video\r\n"
    sdp += f"a={direction}\r\n"
    for ext in vid_rtp["headerExtensions"]:
        sdp += f"a=extmap:{ext['id']} {ext['uri']}\r\n"
    sdp += f"a=rtpmap:{video_payload_type} H264/90000\r\n"
    sdp += f"a=fmtp:{video_payload_type} level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f\r\n"
    sdp += f"a=ssrc:{vid_rtp['encodings'][0]['ssrc']} cname:{vid_rtp['rtcp']['cname']}\r\n"

    return sdp


# --------------------------------------------------------------------------
# aiortc backend
# --------------------------------------------------------------------------


async def async_get_aiortc_frame(
    room_id: str, socket_url: str, auth_token: str, app_token: str
) -> Optional[bytes]:
    """Capture a single frame using an in-process aiortc peer."""
    from aiortc import RTCPeerConnection, RTCSessionDescription  # noqa: PLC0415

    pc = RTCPeerConnection()
    frame_captured_event = asyncio.Event()
    call_failed_event = asyncio.Event()
    captured_frame_bytes: Optional[bytes] = None
    # Every task we create is tracked here and cancelled/awaited before the
    # peer connection is closed, so nothing is left pending.
    tasks: list[asyncio.Task] = []

    async def _capture_frame(track) -> None:
        nonlocal captured_frame_bytes
        try:
            frame = await track.recv()
            img = frame.to_image()

            with BytesIO() as output:
                img.save(output, format="JPEG")
                captured_frame_bytes = output.getvalue()

            _LOGGER.info("Successfully captured video frame from WebRTC stream (aiortc).")
            frame_captured_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # e.g. MediaStreamError when the track ends
            _LOGGER.warning("Error capturing frame: %r", e)
            call_failed_event.set()

    # Synchronous handlers: pyee wraps coroutine handlers in tasks it does not
    # own, which leaked ("Task was destroyed but it is pending") when the
    # connection was closed while track.recv() was still waiting.
    @pc.on("track")
    def on_track(track):
        if track.kind == "video" and not any(
            t.get_name() == "fermax_capture_frame" for t in tasks
        ):
            tasks.append(
                asyncio.create_task(_capture_frame(track), name="fermax_capture_frame")
            )

    @pc.on("connectionstatechange")
    def on_connectionstatechange():
        _LOGGER.debug("WebRTC connection state: %s", pc.connectionState)
        if pc.connectionState == "failed":
            _LOGGER.warning("WebRTC connection failed.")
            call_failed_event.set()

    try:
        async with _fermax_call(room_id, socket_url, auth_token, app_token) as (
            sio,
            server_info,
            call_ended,
        ):
            try:
                transport, consumer = await _consume_video(sio, server_info)
                sdp = _build_sdp_video_only(transport, consumer, direction="sendrecv")

                await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
                await pc.setLocalDescription(await pc.createAnswer())

                dtls_transport = pc.getTransceivers()[0].receiver.transport
                dtls_parameters = {
                    "role": "client",
                    "fingerprints": [
                        {"algorithm": fp.algorithm, "value": fp.value}
                        for fp in dtls_transport.getLocalParameters().fingerprints
                    ],
                }
                await _connect_and_resume(sio, transport, consumer, dtls_parameters)

                waiters = [
                    asyncio.create_task(frame_captured_event.wait()),
                    asyncio.create_task(call_failed_event.wait()),
                    asyncio.create_task(call_ended.wait()),
                ]
                tasks.extend(waiters)

                done, _ = await asyncio.wait(
                    waiters, return_when=asyncio.FIRST_COMPLETED, timeout=FRAME_TIMEOUT
                )
                if not done:
                    _LOGGER.warning(
                        "Timed out waiting for a video frame (connection state: %s).",
                        pc.connectionState,
                    )
                elif captured_frame_bytes is None:
                    _LOGGER.warning(
                        "Call ended or failed before a video frame was received "
                        "(connection state: %s).",
                        pc.connectionState,
                    )
                return captured_frame_bytes
            finally:
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

    except Exception as e:
        _LOGGER.error("Error during WebRTC frame capture process (aiortc): %r", e)
        return None
    finally:
        if pc.connectionState != "closed":
            await pc.close()


# --------------------------------------------------------------------------
# go2rtc backend
# --------------------------------------------------------------------------

_FINGERPRINT_RE = re.compile(r"^a=fingerprint:(\S+) (\S+)\s*$", re.MULTILINE)
_SETUP_RE = re.compile(r"^a=setup:(\w+)\s*$", re.MULTILINE)
_DTLS_ROLE_FROM_SETUP = {"active": "client", "passive": "server"}


def _dtls_parameters_from_answer(answer_sdp: str) -> dict[str, Any]:
    """Extract the DTLS parameters mediasoup needs from go2rtc's SDP answer."""
    fingerprints = [
        {"algorithm": algorithm.lower(), "value": value}
        for algorithm, value in _FINGERPRINT_RE.findall(answer_sdp)
    ]
    if not fingerprints:
        raise ValueError("go2rtc SDP answer has no DTLS fingerprint")
    setup = _SETUP_RE.search(answer_sdp)
    role = _DTLS_ROLE_FROM_SETUP.get(setup.group(1) if setup else "", "auto")
    return {"role": role, "fingerprints": fingerprints}


def _go2rtc_connection(hass: HomeAssistant) -> tuple[str, Any] | None:
    """Return (base_url, session) of Home Assistant's go2rtc, if available."""
    config = hass.data.get(GO2RTC_DATA_KEY)
    if isinstance(config, str):
        if not config:
            return None
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        return config.rstrip("/"), async_get_clientsession(hass)
    url = getattr(config, "url", None)
    session = getattr(config, "session", None)
    if not url or session is None:
        return None
    return url.rstrip("/"), session


async def _go2rtc_request(session, method: str, url: str, **kwargs):
    """Run a request against go2rtc and return (status, content_type, body)."""
    timeout = kwargs.pop("timeout", ClientTimeout(total=GO2RTC_REQUEST_TIMEOUT))
    async with session.request(method, url, timeout=timeout, **kwargs) as resp:
        return resp.status, resp.content_type, await resp.read()


async def _go2rtc_wait_frame(
    session, base_url: str, stream: str, call_ended: asyncio.Event
) -> Optional[bytes]:
    """Poll go2rtc for a JPEG frame until it arrives, the call ends or we time out."""
    deadline = time.monotonic() + FRAME_TIMEOUT
    last_error = "no attempt"
    while (remaining := deadline - time.monotonic()) > 0:
        if call_ended.is_set():
            _LOGGER.warning("Call ended before go2rtc produced a frame.")
            return None
        try:
            status, content_type, body = await _go2rtc_request(
                session,
                "GET",
                f"{base_url}/api/frame.jpeg",
                params={"src": stream},
                timeout=ClientTimeout(total=max(remaining, 1)),
            )
            if status == 200 and body and content_type.startswith("image/"):
                return body
            last_error = f"HTTP {status}: {body[:200]!r}"
        except (ClientError, asyncio.TimeoutError) as err:
            last_error = repr(err)
        _LOGGER.debug("go2rtc frame not ready yet (%s)", last_error)
        await asyncio.sleep(1)
    _LOGGER.warning("Timed out waiting for a frame from go2rtc (last: %s).", last_error)
    return None


async def async_get_go2rtc_frame(
    hass: HomeAssistant,
    room_id: str,
    socket_url: str,
    auth_token: str,
    app_token: str,
) -> Optional[bytes]:
    """Capture a single frame by letting Home Assistant's go2rtc receive the call."""
    connection = _go2rtc_connection(hass)
    if connection is None:
        _LOGGER.warning(
            "go2rtc is not available in Home Assistant (it needs `default_config:` "
            "or a `go2rtc:` entry in configuration.yaml)."
        )
        return None
    base_url, session = connection
    stream = f"fermax_duoxme_{token_hex(4)}"
    stream_created = False

    try:
        async with _fermax_call(room_id, socket_url, auth_token, app_token) as (
            sio,
            server_info,
            call_ended,
        ):
            transport, consumer = await _consume_video(sio, server_info)
            # mediasoup only sends, so the offer must say so for go2rtc to
            # treat it as an incoming (producer) connection.
            offer = _build_sdp_video_only(transport, consumer, direction="sendonly")

            # Without src, /api/streams only lists streams, even for PUT.
            # PATCH registers a temporary stream without saving configuration;
            # its FFmpeg source converts the incoming WebRTC video to JPEG.
            status, _, body = await _go2rtc_request(
                session,
                "PATCH",
                f"{base_url}/api/streams",
                params={"name": stream, "src": f"ffmpeg:{stream}#video=mjpeg"},
            )
            if status >= 400:
                raise RuntimeError(f"go2rtc could not create stream: HTTP {status} {body[:200]!r}")
            stream_created = True

            status, _, body = await _go2rtc_request(
                session,
                "POST",
                f"{base_url}/api/webrtc",
                params={"dst": stream},
                data=offer,
                headers={"Content-Type": "application/sdp"},
            )
            if status >= 400:
                raise RuntimeError(f"go2rtc rejected the SDP offer: HTTP {status} {body[:200]!r}")
            answer = body.decode()
            _LOGGER.debug("go2rtc SDP answer:\n%s", answer)

            await _connect_and_resume(
                sio, transport, consumer, _dtls_parameters_from_answer(answer)
            )

            frame = await _go2rtc_wait_frame(session, base_url, stream, call_ended)
            if frame:
                _LOGGER.info("Successfully captured video frame from WebRTC stream (go2rtc).")
            return frame

    except Exception as e:
        _LOGGER.error("Error during WebRTC frame capture process (go2rtc): %r", e)
        return None
    finally:
        if stream_created:
            with suppress(ClientError, asyncio.TimeoutError):
                await _go2rtc_request(
                    session, "DELETE", f"{base_url}/api/streams", params={"src": stream}
                )
