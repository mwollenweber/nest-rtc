"""Tests for record() frame accounting, without a network peer."""

# pylint: disable=missing-function-docstring,missing-class-docstring
import asyncio
import fractions

import pytest
from aiortc import MediaStreamTrack
from aiortc.rtp import RtpPacket
from av import VideoFrame

from nest_rtc import stream as stream_mod


class SilentTrack(MediaStreamTrack):
    kind = "video"

    async def recv(self) -> VideoFrame:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class FrameTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self) -> None:
        super().__init__()
        self._pts = 0

    async def recv(self) -> VideoFrame:
        await asyncio.sleep(1 / 30)
        frame = VideoFrame(width=320, height=240, format="yuv420p")
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, 90000)
        self._pts += 3000
        return frame


class FakeStream:
    def __init__(self, track: MediaStreamTrack, stats: dict[str, tuple[int, int]]) -> None:
        self.video = track
        self.ended = asyncio.Event()
        self._stats = stats

    async def rtp_stats(self) -> dict[str, tuple[int, int]]:
        return self._stats


def test_no_frames_raises_with_rtp_counters(tmp_path) -> None:
    path = tmp_path / "empty.mp4"
    fake = FakeStream(SilentTrack(), {"video": (0, 0), "audio": (250, 0)})

    with pytest.raises(stream_mod.NoVideoError, match=r"video RTP 0 received / 0 lost"):
        asyncio.run(stream_mod.record(fake, path, duration=0.3))  # type: ignore[arg-type]
    assert not path.exists()


def test_frames_are_counted_and_file_written(tmp_path) -> None:
    path = tmp_path / "clip.mp4"
    fake = FakeStream(FrameTrack(), {"video": (100, 0)})

    frames = asyncio.run(stream_mod.record(fake, path, duration=1.0))  # type: ignore[arg-type]
    assert frames > 0
    assert path.stat().st_size > 0


def test_warning_logged_when_no_frames(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setattr(stream_mod, "NO_FRAME_WARN_SECONDS", 0.05)
    fake = FakeStream(SilentTrack(), {"video": (0, 0)})

    with pytest.raises(stream_mod.NoVideoError):
        asyncio.run(stream_mod.record(fake, tmp_path / "x.mp4", duration=0.3))  # type: ignore[arg-type]
    assert "no video frames after" in caplog.text


def test_dropped_rtp_is_logged_once(caplog) -> None:
    async def run() -> None:
        pc = stream_mod.new_peer_connection()
        await pc.setLocalDescription(await pc.createOffer())
        stream_mod.log_discarded_rtp(pc)
        router = pc.getTransceivers()[0].receiver.transport._rtp_router  # pylint: disable=protected-access
        packet = RtpPacket(payload_type=101, ssrc=7777)
        assert router.route_rtp(packet) is None
        router.route_rtp(packet)
        await pc.close()

    asyncio.run(run())
    assert caplog.text.count("dropped RTP: ssrc=7777 payload type=101") == 1
