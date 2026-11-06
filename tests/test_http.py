"""Tests for request_json error mapping."""

# pylint: disable=missing-function-docstring
import urllib.error

import pytest

from nest_rtc import http


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError(TimeoutError("_ssl.c:1067: The handshake operation timed out")),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    ],
)
def test_network_failures_become_http_error(monkeypatch, exc: BaseException) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(http.urllib.request, "urlopen", fail)
    with pytest.raises(http.HttpError) as info:
        http.request_json("POST", "https://example.invalid/v1/x:executeCommand", json_body={})
    assert info.value.status == 0
    assert info.value.reason == "NETWORK_ERROR"
    assert "/v1/x:executeCommand" in info.value.message
