"""Tests for device parsing, command execution and rate limiting."""

# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument
from datetime import UTC, datetime

import pytest

from nest_rtc import sdm
from nest_rtc.http import HttpError

CAMERA = {
    "name": "enterprises/p/devices/AVPHw-cam1",
    "type": "sdm.devices.types.CAMERA",
    "traits": {
        "sdm.devices.traits.Info": {"customName": ""},
        "sdm.devices.traits.CameraLiveStream": {
            "maxVideoResolution": {"width": 640, "height": 480},
            "videoCodecs": ["H264"],
            "audioCodecs": ["OPUS"],
            "supportedProtocols": ["WEB_RTC"],
        },
    },
    "parentRelations": [
        {"parent": "enterprises/p/structures/s/rooms/r", "displayName": "Backyard"}
    ],
}
THERMOSTAT = {
    "name": "enterprises/p/devices/therm",
    "type": "sdm.devices.types.THERMOSTAT",
    "traits": {"sdm.devices.traits.Info": {"customName": "Hall"}},
}


class FakeTokens:
    def access_token(self) -> str:
        return "tok"


class NoLimit(sdm.RateLimiter):
    def acquire(self, device: str, command: str) -> None:
        return None


def test_parse_device_falls_back_to_room_name() -> None:
    dev = sdm.parse_device(CAMERA)
    assert dev.display_name == "Backyard"
    assert dev.device_id == "AVPHw-cam1"
    assert dev.supports_webrtc
    assert dev.max_resolution == (640, 480)


def test_parse_device_without_stream_trait() -> None:
    dev = sdm.parse_device(THERMOSTAT)
    assert dev.display_name == "Hall"
    assert not dev.supports_webrtc
    assert dev.max_resolution is None


@pytest.mark.parametrize(
    "value",
    ["2026-11-05T23:40:00Z", "2026-11-05T23:40:00.000Z", "2026-11-05T23:40:00.123456789Z"],
)
def test_parse_timestamp(value: str) -> None:
    parsed = sdm.parse_timestamp(value)
    assert parsed.tzinfo is not None
    assert parsed.replace(microsecond=0) == datetime(2026, 11, 5, 23, 40, tzinfo=UTC)


def test_generate_webrtc_sends_command() -> None:
    sent = []

    def fake(method, url, **kwargs):
        sent.append((url, kwargs["json_body"], kwargs["headers"]))
        return {
            "results": {
                "answerSdp": "v=0\r\n",
                "mediaSessionId": "m1",
                "expiresAt": "2026-11-05T23:45:00.000Z",
            }
        }

    client = sdm.SdmClient("p", FakeTokens(), requester=fake, limiter=NoLimit())
    session = client.generate_webrtc(CAMERA["name"], "offer\r\n")
    url, body, headers = sent[0]
    assert url.endswith("/enterprises/p/devices/AVPHw-cam1:executeCommand")
    assert body == {"command": sdm.CMD_GENERATE, "params": {"offerSdp": "offer\r\n"}}
    assert headers["Authorization"] == "Bearer tok"
    assert session.media_session_id == "m1"


def test_execute_retries_on_throttle_then_succeeds() -> None:
    attempts = []
    slept = []

    def fake(method, url, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise HttpError(429, "RESOURCE_EXHAUSTED", "Rate limited.")
        return {"results": {"ok": True}}

    client = sdm.SdmClient("p", FakeTokens(), requester=fake, limiter=NoLimit(), sleep=slept.append)
    assert client.execute("d", "c", {}) == {"ok": True}
    assert slept == [15.0, 30.0]


def test_execute_maps_failed_precondition() -> None:
    def fake(method, url, **kwargs):
        raise HttpError(400, "FAILED_PRECONDITION", "The camera is not available for streaming.")

    client = sdm.SdmClient("p", FakeTokens(), requester=fake, limiter=NoLimit())
    with pytest.raises(sdm.CameraUnavailableError):
        client.execute("d", "c", {})


def test_rate_limiter_waits_for_per_device_window() -> None:
    now = [0.0]
    slept = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = sdm.RateLimiter(clock=lambda: now[0], sleep=sleep)
    for _ in range(sdm.DEVICE_COMMANDS_PER_MINUTE):
        limiter.acquire("d1", "extend")
    assert not slept
    limiter.acquire("d1", "extend")
    assert slept == [60.0]


def test_rate_limiter_caps_project_wide() -> None:
    now = [0.0]
    slept = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = sdm.RateLimiter(clock=lambda: now[0], sleep=sleep)
    for i in range(sdm.PROJECT_COMMANDS_PER_MINUTE):
        limiter.acquire(f"d{i}", "gen")
    limiter.acquire("d-extra", "gen")
    assert slept == [60.0]
