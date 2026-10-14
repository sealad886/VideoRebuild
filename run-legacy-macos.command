#!/bin/bash
# Guided launcher for macOS. Sources remain read-only; no dependency installation.
set -euo pipefail
trap 'status=$?; if [ "$status" -ne 0 ]; then printf "\nStopped (exit %s). No source media was modified. Review the error above.\n" "$status" >&2; fi' EXIT

ROOT="$(cd -- "$(dirname -- "$0")" && pwd -P)"
if [ "$(uname -s)" != 'Darwin' ]; then
    printf 'This launcher requires macOS. On Linux, use python3 videorebuild.pyz --help.\n' >&2
    exit 2
fi
if command -v brew >/dev/null 2>&1; then
    if prefix="$(brew --prefix python@3.13 2>/dev/null)" && [ -d "$prefix/libexec/bin" ]; then
        export PATH="$prefix/libexec/bin:$PATH"
    fi
    if prefix="$(brew --prefix ffmpeg@7 2>/dev/null)" && [ -d "$prefix/bin" ]; then
        export PATH="$prefix/bin:$PATH"
    fi
fi
for tool in python3 ffmpeg ffprobe osascript; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Missing %s. Install the runtime first: brew install python@3.13 ffmpeg@7\n' "$tool" >&2
        exit 2
    fi
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else "Python 3.11 or newer is required")'
if [ ! -f "$ROOT/videorebuild.pyz" ]; then
    printf 'Missing videorebuild.pyz. From the project folder run: python3 tools/build_zipapp.py\n' >&2
    exit 2
fi
python3 "$ROOT/videorebuild.pyz" doctor
RECOVERY="$(osascript -e 'POSIX path of (choose folder with prompt "Select the parent folder containing your PhotoRec recup_dir folders")')"
DESTINATION="$(osascript -e 'POSIX path of (choose folder with prompt "Select a folder on healthy output storage, outside the recovery tree. VideoRebuild-case will be created or reused here.")')"
CASE="${DESTINATION%/}/VideoRebuild-case"
ADD_IMAGE="$(osascript -e 'button returned of (display dialog "Also search an uncompressed source disk image?" buttons {"No", "Yes"} default button "No")')"
ARGS=(run --input "$RECOVERY" --case "$CASE" --deep)
if [ "$ADD_IMAGE" = 'Yes' ]; then
    IMAGE="$(osascript -e 'POSIX path of (choose file with prompt "Select the raw source disk image, not a device or compressed archive")')"
    ARGS+=(--image "$IMAGE")
fi
BATCH="$(osascript -e 'button returned of (display dialog "Reconstruct a 50-record batch or all remaining records? Both choices index the entire input collection first." buttons {"50-record batch", "All remaining"} default button "50-record batch")')"
if [ "$BATCH" = '50-record batch' ]; then
    ARGS+=(--limit 50)
fi
printf '\nCase directory: %s\n' "$CASE"
python3 "$ROOT/videorebuild.pyz" "${ARGS[@]}"
open "$CASE/reports/index.html"
