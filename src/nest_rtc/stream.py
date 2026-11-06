"""WebRTC negotiation with a Nest camera and sinks for the received video.

The SDP offer must satisfy Nest's constraints: Unified Plan, m-lines in the order
audio, video, application; Opus as the only audio codec; audio recvonly; and a data
channel alongside the media. build_offer produces such an offer with aiortc and
validate_offer checks it before it is sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpReceiver,
    RTCSessionDescription,
)
from aiortc.contrib.media import MediaBlackhole, MediaRecorder
from aiortc.rtp import RtpPacket
from av import VideoFrame

from nest_rtc.http import HttpError
from nest_rtc.sdm import CameraUnavailableError, SdmClient, WebRtcSession

log = logging.getLogger(__name__)

# Extend this long before the session's expiry.
EXTEND_LEAD_SECONDS = 60.0
TRACK_TIMEOUT_SECONDS = 20.0
# If no frame has decoded this long after recording starts, log RTP counters to show why.
NO_FRAME_WARN_SECONDS = 5.0
STUN_SERVER = "stun:stun.l.google.com:19302"

# Packed pixel formats write_raw supports, mapped to bytes per pixel.
PACKED_FORMATS = {"bgr24": 3, "rgb24": 3, "bgra": 4, "rgba": 4, "gray": 1}

AUDIO_CODECS = ("audio/opus",)
# Nest cameras advertise H264 only. rtx is kept so lost packets can be retransmitted.
VIDEO_CODECS = ("video/h264", "video/rtx")


class OfferError(ValueError):
    """Raised when an SDP offer violates Nest's constraints."""


class NoVideoError(RuntimeError):
    """Raised when a session connects but no video frame is ever decoded."""


def _video_section(lines: list[str]) -> tuple[int, int]:
    start = next((i for i, line in enumerate(lines) if line.startswith("m=video")), -1)
    if start < 0:
        return -1, -1
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("m=")), len(lines))
    return start, end


def _h264_keys(lines: list[str]) -> dict[int, tuple[str, str]]:
    """Map each H264 payload type in a video section to (profile-level-id, packetization-mode)."""
    h264: set[int] = set()
    fmtp: dict[int, dict[str, str]] = {}
    for line in lines:
        if line.startswith("a=rtpmap:"):
            pt, _, codec = line.removeprefix("a=rtpmap:").partition(" ")
            if codec.lower().startswith("h264/") and pt.isdigit():
                h264.add(int(pt))
        elif line.startswith("a=fmtp:"):
            pt, _, params = line.removeprefix("a=fmtp:").partition(" ")
            if pt.isdigit() and int(pt) not in fmtp:
                fmtp[int(pt)] = dict(
                    item.split("=", 1) for item in params.split(";") if "=" in item
                )
    return {
        pt: (
            fmtp.get(pt, {}).get("profile-level-id", "42e01f").lower(),
            fmtp.get(pt, {}).get("packetization-mode", "0"),
        )
        for pt in h264
    }


def _rename_payload_types(line: str, mapping: dict[int, int]) -> str:
    if line.startswith("m=video"):
        head = line.split()
        return " ".join(head[:3] + [str(mapping.get(int(f), f)) for f in head[3:]])
    for prefix in ("a=rtpmap:", "a=fmtp:", "a=rtcp-fb:"):
        if line.startswith(prefix):
            pt, sep, rest = line.removeprefix(prefix).partition(" ")
            if pt.isdigit():
                pt = str(mapping.get(int(pt), int(pt)))
            if prefix == "a=fmtp:" and rest.startswith("apt="):
                apt = rest.removeprefix("apt=")
                if apt.isdigit():
                    rest = f"apt={mapping.get(int(apt), int(apt))}"
            return f"{prefix}{pt}{sep}{rest}"
    return line


def _h264_mapping(
    offer_keys: dict[int, tuple[str, str]], answer_keys: dict[int, tuple[str, str]]
) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for pt, key in answer_keys.items():
        if offer_keys.get(pt) == key:
            continue
        matches = sorted(q for q, k in offer_keys.items() if k == key)
        if matches and matches[0] not in answer_keys:
            mapping[pt] = matches[0]
    return mapping


