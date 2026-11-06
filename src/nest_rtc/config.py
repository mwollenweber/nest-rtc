"""Settings and refresh-token storage.

Client ID, client secret and Device Access project ID come from environment variables.
The refresh token lives in a 0600 file under the user's config directory, or in
NEST_REFRESH_TOKEN when set. Secrets are never logged.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ENV_PROJECT_ID = "NEST_PROJECT_ID"
ENV_CLIENT_ID = "NEST_CLIENT_ID"
ENV_CLIENT_SECRET = "NEST_CLIENT_SECRET"  # noqa: S105 - variable name, not a secret
ENV_REFRESH_TOKEN = "NEST_REFRESH_TOKEN"  # noqa: S105 - variable name, not a secret
ENV_CREDENTIALS_FILE = "NEST_CREDENTIALS_FILE"


class ConfigError(RuntimeError):
    """Raised when required settings or credentials are missing."""


@dataclass(frozen=True, slots=True)
class Settings:
    """OAuth client and Device Access project settings.

    Attributes:
        project_id: Device Access project ID (a UUID from the Device Access Console).
        client_id: OAuth 2.0 client ID from the linked Google Cloud project.
        client_secret: OAuth 2.0 client secret.
    """

    project_id: str
    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        """Return a representation that omits the client secret."""
        return f"Settings(project_id={self.project_id!r}, client_id={self.client_id!r})"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Read settings from environment variables.

    Args:
        env: Mapping to read from. Defaults to os.environ.

    Returns:
        The populated Settings.

    Raises:
        ConfigError: If any required variable is unset or empty.
    """
    source = os.environ if env is None else env
    names = (ENV_PROJECT_ID, ENV_CLIENT_ID, ENV_CLIENT_SECRET)
    missing = [name for name in names if not source.get(name)]
    if missing:
        raise ConfigError(f"missing environment variables: {', '.join(missing)}")
    return Settings(
        project_id=source[ENV_PROJECT_ID],
        client_id=source[ENV_CLIENT_ID],
        client_secret=source[ENV_CLIENT_SECRET],
    )


def credentials_path(env: dict[str, str] | None = None) -> Path:
    """Return the path of the refresh-token file.

    Args:
        env: Mapping to read from. Defaults to os.environ.

    Returns:
        NEST_CREDENTIALS_FILE if set, otherwise $XDG_CONFIG_HOME/nest-rtc/credentials.json
        (falling back to ~/.config).
    """
    source = os.environ if env is None else env
    if override := source.get(ENV_CREDENTIALS_FILE):
        return Path(override).expanduser()
    base = Path(source.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "nest-rtc" / "credentials.json"


def save_refresh_token(token: str, path: Path) -> None:
    """Write the refresh token to disk readable only by the current user.

    The file is created with mode 0600 before any content is written, so the token
    is never briefly world-readable.

    Args:
        token: OAuth refresh token.
        path: Destination file.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"refresh_token": token}, handle)
    os.chmod(path, 0o600)
    log.info("saved refresh token to %s", path)


def load_refresh_token(path: Path, env: dict[str, str] | None = None) -> str:
    """Return the refresh token from the environment or the credentials file.

    Args:
        path: Credentials file written by save_refresh_token.
        env: Mapping to read from. Defaults to os.environ.

    Returns:
        The refresh token.

    Raises:
        ConfigError: If neither source holds a token.
    """
    source = os.environ if env is None else env
    if token := source.get(ENV_REFRESH_TOKEN):
        return token
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"no refresh token at {path}; run `nest-rtc auth` first") from exc
    token = data.get("refresh_token")
    if not token:
        raise ConfigError(f"{path} has no refresh_token")
    if path.stat().st_mode & 0o077:
        log.warning("%s is readable by other users; chmod 600 it", path)
    return token
