#!/usr/bin/env bash
set -euo pipefail
umask 077
HERE="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
printf '%s\n' 'Paste each path without surrounding quotes. Inputs are read-only; output must be a separate new folder.'
IFS= read -r -p 'Raw image file: ' IMAGE
IFS= read -r -p 'PhotoRec recovery root: ' ROOT
IFS= read -r -p 'New output/case folder: ' CASE
if [[ -z "$IMAGE" || -z "$ROOT" || -z "$CASE" ]]; then
  printf '%s\n' 'ERROR: All three paths are required.' >&2
  exit 2
fi
exec bash "$HERE/run-fragmented-macos.command" --image "$IMAGE" --root "$ROOT" --case "$CASE" --limit 50
