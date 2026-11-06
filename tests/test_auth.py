"""Tests for OAuth URL building, code exchange and token caching."""

# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument
import urllib.parse

import pytest

from nest_rtc import auth


def test_authorization_url_has_pcm_parameters() -> None:
    url = auth.build_authorization_url("proj-1", "client-1")
    parts = urllib.parse.urlsplit(url)
    assert parts.netloc == "nestservices.google.com"
    assert parts.path == "/partnerconnections/proj-1/auth"
    query = urllib.parse.parse_qs(parts.query)
    assert query["scope"] == ["https://www.googleapis.com/auth/sdm.service"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-1"]


@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        ("https://www.google.com/?code=4/abc&scope=x", "4/abc"),
        ("  4/abc \n", "4/abc"),
    ],
)
def test_extract_code(pasted: str, expected: str) -> None:
    assert auth.extract_code(pasted) == expected


def test_extract_code_rejects_url_without_code() -> None:
    with pytest.raises(ValueError, match="no code"):
        auth.extract_code("https://www.google.com/?error=access_denied")


def test_exchange_code_sends_form_body() -> None:
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3599}

    assert auth.exchange_code("cid", "sec", "code", requester=fake) == "r"
    method, url, kwargs = calls[0]
    assert method == "POST"
    assert "sec" not in url
    assert kwargs["form_body"]["grant_type"] == "authorization_code"


def test_token_provider_caches_until_expiry() -> None:
    now = [0.0]
    calls = []

    def fake(method, url, **kwargs):
        calls.append(kwargs["form_body"]["grant_type"])
        return {"access_token": f"tok{len(calls)}", "expires_in": 3600}

    provider = auth.TokenProvider("cid", "sec", "rt", requester=fake, clock=lambda: now[0])
    assert provider.access_token() == "tok1"
    now[0] = 3000.0
    assert provider.access_token() == "tok1"
    now[0] = 3541.0  # past expires_in minus the 60 s margin
    assert provider.access_token() == "tok2"
    assert calls == ["refresh_token", "refresh_token"]
    assert "rt" not in repr(provider)