def align_video_payload_types(offer_sdp: str, answer_sdp: str) -> str:
    """Relabel H264 payload types in a Nest answer to the ones Google actually sends.

    Nest answers name the H264 codec with a payload type from the offer but attach the
    parameters of a different offered H264 codec, then send media with the payload type
    of the codec whose parameters it attached. Observed: the offer has 99 = profile
    42001f and 101 = 42e01f, the answer says 99 with 42e01f, and RTP arrives as 101.
    aiortc trusts the answer and discards every packet. Each answer H264 payload type is
    renamed to the offer payload type with the same profile-level-id and
    packetization-mode, and RTX apt references follow. RTX payload types are unchanged
    because Nest sends RTX with the answer's RTX payload type.

    Args:
        offer_sdp: The offer sent to GenerateWebRtcStream.
        answer_sdp: The answer it returned.

    Returns:
        The answer with H264 payload types aligned to the offer, or unchanged when they
        already agree.
    """
    offer_lines = offer_sdp.splitlines()
    answer_lines = answer_sdp.splitlines()
    o_start, o_end = _video_section(offer_lines)
    a_start, a_end = _video_section(answer_lines)
    if o_start < 0 or a_start < 0:
        return answer_sdp
    mapping = _h264_mapping(
        _h264_keys(offer_lines[o_start:o_end]), _h264_keys(answer_lines[a_start:a_end])
    )
    if not mapping:
        return answer_sdp
    for old, new in sorted(mapping.items()):
        log.info("relabeled answer H264 payload type %d as %d to match Nest media", old, new)
    for i in range(a_start, a_end):
        answer_lines[i] = _rename_payload_types(answer_lines[i], mapping)
    extra = _missing_rtx_lines(
        offer_lines[o_start:o_end], answer_lines[a_start:a_end], set(mapping.values())
    )
    if extra:
        fmt_pts = [line.split()[0].removeprefix("a=rtpmap:") for line in extra[::2]]
        log.info("added offer RTX payload type(s) %s to the answer", ", ".join(fmt_pts))
        answer_lines[a_start] = " ".join([answer_lines[a_start], *fmt_pts])
        answer_lines[a_end:a_end] = extra
    return "\r\n".join(answer_lines) + "\r\n"


def _rtx_apts(lines: list[str]) -> dict[int, int]:
    """Map each RTX payload type in a video section to its apt."""
    rtx: set[int] = set()
    apts: dict[int, int] = {}
    for line in lines:
        if line.startswith("a=rtpmap:"):
            pt, _, codec = line.removeprefix("a=rtpmap:").partition(" ")
            if codec.lower().startswith("rtx/") and pt.isdigit():
                rtx.add(int(pt))
        elif line.startswith("a=fmtp:"):
            pt, _, params = line.removeprefix("a=fmtp:").partition(" ")
            apt = params.removeprefix("apt=")
            if pt.isdigit() and params.startswith("apt=") and apt.isdigit():
                apts.setdefault(int(pt), int(apt))
    return {pt: apts[pt] for pt in rtx if pt in apts}


def _missing_rtx_lines(
    offer_video: list[str], answer_video: list[str], relabeled: set[int]
) -> list[str]:
    """Return rtpmap/fmtp lines for offer RTX payload types the answer leaves out.

    After relabeling, Nest sends retransmissions with the offer's RTX payload type for
    the relabeled codec (102 for 101) while the answer only declares its own (100).
    Declaring both keeps aiortc from dropping either.
    """
    present = {
        int(pt)
        for line in answer_video
        if line.startswith("a=rtpmap:")
        and (pt := line.removeprefix("a=rtpmap:").partition(" ")[0]).isdigit()
    }
    extra: list[str] = []
    for pt, apt in sorted(_rtx_apts(offer_video).items()):
        if apt in relabeled and pt not in present:
            extra += [f"a=rtpmap:{pt} rtx/90000", f"a=fmtp:{pt} apt={apt}"]
    return extra


def log_discarded_rtp(pc: RTCPeerConnection) -> None:
    """Log RTP packets aiortc's router drops, once per (SSRC, payload type).

    aiortc silently discards a packet whose payload type was not negotiated for the
    receiver that owns its SSRC. This hook makes that visible. It wraps a private aiortc
    attribute, so it degrades to a debug message if aiortc's internals change.

    Args:
        pc: Peer connection after setRemoteDescription.
    """
    transceivers = pc.getTransceivers()
    router = (
        getattr(transceivers[0].receiver.transport, "_rtp_router", None) if transceivers else None
    )
    if router is None or not hasattr(router, "route_rtp"):
        log.debug("cannot hook aiortc RTP router; dropped-packet logging disabled")
        return
    original = router.route_rtp
    seen: set[tuple[int, int]] = set()

    def route_rtp(packet: RtpPacket) -> object | None:
        receiver = original(packet)
        key = (packet.ssrc, packet.payload_type)
        if receiver is None and key not in seen:
            seen.add(key)
            known = sorted(router.payload_type_table)
            log.warning(
                "dropped RTP: ssrc=%d payload type=%d (negotiated payload types %s)",
                packet.ssrc,
                packet.payload_type,
                known,
            )
        return receiver

    router.route_rtp = route_rtp


