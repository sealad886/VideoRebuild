#!/usr/bin/env bash
set -euo pipefail
umask 077
HERE="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
printf '%s\n' 'Paste each path without surrounding quotes. Inputs are read-only; output must be a separate new folder.'
IFS= read -r -p 'Raw image file (blank for files-only recovery): ' IMAGE
IFS= read -r -p 'PhotoRec recovery root: ' ROOT
IFS= read -r -p 'New output/case folder: ' CASE
if [[ -z "$ROOT" || -z "$CASE" ]]; then
  printf '%s\n' 'ERROR: Recovered-files and output paths are required.' >&2
  exit 2
fi
ARGS=(--input "$ROOT" --case "$CASE" --limit 50)
if [[ -n "$IMAGE" ]]; then ARGS+=(--image "$IMAGE"); fi
exec bash "$HERE/run-macos.command" "${ARGS[@]}"
