"""Stream tests.

The loopback tests stand in for Google with a second aiortc peer that answers the offer
and sends synthetic video, so negotiation, keepalive and the sinks run end to end
without a real camera.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument
import asyncio
import io
from datetime import UTC, datetime, timedelta

import pytest
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame

from nest_rtc import stream as stream_mod
from nest_rtc.sdm import WebRtcSession

DEVICE = "enterprises/p/devices/cam"


def test_offer_meets_nest_constraints() -> None:
    async def run() -> str:
        pc = stream_mod.new_peer_connection()
        try:
            return await stream_mod.build_offer(pc)
        finally:
            await pc.close()

    sdp = asyncio.run(run())
    assert [line.split(" ")[0] for line in sdp.splitlines() if line.startswith("m=")] == [
        "m=audio",
        "m=video",
        "m=application",
    ]
    video = sdp.split("m=video", 1)[1].split("m=application", 1)[0]
    assert "H264/90000" in video
    assert "VP8" not in video


@pytest.mark.parametrize(
    ("sdp", "message"),
    [
        ("v=0\r\nm=video 9 X 96\r\nm=audio 9 X 111\r\nm=application 9 X x\r\n", "m-lines"),
        (
            "v=0\r\nm=audio 9 X 111\r\na=recvonly\r\na=rtpmap:111 opus/48000/2\r\n"
            "m=video 9 X 96\r\nm=application 9 X x",
            "newline",
        ),
        (
            "v=0\r\nm=audio 9 X 111\r\na=sendrecv\r\na=rtpmap:111 opus/48000/2\r\n"
            "m=video 9 X 96\r\nm=application 9 X x\r\n",
            "recvonly",
        ),
        (
            "v=0\r\nm=audio 9 X 111 0\r\na=recvonly\r\na=rtpmap:111 opus/48000/2\r\n"
            "a=rtpmap:0 PCMU/8000\r\nm=video 9 X 96\r\nm=application 9 X x\r\n",
            "Opus only",
        ),
    ],
)
def test_validate_offer_rejects(sdp: str, message: str) -> None:
    with pytest.raises(stream_mod.OfferError, match=message):
        stream_mod.validate_offer(sdp)


def test_frame_bytes_strips_row_padding() -> None:
    frame = VideoFrame(width=33, height=7, format="yuv420p")
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    data = stream_mod.frame_bytes(frame, 33, 7, "bgr24")
    assert len(data) == 33 * 7 * 3


def test_frame_bytes_rejects_planar_format() -> None:
    frame = VideoFrame(width=16, height=16, format="yuv420p")
    with pytest.raises(ValueError, match="unsupported"):
        stream_mod.frame_bytes(frame, 16, 16, "yuv420p")


class LoopbackSdm:
    """Fake SdmClient whose 'camera' is a local aiortc peer sending test video."""

    def __init__(self, loop: asyncio.AbstractEventLoop, lifetime: float, honour_extend: bool):
        self.loop = loop
        self.lifetime = lifetime
        self.honour_extend = honour_extend
        self.peer: RTCPeerConnection | None = None
        self.calls: list[str] = []

    async def _answer(self, offer: str) -> str:
        peer = self.peer = RTCPeerConnection()
        await peer.setRemoteDescription(RTCSessionDescription(sdp=offer, type="offer"))
        peer.addTrack(AudioStreamTrack())
        peer.addTrack(VideoStreamTrack())
        await peer.setLocalDescription(await peer.createAnswer())
        return peer.localDescription.sdp

    def _expiry(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.lifetime)

    def generate_webrtc(self, device: str, offer_sdp: str) -> WebRtcSession:
        self.calls.append("generate")
        stream_mod.validate_offer(offer_sdp)
        answer = asyncio.run_coroutine_threadsafe(self._answer(offer_sdp), self.loop).result(10)
        return WebRtcSession(answer, "m1", self._expiry())

    def extend_webrtc(self, device: str, session: WebRtcSession) -> WebRtcSession:
        self.calls.append("extend")
        expiry = self._expiry() if self.honour_extend else session.expires_at
        return WebRtcSession(session.answer_sdp, session.media_session_id, expiry)

    def stop_webrtc(self, device: str, session: WebRtcSession) -> None:
        self.calls.append("stop")

    async def close(self) -> None:
        if self.peer is not None:
            await self.peer.close()


def test_loopback_raw_frames() -> None:
    async def run() -> tuple[int, int, int, bytes, list[str]]:
        fake = LoopbackSdm(asyncio.get_running_loop(), lifetime=300, honour_extend=True)
        out = io.BytesIO()
        try:
            async with stream_mod.NestLiveStream(fake, DEVICE) as live:
                frames, width, height = await stream_mod.write_raw(live, out, duration=2.0)
        finally:
            await fake.close()
        return frames, width, height, out.getvalue(), fake.calls

    frames, width, height, data, calls = asyncio.run(run())
    assert frames > 0
    assert (width, height) == (640, 480)
    assert len(data) == frames * width * height * 3
    assert calls == ["generate", "stop"]


def test_loopback_keepalive_extends(monkeypatch) -> None:
    monkeypatch.setattr(stream_mod, "EXTEND_LEAD_SECONDS", 1.5)

    async def run() -> list[str]:
        fake = LoopbackSdm(asyncio.get_running_loop(), lifetime=2.0, honour_extend=True)
        try:
            async with stream_mod.NestLiveStream(fake, DEVICE) as live:
                await asyncio.sleep(2.0)
                assert not live.ended.is_set()
        finally:
            await fake.close()
        return fake.calls

    calls = asyncio.run(run())
    assert calls.count("extend") >= 2
    assert calls[-1] == "stop"


def test_loopback_ignored_extension_ends_stream(monkeypatch) -> None:
    monkeypatch.setattr(stream_mod, "EXTEND_LEAD_SECONDS", 1.0)

    async def run() -> tuple[bool, list[str]]:
        fake = LoopbackSdm(asyncio.get_running_loop(), lifetime=1.5, honour_extend=False)
        try:
            async with stream_mod.NestLiveStream(fake, DEVICE) as live:
                await asyncio.wait_for(live.ended.wait(), 5)
                ended = live.ended.is_set()
        finally:
            await fake.close()
        return ended, fake.calls

    ended, calls = asyncio.run(run())
    assert ended
    assert calls == ["generate", "extend", "stop"]


def test_loopback_record_mp4(tmp_path) -> None:
    path = tmp_path / "clip.mp4"

    async def run() -> None:
        fake = LoopbackSdm(asyncio.get_running_loop(), lifetime=300, honour_extend=True)
        try:
            async with stream_mod.NestLiveStream(fake, DEVICE) as live:
                await stream_mod.record(live, path, duration=2.0)
        finally:
            await fake.close()

    asyncio.run(run())
    assert path.stat().st_size > 1000
