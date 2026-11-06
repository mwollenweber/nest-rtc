"""Minimal JSON-over-HTTPS helpers built on the standard library.

Calls block, so async callers should run them with asyncio.to_thread.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30


class HttpError(RuntimeError):
    """Raised for non-2xx responses and for network failures.

    Attributes:
        status: HTTP status code, or 0 when no response arrived (reason NETWORK_ERROR).
        reason: Google API error status (for example RESOURCE_EXHAUSTED), if present.
        message: Error message from the response body, if present.
    """

    def __init__(self, status: int, reason: str, message: str) -> None:
        """Initialize the error.

        Args:
            status: HTTP status code.
            reason: Google API error status, or an empty string.
            message: Error message from the body, or an empty string.
        """
        super().__init__(f"HTTP {status} {reason}: {message}".strip())
        self.status = status
        self.reason = reason
        self.message = message


def _parse_error(exc: urllib.error.HTTPError) -> HttpError:
    raw = exc.read().decode("utf-8", errors="replace")
    reason, message = "", raw[:500]
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return HttpError(exc.code, reason, message)
    err = body.get("error")
    if isinstance(err, dict):
        reason = str(err.get("status", ""))
        message = str(err.get("message", ""))
    elif isinstance(err, str):
        # OAuth token endpoint errors look like {"error": "invalid_grant", ...}.
        reason = err
        message = str(body.get("error_description", ""))
    return HttpError(exc.code, reason, message)


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send an HTTPS request and decode a JSON response.

    Args:
        method: HTTP method.
        url: Absolute https URL.
        headers: Extra request headers.
        json_body: Body to send as application/json.
        form_body: Body to send as application/x-www-form-urlencoded.

    Returns:
        The decoded JSON object, or an empty dict for an empty body.

    Raises:
        ValueError: If the URL is not https or both body kinds are given.
        HttpError: For non-2xx responses.
    """
    if not url.startswith("https://"):
        raise ValueError("only https URLs are allowed")
    if json_body is not None and form_body is not None:
        raise ValueError("pass json_body or form_body, not both")
    all_headers = {"Accept": "application/json", **(headers or {})}
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        all_headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)  # noqa: S310
    # Log the path only. Query strings and bodies can carry tokens.
    log.debug("%s %s", method, urllib.parse.urlsplit(url).path)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise _parse_error(exc) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        # Network failures (DNS, TLS handshake timeout, reset) surface as HttpError so
        # callers handle them with API errors instead of crashing with a traceback.
        reason = getattr(exc, "reason", exc)
        raise HttpError(
            0, "NETWORK_ERROR", f"{method} {urllib.parse.urlsplit(url).path}: {reason}"
        ) from None
    return json.loads(raw) if raw else {}
