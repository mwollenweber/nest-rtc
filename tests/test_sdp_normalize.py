"""Tests for repairing Nest SDP answers before aiortc parses them."""

# pylint: disable=missing-function-docstring
from aiortc.sdp import SessionDescription

from nest_rtc.sdm import normalize_answer_sdp

BASE = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
    "a=rtpmap:96 H264/90000\r\n"
)


def _candidates(sdp: str) -> list[list[str]]:
    return [
        line.removeprefix("a=candidate:").split()
        for line in sdp.splitlines()
        if line.startswith("a=candidate:")
    ]


def test_space_after_colon_is_repaired_and_parses() -> None:
    sdp = BASE + "a=candidate: 1 udp 2113939711 192.0.2.10 19305 typ host generation 0\r\n"
    fixed = normalize_answer_sdp(sdp)
    (bits,) = _candidates(fixed)
    assert bits[1:4] == ["1", "udp", "2113939711"]
    parsed = SessionDescription.parse(fixed)
    assert parsed.media[0].ice_candidates[0].port == 19305


def test_missing_foundation_is_repaired_and_parses() -> None:
    sdp = BASE + "a=candidate:1 udp 2113939711 192.0.2.10 19305 typ host\r\n"
    fixed = normalize_answer_sdp(sdp)
    (bits,) = _candidates(fixed)
    assert bits[1:3] == ["1", "udp"]
    assert SessionDescription.parse(fixed).media[0].ice_candidates[0].ip == "192.0.2.10"


def test_tcp_candidate_is_repaired() -> None:
    sdp = BASE + "a=candidate: 1 TCP 2113939711 192.0.2.10 443 typ host tcptype passive\r\n"
    (bits,) = _candidates(normalize_answer_sdp(sdp))
    assert bits[1:3] == ["1", "TCP"]


def test_well_formed_candidate_is_unchanged() -> None:
    line = "a=candidate:abc 1 udp 2113939711 192.0.2.10 19305 typ host"
    assert line in normalize_answer_sdp(BASE + line + "\r\n").splitlines()


def test_foundations_are_unique() -> None:
    sdp = BASE + (
        "a=candidate: 1 udp 2113939711 192.0.2.10 19305 typ host\r\n"
        "a=candidate: 1 udp 2113939711 192.0.2.11 19305 typ host\r\n"
    )
    foundations = [bits[0] for bits in _candidates(normalize_answer_sdp(sdp))]
    assert len(set(foundations)) == len(foundations)


def test_sdp_without_candidates_is_unchanged() -> None:
    assert normalize_answer_sdp(BASE) == BASE
