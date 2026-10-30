#!/usr/bin/env bash
# Use installed Homebrew tools when present; never install or update silently.
set -euo pipefail
umask 077
HERE="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=''
for PREFIX in /opt/homebrew /usr/local; do
  if [[ -z "$PYTHON" && -x "$PREFIX/opt/python@3.13/bin/python3.13" ]]; then
    PYTHON="$PREFIX/opt/python@3.13/bin/python3.13"
  fi
done
if [[ -z "$PYTHON" ]]; then
  if ! PYTHON="$(command -v python3)"; then
    printf '%s\n' 'ERROR: Python 3.13+ is required. Install python@3.13 with Homebrew.' >&2
    exit 2
  fi
fi
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else "ERROR: Python 3.13+ is required")'
for PREFIX in /opt/homebrew /usr/local; do
  if [[ -x "$PREFIX/bin/brew" ]] && FFMPEG_PREFIX="$("$PREFIX/bin/brew" --prefix ffmpeg 2>/dev/null)"; then
    if [[ -x "$FFMPEG_PREFIX/bin/ffmpeg" && -x "$FFMPEG_PREFIX/bin/ffprobe" ]] &&
       PATH="$FFMPEG_PREFIX/bin:$PATH" "$PYTHON" "$HERE/videorebuild.pyz" doctor >/dev/null 2>&1; then
      # Keep the first complete supported pair; failed probes leave PATH intact.
      export PATH="$FFMPEG_PREFIX/bin:$PATH"
      break
    fi
  fi
done
exec "$PYTHON" "$HERE/videorebuild.pyz" run "$@"
