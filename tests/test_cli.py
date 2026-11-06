"""Tests for camera selection and argument parsing."""

# pylint: disable=missing-function-docstring,missing-class-docstring,unused-argument
import pytest

from nest_rtc.cli import CameraNotFoundError, build_parser, resolve_camera
from nest_rtc.sdm import Device

DEVICES = [
    Device("enterprises/p/devices/a1", "sdm.devices.types.CAMERA", "Backyard", ("WEB_RTC",)),
    Device("enterprises/p/devices/b2", "sdm.devices.types.CAMERA", "Office", ("WEB_RTC",)),
    Device("enterprises/p/devices/c3", "sdm.devices.types.CAMERA", "Office", ("WEB_RTC",)),
]


def test_resolve_by_name_case_insensitive() -> None:
    assert resolve_camera(DEVICES, "backyard").device_id == "a1"


def test_resolve_by_id_beats_duplicate_names() -> None:
    assert resolve_camera(DEVICES, "c3").device_id == "c3"


def test_resolve_ambiguous() -> None:
    with pytest.raises(CameraNotFoundError, match="ambiguous"):
        resolve_camera(DEVICES, "office")


def test_resolve_missing_lists_names() -> None:
    with pytest.raises(CameraNotFoundError, match="Backyard"):
        resolve_camera(DEVICES, "garage")


def test_parser_rejects_nonpositive_duration() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["raw", "Backyard", "-t", "0"])
