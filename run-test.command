#!/bin/zsh
# Test harness for nest-rtc: records Backyard and verifies the file. Output goes to run.log.
cd "${0:A:h}" || exit 1
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
source ./config.rc
{
  echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) record"
  rm -f backyard.mp4
  uv run nest-rtc record Backyard -t 30 -o backyard.mp4
  echo "=== exit $?"
  uv run python -c '
import av, sys
try:
    with av.open("backyard.mp4") as c:
        v = c.streams.video[0]
        n = sum(1 for _ in c.decode(v))
        print(f"verify: {v.codec_context.name} {v.width}x{v.height} frames={n} duration={float(c.duration or 0)/1e6:.1f}s")
except Exception as e:
    print("verify failed:", e); sys.exit(1)
'
  echo "=== done"
} > run.log 2>&1
