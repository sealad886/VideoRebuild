#!/usr/bin/env bash
set -euo pipefail
HERE="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$HERE/run-fragmented-macos.command" "$@"
