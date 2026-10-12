#!/bin/sh
# Backward-compatible entrypoint. Maphesto now runs as a TUI.
exec "$(dirname "$0")/maphesto" "$@"
