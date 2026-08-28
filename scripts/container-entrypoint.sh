#!/bin/sh
set -eu

# Reports, event JSON, SQLite, and logs can reveal private camera activity.
# The viewer runs as a separate user and receives output access only through a
# host supplementary group. Keep artifacts private from everyone else while
# allowing that group to read reports. Operators can tighten this when no
# viewer is deployed.
umask "${ANALYZER_UMASK:-0027}"

if [ "${1:-}" = "scene-author" ]; then
  shift
  exec python -m app.scene_author "$@"
fi

exec python -m app.run_daily "$@"
