"""Command-line interface: auth, list, record, raw."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from nest_rtc import auth, config
from nest_rtc.http import HttpError
from nest_rtc.sdm import CameraUnavailableError, Device, SdmClient
from nest_rtc.stream import PACKED_FORMATS, NestLiveStream, NoVideoError, record, write_raw

log = logging.getLogger("nest_rtc")


class CameraNotFoundError(LookupError):
    """Raised when a camera selector matches nothing, or more than one device."""


def resolve_camera(devices: Sequence[Device], selector: str) -> Device:
    """Find one camera by display name, short ID or full resource name.

    Args:
        devices: Devices from devices.list.
        selector: Case-insensitive display name, device ID, or resource name.

    Returns:
        The single matching device.

    Raises:
        CameraNotFoundError: If nothing or more than one device matches.
    """
    wanted = selector.strip()
    exact = [d for d in devices if wanted in {d.name, d.device_id}]
    matches = exact or [d for d in devices if d.display_name.lower() == wanted.lower()]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(sorted(d.display_name for d in devices)) or "none"
    if not matches:
        raise CameraNotFoundError(f"no camera matches {selector!r}; available: {names}")
    raise CameraNotFoundError(f"{selector!r} is ambiguous; use the device ID from `nest-rtc list`")


def _client(settings: config.Settings) -> SdmClient:
    token = config.load_refresh_token(config.credentials_path())
    provider = auth.TokenProvider(settings.client_id, settings.client_secret, token)
    return SdmClient(settings.project_id, provider)


def cmd_auth(args: argparse.Namespace, settings: config.Settings) -> int:
    """Run the one-time PCM consent flow and store the refresh token.

    Args:
        args: Parsed arguments.
        settings: Loaded settings.

    Returns:
        Process exit code.
    """
    url = auth.build_authorization_url(settings.project_id, settings.client_id, args.redirect_uri)
    sys.stderr.write(
        "Open this URL, pick the cameras to share, and approve:\n\n"
        f"  {url}\n\n"
        "Paste the full URL you were redirected to (or just the code): "
    )
    sys.stderr.flush()
    code = auth.extract_code(sys.stdin.readline())
    token = auth.exchange_code(settings.client_id, settings.client_secret, code, args.redirect_uri)
    path = config.credentials_path()
    config.save_refresh_token(token, path)
    sys.stderr.write(f"Saved refresh token to {path}\n")
    return 0


def cmd_list(args: argparse.Namespace, settings: config.Settings) -> int:
    """Print devices with their streaming capabilities.

    Args:
        args: Parsed arguments.
        settings: Loaded settings.

    Returns:
        Process exit code.
    """
    devices = _client(settings).list_devices()
    if args.json:
        rows = [
            {
                "name": d.display_name,
                "device_id": d.device_id,
                "type": d.device_type,
                "protocols": list(d.protocols),
                "max_resolution": list(d.max_resolution) if d.max_resolution else None,
                "video_codecs": list(d.video_codecs),
            }
            for d in devices
        ]
        sys.stdout.write(json.dumps(rows, indent=2) + "\n")
        return 0
    for d in devices:
        res = "x".join(map(str, d.max_resolution)) if d.max_resolution else "-"
        protocols = ",".join(d.protocols) or "-"
        kind = d.device_type.rsplit(".", 1)[-1]
        sys.stdout.write(
            f"{d.display_name:<28} {kind:<11} {protocols:<12} {res:<10} {d.device_id}\n"
        )
    return 0


async def _record(client: SdmClient, device: Device, args: argparse.Namespace) -> None:
    async with NestLiveStream(client, device.name, keepalive=not args.no_keepalive) as stream:
        await record(stream, args.output, args.duration)


async def _raw(client: SdmClient, device: Device, args: argparse.Namespace) -> None:
    async with NestLiveStream(client, device.name, keepalive=not args.no_keepalive) as stream:
        frames, width, height = await write_raw(
            stream, sys.stdout.buffer, args.duration, args.pix_fmt
        )
    log.info("wrote %d frames at %dx%d", frames, width, height)


def _pick_streamable(client: SdmClient, selector: str) -> Device:
    device = resolve_camera(client.list_devices(), selector)
    if not device.supports_webrtc:
        protocols = ",".join(device.protocols) or "none"
        raise CameraNotFoundError(f"{device.display_name} does not offer WebRTC ({protocols})")
    return device


def cmd_record(args: argparse.Namespace, settings: config.Settings) -> int:
    """Record a camera to a media file.

    Args:
        args: Parsed arguments.
        settings: Loaded settings.

    Returns:
        Process exit code.
    """
    client = _client(settings)
    asyncio.run(_record(client, _pick_streamable(client, args.camera), args))
    return 0


def cmd_raw(args: argparse.Namespace, settings: config.Settings) -> int:
    """Write raw decoded frames to stdout.

    Args:
        args: Parsed arguments.
        settings: Loaded settings.

    Returns:
        Process exit code.
    """
    if sys.stdout.isatty():
        log.error("refusing to write raw video to a terminal; pipe it into ffplay or a file")
        return 2
    client = _client(settings)
    asyncio.run(_raw(client, _pick_streamable(client, args.camera), args))
    return 0


def _positive(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="nest-rtc",
        description="Pull live WebRTC video from Google Nest cameras via the SDM API.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="authorize cameras and store a refresh token")
    p_auth.add_argument("--redirect-uri", default=auth.DEFAULT_REDIRECT_URI)
    p_auth.set_defaults(func=cmd_auth)

    p_list = sub.add_parser("list", help="list devices, protocols and max resolution")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    stream_opts = argparse.ArgumentParser(add_help=False)
    stream_opts.add_argument("camera", help="display name, device ID or resource name")
    stream_opts.add_argument(
        "-t", "--duration", type=_positive, help="seconds to capture (default: until it ends)"
    )
    stream_opts.add_argument(
        "--no-keepalive", action="store_true", help="do not extend past the first 5 minutes"
    )

    p_rec = sub.add_parser("record", parents=[stream_opts], help="record to a media file")
    p_rec.add_argument("-o", "--output", type=Path, required=True, help="e.g. backyard.mp4")
    p_rec.set_defaults(func=cmd_record)

    p_raw = sub.add_parser("raw", parents=[stream_opts], help="raw frames to stdout")
    p_raw.add_argument("--pix-fmt", default="bgr24", choices=sorted(PACKED_FORMATS))
    p_raw.set_defaults(func=cmd_raw)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Arguments, defaulting to sys.argv[1:].

    Returns:
        Process exit code.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not args.verbose:
        # aiortc and aioice are chatty at INFO.
        logging.getLogger("aiortc").setLevel(logging.WARNING)
        logging.getLogger("aioice").setLevel(logging.WARNING)
    try:
        settings = config.load_settings()
        return int(args.func(args, settings))
    except (
        config.ConfigError,
        CameraNotFoundError,
        CameraUnavailableError,
        NoVideoError,
        ValueError,
    ) as exc:
        log.error("%s", exc)
        return 1
    except HttpError as exc:
        log.error("API error: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
