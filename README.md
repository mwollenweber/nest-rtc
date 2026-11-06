# nest-rtc

Pull live video from Google Nest cameras over WebRTC through Google's Smart Device
Management (SDM) API. Cameras managed in the Google Home app only expose WebRTC, so this
tool negotiates the session with aiortc, keeps it alive past Google's 5-minute limit, and
hands you decoded frames as raw pixels or a recorded file.

## One-time setup

1. Register for Device Access at https://console.nest.google.com/device-access. Google
   charges a one-time US$5 fee and requires a consumer Google account (not Workspace).
   The account tied to the project cannot be changed later.
2. In Google Cloud, create a project, enable the Smart Device Management API, and create
   an OAuth client of type "Web application" with `https://www.google.com` as an
   authorized redirect URI.
3. Back in the Device Access Console, create a project and paste in the OAuth client ID.
   Note the Device Access project ID.
4. Export the three settings. Keep the secret out of shell history and dotfiles in git.

   ```bash
   export NEST_PROJECT_ID=...      # Device Access project ID (UUID)
   export NEST_CLIENT_ID=...       # OAuth client ID
   export NEST_CLIENT_SECRET=...   # OAuth client secret
   ```

5. Install and authorize:

   ```bash
   uv sync
   uv run nest-rtc auth
   ```

   Open the printed URL, sign in with the account that owns the cameras, select the
   cameras to share, and paste back the URL Google redirects to. The refresh token is
   written to `~/.config/nest-rtc/credentials.json` with mode 0600. Set
   `NEST_CREDENTIALS_FILE` to store it elsewhere, or `NEST_REFRESH_TOKEN` to skip the file.

If the OAuth consent screen is left in "Testing", Google expires the refresh token after
7 days and you will need to run `auth` again. Publishing the consent screen to production
avoids this. Google's Device Access docs say OAuth verification is not required for
personal use.

## Usage

```bash
# What you have, how it streams, and the advertised max resolution.
uv run nest-rtc list

# Record 60 seconds to MP4.
uv run nest-rtc record Backyard -t 60 -o backyard.mp4

# Raw BGR frames to stdout. The size is logged to stderr on the first frame.
uv run nest-rtc raw Backyard | ffplay -f rawvideo -pixel_format bgr24 -video_size 1920x1080 -
```

Cameras are selected by display name (case-insensitive), device ID, or full resource
name. If two cameras share a name, use the device ID from `list`.

Reading raw frames from Python:

```python
import subprocess

import numpy as np

W, H = 1920, 1080  # from `nest-rtc list` or the stderr log line
proc = subprocess.Popen(["nest-rtc", "raw", "Backyard"], stdout=subprocess.PIPE)
frame_size = W * H * 3
while chunk := proc.stdout.read(frame_size):
    frame = np.frombuffer(chunk, np.uint8).reshape(H, W, 3)  # BGR, ready for OpenCV
```

Using the library directly:

```python
from nest_rtc.stream import NestLiveStream

async with NestLiveStream(client, device.name) as live:
    while not live.ended.is_set():
        frame = await live.video.recv()  # av.VideoFrame
```

## Limits and behavior

- Google caps each session at 5 minutes. The tool extends it 60 seconds before expiry.
  Google ignores extension for cameras on battery power, so those streams end after one
  session. The tool detects the ignored extension and logs it.
- The Sandbox allows 10 `executeCommand` calls per minute per project and 5 per minute
  per device per command. A client-side limiter waits instead of tripping the server
  limit, and throttled calls are retried after 15, 30 and 60 seconds.
- Video arrives through Google's servers, never over the LAN. The host needs outbound
  UDP for ICE and media.
- Audio is received, because Nest requires the audio m-line, and then discarded.
- `record` decodes and re-encodes to H264. `raw` scales any later frame to the first
  frame's size so a fixed-size reader never desynchronizes.

## Data handling

The tool keeps nothing beyond the refresh token file and the output you ask for. It sends
no telemetry. Logs carry device names and session expiry times, never tokens, SDP or
request bodies.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pylint src/nest_rtc tests
```

The stream tests use a loopback: a fake SDM client answers the offer with a second
aiortc peer that sends synthetic video. They exercise negotiation, keepalive, the
battery-camera path and both sinks without a camera, but they do not prove Google
accepts the offer.
