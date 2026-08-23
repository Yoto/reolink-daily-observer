#!/bin/sh
set -eu

# Reports, event JSON, SQLite, and logs can reveal private camera activity.
umask 077

if [ "${1:-}" = "scene-author" ]; then
  shift
  exec python -m app.scene_author "$@"
fi

exec python -m app.run_daily "$@"
