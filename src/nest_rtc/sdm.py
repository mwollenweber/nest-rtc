"""Smart Device Management API client: device listing and WebRTC stream commands."""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nest_rtc.auth import Requester, TokenProvider
from nest_rtc.http import HttpError, request_json

log = logging.getLogger(__name__)

SDM_BASE = "https://smartdevicemanagement.googleapis.com/v1"

CMD_GENERATE = "sdm.devices.commands.CameraLiveStream.GenerateWebRtcStream"
CMD_EXTEND = "sdm.devices.commands.CameraLiveStream.ExtendWebRtcStream"
CMD_STOP = "sdm.devices.commands.CameraLiveStream.StopWebRtcStream"

TRAIT_INFO = "sdm.devices.traits.Info"
TRAIT_LIVE = "sdm.devices.traits.CameraLiveStream"

# Sandbox limits from developers.google.com/nest/device-access/project/limits:
# executeCommand is 10 QPM per project per user, and each command is 5 QPM per device.
PROJECT_COMMANDS_PER_MINUTE = 10
DEVICE_COMMANDS_PER_MINUTE = 5
RETRY_DELAYS_SECONDS = (15.0, 30.0, 60.0)

_FRACTION = re.compile(r"\.(\d+)")
_CANDIDATE_PREFIX = "a=candidate:"
_TRANSPORTS = frozenset({"udp", "tcp"})


class CameraUnavailableError(RuntimeError):
    """Raised when Google reports the camera offline or not streamable."""


@dataclass(frozen=True, slots=True)
class Device:
    """A camera-capable device as returned by devices.list.

    Attributes:
        name: Full resource name, enterprises/<project>/devices/<id>.
        device_type: SDM type, for example sdm.devices.types.CAMERA.
        display_name: Info.customName, else the room name, else the short ID.
        protocols: Supported live-stream protocols, for example ("WEB_RTC",).
        max_resolution: (width, height) from CameraLiveStream, or None if absent.
        video_codecs: Video codecs the camera advertises.
    """

    name: str
    device_type: str
    display_name: str
    protocols: tuple[str, ...] = ()
    max_resolution: tuple[int, int] | None = None
    video_codecs: tuple[str, ...] = ()

    @property
    def device_id(self) -> str:
        """Return the last path segment of the resource name."""
        return self.name.rsplit("/", 1)[-1]

    @property
    def supports_webrtc(self) -> bool:
        """Return True if the camera can stream over WebRTC."""
        return "WEB_RTC" in self.protocols


@dataclass(slots=True)
class WebRtcSession:
    """A live WebRTC session negotiated through GenerateWebRtcStream.

    Attributes:
        answer_sdp: SDP answer to apply as the remote description.
        media_session_id: ID used to extend or stop the session.
        expires_at: Session expiry (timezone-aware UTC).
    """

    answer_sdp: str
    media_session_id: str
    expires_at: datetime


def normalize_answer_sdp(sdp: str) -> str:
    """Repair candidate lines in a Nest SDP answer so aiortc can parse them.

    aiortc expects `foundation component transport priority ip port typ type` and calls
    int() on the second field. Nest answers can carry a candidate with only one field
    before the transport, which makes that call fail on the transport name. Such lines
    get a unique foundation and component 1, which is correct because the session uses
    rtcp-mux. Well-formed lines are left unchanged.

    Args:
        sdp: SDP answer from GenerateWebRtcStream.

    Returns:
        The SDP with malformed candidate lines repaired, using CRLF line endings.
    """
    out: list[str] = []
    repaired = 0
    for index, line in enumerate(sdp.splitlines()):
        fixed = line
        if line.startswith(_CANDIDATE_PREFIX):
            fields = line.removeprefix(_CANDIDATE_PREFIX).split()
            if len(fields) >= 2 and fields[1].lower() in _TRANSPORTS:  # noqa: PLR2004
                fixed = _CANDIDATE_PREFIX + " ".join([f"nest{index}", "1", *fields[1:]])
                repaired += 1
        out.append(fixed)
    if repaired:
        log.info("repaired %d malformed ICE candidate line(s) in the answer", repaired)
    return "\r\n".join(out) + "\r\n"


def parse_timestamp(value: str) -> datetime:
    """Parse an RFC 3339 timestamp from the SDM API.

    Google may send more than six fractional digits, which datetime.fromisoformat
    rejects, so the fraction is truncated to microseconds.

    Args:
        value: Timestamp such as 2026-11-05T23:40:00.123456789Z.

    Returns:
        A timezone-aware datetime.
    """
    trimmed = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value, count=1)
    return datetime.fromisoformat(trimmed)


def parse_device(raw: dict[str, Any]) -> Device:
    """Convert one devices.list entry into a Device.

    Args:
        raw: A device object from the API.

    Returns:
        The parsed Device.
    """
    traits = raw.get("traits", {})
    name = str(raw.get("name", ""))
    display = str(traits.get(TRAIT_INFO, {}).get("customName", "")).strip()
    if not display:
        for rel in raw.get("parentRelations", []):
            if rel.get("displayName"):
                display = str(rel["displayName"])
                break
    if not display:
        display = name.rsplit("/", 1)[-1]
    live = traits.get(TRAIT_LIVE, {})
    res = live.get("maxVideoResolution")
    max_res = (int(res["width"]), int(res["height"])) if res else None
    return Device(
        name=name,
        device_type=str(raw.get("type", "")),
        display_name=display,
        protocols=tuple(live.get("supportedProtocols", ())),
        max_resolution=max_res,
        video_codecs=tuple(live.get("videoCodecs", ())),
    )