class CountingTrack(MediaStreamTrack):
    """Pass-through video track that counts frames and records the first frame's size.

    Attributes:
        frames: Frames received so far.
        width: Width of the first frame, or 0 before any frame.
        height: Height of the first frame, or 0 before any frame.
    """

    kind = "video"

    def __init__(self, source: MediaStreamTrack) -> None:
        """Wrap a track.

        Args:
            source: The remote video track.
        """
        super().__init__()
        self._source = source
        self.frames = 0
        self.width = 0
        self.height = 0

    async def recv(self) -> VideoFrame:
        """Return the next frame from the source track.

        Returns:
            The decoded frame.
        """
        frame = await self._source.recv()
        if self.frames == 0 and isinstance(frame, VideoFrame):
            self.width, self.height = frame.width, frame.height
            log.info("first video frame: %dx%d", self.width, self.height)
        self.frames += 1
        return frame  # type: ignore[return-value]


def new_peer_connection() -> RTCPeerConnection:
    """Create a receive-only peer connection shaped for Nest cameras.

    Returns:
        A peer connection with an Opus-only recvonly audio transceiver, an H264 recvonly
        video transceiver, and a data channel, created in that order so the offer's
        m-lines come out audio, video, application.
    """
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(urls=STUN_SERVER)]))
    audio = pc.addTransceiver("audio", direction="recvonly")
    audio_caps = RTCRtpReceiver.getCapabilities("audio")
    audio.setCodecPreferences([c for c in audio_caps.codecs if c.mimeType.lower() in AUDIO_CODECS])
    video = pc.addTransceiver("video", direction="recvonly")
    video_caps = RTCRtpReceiver.getCapabilities("video")
    video.setCodecPreferences([c for c in video_caps.codecs if c.mimeType.lower() in VIDEO_CODECS])
    pc.createDataChannel("dataSendChannel")
    return pc


def validate_offer(sdp: str) -> None:
    """Check an SDP offer against Nest's documented constraints.

    Args:
        sdp: The SDP offer.

    Raises:
        OfferError: Describing the first violated constraint.
    """
    lines = sdp.splitlines()
    kinds = [line[2:].split(" ", 1)[0] for line in lines if line.startswith("m=")]
    if kinds != ["audio", "video", "application"]:
        raise OfferError(f"m-lines must be audio, video, application; got {kinds}")
    if not sdp.endswith("\n"):
        raise OfferError("offer must end with a newline")
    sections: list[list[str]] = []
    for line in lines:
        if line.startswith("m="):
            sections.append([])
        if sections:
            sections[-1].append(line)
    audio = sections[0]
    if "a=recvonly" not in audio:
        raise OfferError("audio must be a=recvonly")
    audio_codecs = {
        line.split(" ", 1)[1].split("/")[0].lower()
        for line in audio
        if line.startswith("a=rtpmap:")
    }
    if audio_codecs != {"opus"}:
        raise OfferError(f"audio codecs must be Opus only; got {sorted(audio_codecs)}")


async def build_offer(pc: RTCPeerConnection) -> str:
    """Create, apply and validate a local offer.

    aiortc gathers ICE candidates during setLocalDescription, so the returned SDP is
    complete and needs no trickle ICE.

    Args:
        pc: A peer connection from new_peer_connection.

    Returns:
        The validated offer SDP.
    """
    await pc.setLocalDescription(await pc.createOffer())
    sdp = pc.localDescription.sdp
    validate_offer(sdp)
    return sdp


