#!/bin/sh
set -eu

# Reports, event JSON, SQLite, and logs can reveal private camera activity.
umask 077
exec python -m app.run_daily "$@"
