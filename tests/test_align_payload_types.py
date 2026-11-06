"""Tests for aligning Nest's H264 payload types with the offer."""

# pylint: disable=missing-function-docstring
from aiortc.sdp import SessionDescription

from nest_rtc.stream import align_video_payload_types

# Video sections reduced from a real Nest exchange (2026-11-06). Candidates and ICE/DTLS
# attributes are omitted because the function only touches the video section's codecs.
OFFER = "\r\n".join(
    [
        "v=0",
        "o=- 1 1 IN IP4 0.0.0.0",
        "s=-",
        "t=0 0",
        "m=audio 9 UDP/TLS/RTP/SAVPF 96",
        "a=rtpmap:96 opus/48000/2",
        "m=video 9 UDP/TLS/RTP/SAVPF 99 100 101 102",
        "a=rtpmap:99 H264/90000",
        "a=fmtp:99 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f",
        "a=rtpmap:100 rtx/90000",
        "a=fmtp:100 apt=99",
        "a=rtpmap:101 H264/90000",
        "a=fmtp:101 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
        "a=rtpmap:102 rtx/90000",
        "a=fmtp:102 apt=101",
        "",
    ]
)

ANSWER_VIDEO = [
    "m=video 9 UDP/TLS/RTP/SAVPF 99 100 99 100",
    "c=IN IP4 0.0.0.0",
    "a=mid:1",
    "a=sendonly",
    "a=rtcp-mux",
    "a=rtpmap:99 H264/90000",
    "a=rtcp-fb:99 nack pli",
    "a=fmtp:99 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
    "a=rtpmap:100 rtx/90000",
    "a=fmtp:100 apt=99",
    "a=rtpmap:99 H264/90000",
    "a=rtcp-fb:99 nack pli",
    "a=fmtp:99 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
    "a=rtpmap:100 rtx/90000",
    "a=fmtp:100 apt=99",
    "a=ssrc-group:FID 7777 7778",
    "a=ssrc:7777 cname:7777",
    "a=ssrc:7778 cname:7777",
]
ANSWER = "\r\n".join(
    [
        "v=0",
        "o=- 0 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "m=audio 9 UDP/TLS/RTP/SAVPF 96",
        "a=mid:0",
        "a=rtpmap:96 opus/48000/2",
        *ANSWER_VIDEO,
        "m=application 9 DTLS/SCTP 5000",
        "a=mid:2",
        "",
    ]
)


def test_nest_answer_relabels_h264_to_offer_payload_type() -> None:
    fixed = align_video_payload_types(OFFER, ANSWER)
    lines = fixed.splitlines()
    assert "m=video 9 UDP/TLS/RTP/SAVPF 101 100 101 100 102" in lines
    assert "a=rtpmap:101 H264/90000" in lines
    assert "a=fmtp:100 apt=101" in lines
    assert "a=rtcp-fb:101 nack pli" in lines
    assert not any(line.startswith(("a=rtpmap:99", "a=fmtp:99")) for line in lines)


def test_relabeled_answer_parses_with_h264_on_101_and_rtx_on_100() -> None:
    video = SessionDescription.parse(align_video_payload_types(OFFER, ANSWER)).media[1]
    codecs = {}
    for codec in video.rtp.codecs:
        # aiortc applies fmtp to the first codec with a payload type; keep that one.
        codecs.setdefault(codec.payloadType, codec)
    assert codecs[101].mimeType == "video/H264"
    assert codecs[101].parameters["profile-level-id"] == "42e01f"
    assert codecs[100].parameters["apt"] == 101


def test_audio_and_application_sections_unchanged() -> None:
    fixed = align_video_payload_types(OFFER, ANSWER).splitlines()
    assert "a=rtpmap:96 opus/48000/2" in fixed
    assert "m=application 9 DTLS/SCTP 5000" in fixed


def test_consistent_answer_is_unchanged() -> None:
    consistent = ANSWER.replace("profile-level-id=42e01f", "profile-level-id=42001f")
    assert align_video_payload_types(OFFER, consistent) == consistent


def test_answer_without_video_is_unchanged() -> None:
    no_video = "v=0\r\ns=-\r\nm=audio 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 opus/48000/2\r\n"
    assert align_video_payload_types(OFFER, no_video) == no_video


def test_offer_rtx_for_relabeled_codec_is_added() -> None:
    # Nest sent retransmissions as 102 (the offer's RTX for 101) after relabeling.
    fixed = align_video_payload_types(OFFER, ANSWER).splitlines()
    assert "m=video 9 UDP/TLS/RTP/SAVPF 101 100 101 100 102" in fixed
    assert "a=rtpmap:102 rtx/90000" in fixed
    assert "a=fmtp:102 apt=101" in fixed
    video = SessionDescription.parse("\r\n".join(fixed) + "\r\n").media[1]
    rtx: dict[int, object] = {}
    for codec in video.rtp.codecs:
        if codec.mimeType == "video/rtx":
            rtx.setdefault(codec.payloadType, codec.parameters.get("apt"))
    assert rtx[102] == 101
    assert rtx[100] == 101
