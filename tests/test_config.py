"""Tests for settings and refresh-token storage."""

# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument
import stat

import pytest

from nest_rtc import config


def test_load_settings_reports_all_missing() -> None:
    with pytest.raises(config.ConfigError) as err:
        config.load_settings({})
    assert "NEST_PROJECT_ID" in str(err.value)
    assert "NEST_CLIENT_SECRET" in str(err.value)


def test_settings_repr_hides_secret() -> None:
    settings = config.load_settings(
        {"NEST_PROJECT_ID": "p", "NEST_CLIENT_ID": "c", "NEST_CLIENT_SECRET": "hunter2"}
    )
    assert "hunter2" not in repr(settings)


def test_refresh_token_round_trip_is_private(tmp_path) -> None:
    path = tmp_path / "sub" / "credentials.json"
    config.save_refresh_token("rt-123", path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config.load_refresh_token(path, env={}) == "rt-123"


def test_env_refresh_token_wins(tmp_path) -> None:
    assert config.load_refresh_token(tmp_path / "none.json", {"NEST_REFRESH_TOKEN": "env"}) == "env"


def test_missing_token_file_points_to_auth(tmp_path) -> None:
    with pytest.raises(config.ConfigError, match="nest-rtc auth"):
        config.load_refresh_token(tmp_path / "none.json", env={})


def test_credentials_path_honours_xdg(tmp_path) -> None:
    path = config.credentials_path({"XDG_CONFIG_HOME": str(tmp_path)})
    assert path == tmp_path / "nest-rtc" / "credentials.json"
