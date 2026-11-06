"""OAuth for the Smart Device Management API.

Authorization goes through Google's Partner Connections Manager (PCM), which lets the
home owner choose which devices the Device Access project may see. The resulting
refresh token is exchanged for short-lived access tokens as needed.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from nest_rtc.http import request_json

log = logging.getLogger(__name__)

PCM_AUTH_URL = "https://nestservices.google.com/partnerconnections/{project_id}/auth"
TOKEN_URL = "https://www.googleapis.com/oauth2/v4/token"  # noqa: S105 - endpoint, not a secret
SDM_SCOPE = "https://www.googleapis.com/auth/sdm.service"
DEFAULT_REDIRECT_URI = "https://www.google.com"

# Refresh this many seconds before the access token's stated expiry.
EXPIRY_MARGIN_SECONDS = 60

Requester = Callable[..., dict[str, Any]]


def build_authorization_url(
    project_id: str, client_id: str, redirect_uri: str = DEFAULT_REDIRECT_URI
) -> str:
    """Build the PCM consent URL.

    Args:
        project_id: Device Access project ID.
        client_id: OAuth 2.0 client ID.
        redirect_uri: Redirect URI registered on the OAuth client.

    Returns:
        URL to open in a browser. After consent, Google redirects to redirect_uri with
        a `code` query parameter.
    """
    query = urllib.parse.urlencode(
        {
            "redirect_uri": redirect_uri,
            "access_type": "offline",
            "prompt": "consent",
            "client_id": client_id,
            "response_type": "code",
            "scope": SDM_SCOPE,
        }
    )
    return f"{PCM_AUTH_URL.format(project_id=urllib.parse.quote(project_id))}?{query}"


def extract_code(redirect_or_code: str) -> str:
    """Pull the authorization code out of a pasted redirect URL or bare code.

    Args:
        redirect_or_code: Either the full URL Google redirected to, or the code itself.

    Returns:
        The authorization code.

    Raises:
        ValueError: If a URL was given without a `code` parameter.
    """
    text = redirect_or_code.strip()
    if "://" not in text:
        return text
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query)
    codes = params.get("code")
    if not codes:
        raise ValueError("redirect URL has no code parameter")
    return codes[0]


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    requester: Requester = request_json,
) -> str:
    """Exchange an authorization code for a refresh token.

    Args:
        client_id: OAuth 2.0 client ID.
        client_secret: OAuth 2.0 client secret.
        code: Authorization code from the PCM redirect.
        redirect_uri: The same redirect URI used to build the consent URL.
        requester: HTTP function, injectable for tests.

    Returns:
        The refresh token.

    Raises:
        RuntimeError: If Google did not return a refresh token.
    """
    body = requester(
        "POST",
        TOKEN_URL,
        form_body={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    token = body.get("refresh_token")
    if not token:
        raise RuntimeError(
            "token response had no refresh_token; repeat consent with prompt=consent"
        )
    return str(token)


class TokenProvider:
    """Caches an access token and refreshes it from a refresh token when it expires.

    Thread-safe, because SDM calls run in worker threads.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        requester: Requester = request_json,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the provider.

        Args:
            client_id: OAuth 2.0 client ID.
            client_secret: OAuth 2.0 client secret.
            refresh_token: Long-lived refresh token from exchange_code.
            requester: HTTP function, injectable for tests.
            clock: Monotonic clock, injectable for tests.
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._requester = requester
        self._clock = clock
        self._lock = threading.Lock()
        self._access_token = ""
        self._expires_at = 0.0

    def __repr__(self) -> str:
        """Return a representation without any token material."""
        return f"TokenProvider(client_id={self._client_id!r})"

    def access_token(self) -> str:
        """Return a valid access token, refreshing it if it is missing or about to expire.

        Returns:
            Bearer token for the SDM API.

        Raises:
            RuntimeError: If the refresh response has no access token.
            nest_rtc.http.HttpError: If the token endpoint rejects the refresh.
        """
        with self._lock:
            if self._access_token and self._clock() < self._expires_at:
                return self._access_token
            body = self._requester(
                "POST",
                TOKEN_URL,
                form_body={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            token = body.get("access_token")
            if not token:
                raise RuntimeError("token refresh returned no access_token")
            lifetime = int(body.get("expires_in", 3600))
            self._access_token = str(token)
            self._expires_at = self._clock() + max(lifetime - EXPIRY_MARGIN_SECONDS, 0)
            log.debug("refreshed access token, valid for %ss", lifetime)
            return self._access_token
