#!/bin/sh
set -u

project_dir="${REOLINK_PROJECT_DIR:-/opt/reolink-analyzer}"
docker_bin="${REOLINK_DOCKER_BIN:-/usr/bin/docker}"
docker_host="${REOLINK_DOCKER_HOST:-unix:///var/run/docker.sock}"

compose() {
  "$docker_bin" -H "$docker_host" compose \
    --project-directory "$project_dir" \
    --env-file "$project_dir/.env" \
    -f "$project_dir/docker-compose.yml" "$@"
}

compose run --rm analyzer
analyzer_status=$?

# Exit 2 is a completed but degraded report. Retention independently verifies
# every expired date before deleting it, so one partial current day must not
# prevent cleanup of older, fully processed dates.
case "$analyzer_status" in
  0|2)
    compose run --rm retention retention
    retention_status=$?
    if [ "$retention_status" -ne 0 ]; then
      exit "$retention_status"
    fi
    ;;
  *)
    exit "$analyzer_status"
    ;;
esac

exit "$analyzer_status"
