"""WebRTC logic for capturing a frame from a Fermax call."""
import asyncio
import logging
from io import BytesIO
from typing import Optional

import socketio
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from PIL import Image

_LOGGER = logging.getLogger(__name__)

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

async def async_get_webrtc_frame(
    room_id: str, socket_url: str, auth_token: str, app_token: str
) -> Optional[bytes]:
    """Connect to a call via WebRTC and capture a single frame."""
    sio = socketio.AsyncClient(logger=False, engineio_logger=False)
    pc = RTCPeerConnection()
    frame_captured_event = asyncio.Event()
    call_failed_event = asyncio.Event()
    captured_frame_bytes: Optional[bytes] = None

    @pc.on("track")
    async def on_track(track):
        nonlocal captured_frame_bytes
        if track.kind == "video" and not frame_captured_event.is_set():
            try:
                frame = await track.recv()
                img = frame.to_image()
                
                with BytesIO() as output:
                    img.save(output, format="JPEG")
                    captured_frame_bytes = output.getvalue()
                
                _LOGGER.info("Successfully captured video frame from WebRTC stream.")
                frame_captured_event.set()
            except Exception as e:
                _LOGGER.error("Error capturing frame: %s", e)
                call_failed_event.set()

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        if pc.connectionState == "failed":
            _LOGGER.error("WebRTC connection failed.")
            call_failed_event.set()

    @sio.event
    async def end_up(_):
        if not frame_captured_event.is_set():
            call_failed_event.set()

    try:
        await sio.connect(socket_url, transports=["polling"], socketio_path="/socket.io")
        
        join_response_future = asyncio.Future()
        await sio.emit(
            "join_call",
            {
                "appToken": app_token,
                "roomId": room_id,
                "fermaxOauthToken": auth_token,
                "protocolVersion": "0.8.2",
            },
            callback=lambda data: join_response_future.set_result(data),
        )

        join_response = await asyncio.wait_for(join_response_future, timeout=10)
        await _setup_webrtc_handshake_video_only(sio, pc, join_response.get("result", {}))

        success_task = asyncio.create_task(frame_captured_event.wait())
        failure_task = asyncio.create_task(call_failed_event.wait())
        
        await asyncio.wait([success_task, failure_task], return_when=asyncio.FIRST_COMPLETED, timeout=15.0)

        return captured_frame_bytes

    except Exception as e:
        _LOGGER.error("Error during WebRTC frame capture process: %s", e)
        return None
    finally:
        if sio.connected:
            hangup_ack_received = asyncio.Event()
            def hangup_callback(*args):
                hangup_ack_received.set()
            
            await sio.emit("hang_up", "{}", callback=hangup_callback)
            try:
                await asyncio.wait_for(hangup_ack_received.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                _LOGGER.warning("Timed out waiting for hang_up ACK.")
            await sio.disconnect()

        if pc.connectionState != 'closed':
            await pc.close()


async def _setup_webrtc_handshake_video_only(sio_client, peer_connection, server_info):
    """Perform the complex, video-only WebRTC handshake."""
    peer_connection.configuration = RTCConfiguration(iceServers=server_info["iceServers"])
    
    video_transport_info = server_info["recvTransportVideo"]
    video_producer_id = server_info["producerIdVideo"]

    video_consume_future = asyncio.Future()
    await sio_client.emit(
        "transport_consume", 
        {"transportId": video_transport_info["id"], "producerId": video_producer_id, "rtpCapabilities": RTP_CAPABILITIES}, 
        callback=lambda data: video_consume_future.set_result(data)
    )
    video_consumer_params = await video_consume_future

    sdp_str = _build_sdp_video_only(video_transport_info, video_consumer_params)
    
    remote_description = RTCSessionDescription(sdp=sdp_str, type="offer")
    await peer_connection.setRemoteDescription(remote_description)
    answer = await peer_connection.createAnswer()
    await peer_connection.setLocalDescription(answer)
    
    dtls_transport = peer_connection.getTransceivers()[0].receiver.transport
    local_dtls_params = { "role": "client", "fingerprints": [{"algorithm": fp.algorithm, "value": fp.value} for fp in dtls_transport.getLocalParameters().fingerprints] }

    await sio_client.emit("transport_connect", {"transportId": video_transport_info["id"], "dtlsParameters": local_dtls_params})
    
    await sio_client.emit("consumer_resume", {"consumerId": video_consumer_params["result"]["id"]})


def _build_sdp_video_only(video_transport_info, video_consumer_params):
    """Constructs the SDP string for a video-only WebRTC offer."""
    ice = video_transport_info["iceParameters"]
    dtls = video_transport_info["dtlsParameters"]
    
    sdp = "v=0\r\n"
    sdp += "o=- 5133548076695286524 2 IN IP4 127.0.0.1\r\n"
    sdp += "s=-\r\n"
    sdp += "t=0 0\r\n"
    sdp += "a=msid-semantic: WMS\r\n"
    sdp += f"a=ice-ufrag:{ice['usernameFragment']}\r\n"
    sdp += f"a=ice-pwd:{ice['password']}\r\n"
    for fp in dtls['fingerprints']:
        sdp += f"a=fingerprint:{fp['algorithm']} {fp['value'].upper()}\r\n"
    sdp += "a=setup:actpass\r\n"

    # Video Media Section
    vid_rtp = video_consumer_params["result"]["rtpParameters"]
    video_payload_type = vid_rtp['codecs'][0]['payloadType']
    sdp += f"m=video 9 UDP/TLS/RTP/SAVPF {video_payload_type}\r\n"
    sdp += "c=IN IP4 0.0.0.0\r\n"
    sdp += "a=rtcp-mux\r\n"
    for candidate in video_transport_info.get("iceCandidates", []):
        sdp += f"a=candidate:{candidate['foundation']} 1 {candidate['protocol']} {candidate['priority']} {candidate['ip']} {candidate['port']} typ {candidate['type']}\r\n"
    sdp += "a=end-of-candidates\r\n"
    sdp += "a=mid:video\r\n"
    sdp += "a=sendrecv\r\n"
    for ext in vid_rtp["headerExtensions"]:
        sdp += f"a=extmap:{ext['id']} {ext['uri']}\r\n"
    sdp += f"a=rtpmap:{video_payload_type} H264/90000\r\n"
    sdp += f"a=fmtp:{video_payload_type} level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f\r\n"
    sdp += f"a=ssrc:{vid_rtp['encodings'][0]['ssrc']} cname:{vid_rtp['rtcp']['cname']}\r\n"
    
    return sdp