@dataclass(slots=True)
class _Window:
    limit: int
    period: float
    stamps: deque[float] = field(default_factory=deque)

    def wait_time(self, now: float) -> float:
        """Drop expired stamps and return seconds until a slot frees up (0 if one is free)."""
        while self.stamps and now - self.stamps[0] >= self.period:
            self.stamps.popleft()
        if len(self.stamps) < self.limit:
            return 0.0
        return self.period - (now - self.stamps[0])


class RateLimiter:
    """Client-side sliding-window limiter that keeps commands under Google's quotas.

    Hitting the server-side limit throttles every project touching the device until the
    minute rolls over, so it is cheaper to wait here first.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialize the limiter.

        Args:
            clock: Monotonic clock, injectable for tests.
            sleep: Sleep function, injectable for tests.
        """
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._project = _Window(PROJECT_COMMANDS_PER_MINUTE, 60.0)
        self._per_key: dict[tuple[str, str], _Window] = {}

    def acquire(self, device: str, command: str) -> None:
        """Block until a command may be sent, then record it.

        Args:
            device: Device resource name.
            command: SDM command name.
        """
        while True:
            with self._lock:
                now = self._clock()
                key_window = self._per_key.setdefault(
                    (device, command), _Window(DEVICE_COMMANDS_PER_MINUTE, 60.0)
                )
                delay = max(self._project.wait_time(now), key_window.wait_time(now))
                if delay <= 0:
                    self._project.stamps.append(now)
                    key_window.stamps.append(now)
                    return
            log.info("rate limit: waiting %.1fs before %s", delay, command.rsplit(".", 1)[-1])
            self._sleep(delay)


class SdmClient:
    """Blocking client for the SDM endpoints this tool needs."""

    def __init__(
        self,
        project_id: str,
        tokens: TokenProvider,
        requester: Requester = request_json,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialize the client.

        Args:
            project_id: Device Access project ID.
            tokens: Source of bearer tokens.
            requester: HTTP function, injectable for tests.
            limiter: Command rate limiter. A new one is created if omitted.
            sleep: Sleep function used between retries, injectable for tests.
        """
        self._project_id = project_id
        self._tokens = tokens
        self._requester = requester
        self._limiter = limiter or RateLimiter()
        self._sleep = sleep

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.access_token()}"}

    def list_devices(self) -> list[Device]:
        """List every device the user authorized for this project.

        Returns:
            Parsed devices, cameras and non-cameras alike.
        """
        url = f"{SDM_BASE}/enterprises/{urllib.parse.quote(self._project_id)}/devices"
        body = self._requester("GET", url, headers=self._headers())
        return [parse_device(d) for d in body.get("devices", [])]

    def execute(self, device: str, command: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run a device command with client-side rate limiting and retry on throttling.

        Args:
            device: Device resource name.
            command: SDM command name.
            params: Command parameters.

        Returns:
            The `results` object from the response.

        Raises:
            CameraUnavailableError: If Google reports FAILED_PRECONDITION.
            nest_rtc.http.HttpError: For other API errors, or throttling that persists
                after all retries.
        """
        url = f"{SDM_BASE}/{device}:executeCommand"
        attempts = (*RETRY_DELAYS_SECONDS, None)
        for delay in attempts:
            self._limiter.acquire(device, command)
            try:
                body = self._requester(
                    "POST",
                    url,
                    headers=self._headers(),
                    json_body={"command": command, "params": params},
                )
            except HttpError as exc:
                if exc.reason == "FAILED_PRECONDITION":
                    raise CameraUnavailableError(exc.message or "camera unavailable") from exc
                if exc.reason == "RESOURCE_EXHAUSTED" and delay is not None:
                    log.warning("throttled by Google; retrying in %.0fs", delay)
                    self._sleep(delay)
                    continue
                raise
            return dict(body.get("results", {}))
        raise AssertionError("unreachable")  # pragma: no cover

    def generate_webrtc(self, device: str, offer_sdp: str) -> WebRtcSession:
        """Start a WebRTC live stream.

        Args:
            device: Device resource name.
            offer_sdp: SDP offer meeting Nest's constraints (see nest_rtc.stream).

        Returns:
            The negotiated session. Its answer must be applied within 30 seconds.
        """
        res = self.execute(device, CMD_GENERATE, {"offerSdp": offer_sdp})
        return WebRtcSession(
            answer_sdp=normalize_answer_sdp(str(res["answerSdp"])),
            media_session_id=str(res["mediaSessionId"]),
            expires_at=parse_timestamp(str(res["expiresAt"])),
        )

    def extend_webrtc(self, device: str, session: WebRtcSession) -> WebRtcSession:
        """Extend a session by another five minutes.

        Google ignores extension for cameras on battery power. In that case the returned
        expiry does not move, which callers can detect.

        Args:
            device: Device resource name.
            session: The active session.

        Returns:
            The session with updated media_session_id and expires_at.
        """
        res = self.execute(device, CMD_EXTEND, {"mediaSessionId": session.media_session_id})
        return WebRtcSession(
            answer_sdp=session.answer_sdp,
            media_session_id=str(res.get("mediaSessionId", session.media_session_id)),
            expires_at=parse_timestamp(str(res["expiresAt"])),
        )

    def stop_webrtc(self, device: str, session: WebRtcSession) -> None:
        """Stop a session and invalidate it.

        Args:
            device: Device resource name.
            session: The active session.
        """
        self.execute(device, CMD_STOP, {"mediaSessionId": session.media_session_id})