class NestLiveStream:
    """Async context manager that holds a live WebRTC session open.

    On entry it negotiates the session and waits for the video track. While open, a
    background task extends the session before each five-minute expiry. `ended` is set
    when the session can no longer continue: an ignored extension (battery power), an
    API error, or a failed connection. On exit the session is stopped and the peer
    connection closed.

    Attributes:
        video: The remote video track, available after entry.
        ended: Set when the stream has stopped or will stop at its current expiry.
    """

    def __init__(
        self,
        client: SdmClient,
        device: str,
        *,
        keepalive: bool = True,
        track_timeout: float = TRACK_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the stream.

        Args:
            client: SDM client used to generate, extend and stop the session.
            device: Device resource name.
            keepalive: Whether to extend the session before it expires.
            track_timeout: Seconds to wait for the video track after negotiation.
        """
        self._client = client
        self._device = device
        self._keepalive = keepalive
        self._track_timeout = track_timeout
        self._pc: RTCPeerConnection | None = None
        self._session: WebRtcSession | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._blackhole = MediaBlackhole()
        self.video: MediaStreamTrack | None = None
        self.ended = asyncio.Event()

    @property
    def session(self) -> WebRtcSession | None:
        """Return the current session, or None before negotiation."""
        return self._session

    async def rtp_stats(self) -> dict[str, tuple[int, int]]:
        """Return inbound RTP counters per media kind.

        Returns:
            Mapping of "audio"/"video" to (packets received, packets lost). Empty when
            the peer connection is closed.
        """
        if self._pc is None:
            return {}
        report = await self._pc.getStats()
        stats: dict[str, tuple[int, int]] = {}
        for entry in report.values():
            if getattr(entry, "type", "") == "inbound-rtp":
                stats[entry.kind] = (int(entry.packetsReceived), int(entry.packetsLost))
        return stats

    async def __aenter__(self) -> Self:
        """Negotiate the session and wait for video.

        Returns:
            This stream.

        Raises:
            TimeoutError: If no video track arrives within track_timeout.
        """
        pc = self._pc = new_peer_connection()
        got_video = asyncio.Event()

        @pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            log.info("received %s track", track.kind)
            if track.kind == "video":
                self.video = track
                got_video.set()
            else:
                # Audio is discarded by default. Unread tracks buffer frames without bound.
                self._blackhole.addTrack(track)

        @pc.on("connectionstatechange")
        async def on_state() -> None:
            log.info("connection state: %s", pc.connectionState)
            if pc.connectionState in {"failed", "closed"}:
                self.ended.set()

        offer = await build_offer(pc)
        self._session = await asyncio.to_thread(self._client.generate_webrtc, self._device, offer)
        answer = align_video_payload_types(offer, self._session.answer_sdp)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        log_discarded_rtp(pc)
        await self._blackhole.start()
        try:
            await asyncio.wait_for(got_video.wait(), self._track_timeout)
        except TimeoutError:
            await self.__aexit__(None, None, None)
            raise
        log.info("live until %s", self._session.expires_at.isoformat())
        if self._keepalive:
            self._tasks.append(asyncio.create_task(self._extend_loop()))
        else:
            self._tasks.append(asyncio.create_task(self._expire_at(self._session.expires_at)))
        return self

    async def _expire_at(self, when: datetime) -> None:
        await asyncio.sleep(max((when - datetime.now(UTC)).total_seconds(), 0))
        log.info("session expired")
        self.ended.set()

    async def _extend_loop(self) -> None:
        while self._session is not None and not self.ended.is_set():
            current = self._session
            wait = (current.expires_at - datetime.now(UTC)).total_seconds() - EXTEND_LEAD_SECONDS
            await asyncio.sleep(max(wait, 0))
            try:
                renewed = await asyncio.to_thread(self._client.extend_webrtc, self._device, current)
            except (CameraUnavailableError, HttpError) as exc:
                log.error("extension failed: %s", exc)
                await self._expire_at(current.expires_at)
                return
            if renewed.expires_at <= current.expires_at:
                log.warning("extension ignored; the camera is likely on battery power")
                await self._expire_at(current.expires_at)
                return
            self._session = renewed
            log.info("extended until %s", renewed.expires_at.isoformat())

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the session and release resources.

        Args:
            exc_type: Exception type, if the block raised.
            exc: Exception, if the block raised.
            tb: Traceback, if the block raised.
        """
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self._session is not None:
            try:
                await asyncio.to_thread(self._client.stop_webrtc, self._device, self._session)
            except (CameraUnavailableError, HttpError) as err:
                # The session expires on its own; failing to stop it early is not fatal.
                log.warning("stop failed: %s", err)
            self._session = None
        await self._blackhole.stop()
        if self._pc is not None:
            await self._pc.close()
            self._pc = None
        self.ended.set()


async def _wait_until_done(stream: NestLiveStream, duration: float | None) -> None:
    if duration is None:
        await stream.ended.wait()
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stream.ended.wait(), duration)


