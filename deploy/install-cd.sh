#!/usr/bin/env bash
set -euo pipefail

# Run as root from this reviewed checkout. This installer never starts the
# analyzer and never starts or stops the production containers.
if [[ "${EUID}" -ne 0 ]]; then
  echo "install-cd.sh must be run as root" >&2
  exit 1
fi

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo="/opt/reolink-analyzer"
helper_source="$script_dir/reolink-deploy.py"
override_source="$script_dir/reolink-analyzer.override.conf"

reolink_git() {
  /usr/sbin/runuser -u reolink -- /usr/bin/git -C "$repo" "$@"
}

if [[ ! -f "$helper_source" || ! -f "$override_source" ]]; then
  echo "reviewed deployment files are missing" >&2
  exit 1
fi

if ! production_sha="$(reolink_git rev-parse HEAD 2>/dev/null)"; then
  echo "production preflight failed: checkout is unavailable" >&2
  exit 1
fi
if ! production_branch="$(reolink_git symbolic-ref --quiet --short HEAD 2>/dev/null)"; then
  production_branch="detached"
fi
if ! production_status="$(reolink_git status --porcelain=v1 --untracked-files=no --ignore-submodules=all 2>/dev/null)"; then
  echo "production preflight failed: could not inspect tracked status" >&2
  exit 1
fi
if [[ -n "$production_status" ]]; then
  tracked_dirty=true
else
  tracked_dirty=false
fi
printf 'production preflight: sha=%s branch=%s tracked-dirty=%s\n' "$production_sha" "$production_branch" "$tracked_dirty"
if [[ "$production_branch" != "main" || "$tracked_dirty" != false ]]; then
  echo "production preflight failed: checkout must be clean on main; no files were changed" >&2
  exit 1
fi

service_state="$(/usr/bin/systemctl show -p ActiveState --value reolink-analyzer.service 2>/dev/null || true)"
case "$service_state" in
  active|activating|reloading|deactivating)
  echo "production preflight failed: reolink-analyzer.service is active or activating" >&2
  exit 1
  ;;
esac

if ! /usr/bin/getent passwd gha-deploy >/dev/null; then
  /usr/sbin/useradd --create-home --home-dir /home/gha-deploy --shell /bin/bash gha-deploy
fi
gha_home="$(/usr/bin/getent passwd gha-deploy | /usr/bin/cut -d: -f6)"
if [[ "$gha_home" != "/home/gha-deploy" ]]; then
  echo "gha-deploy exists with an unexpected home directory" >&2
  exit 1
fi
for forbidden_group in docker camera reolink-analysis; do
  if /usr/bin/id -nG gha-deploy | /usr/bin/tr ' ' '\n' | /usr/bin/grep -Fxq "$forbidden_group"; then
    echo "gha-deploy must not belong to $forbidden_group" >&2
    exit 1
  fi
done
/usr/bin/chmod 0700 /home/gha-deploy

/usr/bin/install -o root -g root -m 0755 "$helper_source" /usr/local/sbin/reolink-deploy
/usr/bin/install -d -o root -g root -m 0755 /etc/systemd/system/reolink-analyzer.service.d
/usr/bin/install -o root -g root -m 0644 "$override_source" \
  /etc/systemd/system/reolink-analyzer.service.d/10-cd-lock.conf

sudoers_tmp="$(/usr/bin/mktemp /etc/sudoers.d/reolink-deploy.XXXXXX)"
trap '/usr/bin/rm -f "$sudoers_tmp"' EXIT
/usr/bin/chown root:root "$sudoers_tmp"
/usr/bin/chmod 0440 "$sudoers_tmp"
/usr/bin/printf '%s\n' 'gha-deploy ALL=(reolink) NOPASSWD: /usr/local/sbin/reolink-deploy' > "$sudoers_tmp"
/usr/sbin/visudo -cf "$sudoers_tmp"
/usr/bin/install -o root -g root -m 0440 "$sudoers_tmp" /etc/sudoers.d/reolink-deploy
/usr/bin/rm -f "$sudoers_tmp"
trap - EXIT

/usr/bin/systemctl daemon-reload
printf '%s\n' "installed /usr/local/sbin/reolink-deploy and the analyzer lock override" \
  "installed the gha-deploy sudo rule; no service was started or restarted" \
  "next step: run the separately reviewed deploy/install-runner.sh as root"