async def record(stream: NestLiveStream, path: Path, duration: float | None) -> None:
    """Record the video track to a media file.

    The container comes from the file extension (.mp4, .mkv, ...). aiortc decodes the
    WebRTC stream and re-encodes it, so the file is H264 regardless of what the camera
    sent.

    Args:
        stream: An entered NestLiveStream.
        path: Output file.
        duration: Seconds to record, or None to record until the stream ends.

    Returns:
        Number of video frames written.

    Raises:
        RuntimeError: If the stream has no video track.
        NoVideoError: If no frame decoded during the recording. PyAV creates the file
            only when the first packet is muxed, so no file exists in that case.
    """
    if stream.video is None:
        raise RuntimeError("stream has no video track")
    counter = CountingTrack(stream.video)
    recorder = MediaRecorder(str(path))
    recorder.addTrack(counter)
    await recorder.start()
    watchdog = asyncio.create_task(_warn_if_no_frames(stream, counter))
    try:
        await _wait_until_done(stream, duration)
        stats = await stream.rtp_stats()
    finally:
        watchdog.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog
        await recorder.stop()
    if counter.frames == 0:
        raise NoVideoError(f"no video frames decoded; {_describe_stats(stats)}")
    log.info("wrote %s: %d frames at %dx%d", path, counter.frames, counter.width, counter.height)
    return counter.frames


def _describe_stats(stats: dict[str, tuple[int, int]]) -> str:
    if not stats:
        return "no inbound RTP stats"
    return ", ".join(
        f"{kind} RTP {received} received / {lost} lost"
        for kind, (received, lost) in sorted(stats.items())
    )


async def _warn_if_no_frames(stream: NestLiveStream, counter: CountingTrack) -> None:
    await asyncio.sleep(NO_FRAME_WARN_SECONDS)
    if counter.frames == 0:
        stats = await stream.rtp_stats()
        log.warning(
            "no video frames after %.0fs; %s", NO_FRAME_WARN_SECONDS, _describe_stats(stats)
        )


def frame_bytes(frame: VideoFrame, width: int, height: int, pixel_format: str) -> bytes:
    """Convert a decoded frame to tightly packed raw pixels.

    Args:
        frame: Decoded video frame.
        width: Output width. Frames of another size are scaled to it.
        height: Output height.
        pixel_format: One of PACKED_FORMATS.

    Returns:
        Exactly width * height * bytes-per-pixel bytes with no row padding.

    Raises:
        ValueError: If pixel_format is not a supported packed format.
    """
    if pixel_format not in PACKED_FORMATS:
        raise ValueError(f"unsupported pixel format {pixel_format!r}")
    out = frame.reformat(width=width, height=height, format=pixel_format)
    plane = out.planes[0]
    stride = plane.line_size
    packed = width * PACKED_FORMATS[pixel_format]
    data = bytes(plane)
    if stride == packed:
        return data[: packed * height]
    # libswscale pads rows for alignment; strip the padding so readers see fixed-size rows.
    return b"".join(data[y * stride : y * stride + packed] for y in range(height))


async def write_raw(
    stream: NestLiveStream,
    out: BinaryIO,
    duration: float | None,
    pixel_format: str = "bgr24",
) -> tuple[int, int, int]:
    """Write decoded frames as headerless raw video, for piping into ffmpeg or OpenCV.

    The first frame fixes the output size. Later frames are scaled to it so a reader
    with a fixed frame size never desynchronizes.

    Args:
        stream: An entered NestLiveStream.
        out: Binary stream to write to (for example sys.stdout.buffer).
        duration: Seconds to write, or None to write until the stream ends.
        pixel_format: Packed pixel format.

    Returns:
        (frames written, width, height).

    Raises:
        RuntimeError: If the stream has no video track.
    """
    if stream.video is None:
        raise RuntimeError("stream has no video track")
    deadline = None if duration is None else time.monotonic() + duration
    frames, width, height = 0, 0, 0
    while not stream.ended.is_set():
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            break
        try:
            frame = await asyncio.wait_for(stream.video.recv(), remaining)
        except TimeoutError:
            break
        if not isinstance(frame, VideoFrame):
            continue
        if frames == 0:
            width, height = frame.width, frame.height
            log.info("raw video: %dx%d %s", width, height, pixel_format)
        out.write(frame_bytes(frame, width, height, pixel_format))
        frames += 1
    out.flush()
    return frames, width, height
