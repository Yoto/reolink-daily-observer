#!/usr/bin/env bash
set -euo pipefail

# Root-only, fixed-input installer for the already downloaded official runner.
# It intentionally does not use --replace and never prints the registration token.
if [[ "${EUID}" -ne 0 ]]; then
  echo "install-runner.sh must be run as root" >&2
  exit 1
fi

staging=/home/codex/camera/cd-bootstrap
archive="$staging/actions-runner-linux-x64-2.337.0.tar.gz"
token_file="$staging/registration-token"
target=/home/gha-deploy/actions-runner
expected_sha=70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613

if [[ ! -r "$archive" || ! -r "$token_file" ]]; then
  echo "runner staging files are missing" >&2
  exit 1
fi
if [[ "$(/usr/bin/stat -c '%a' "$token_file")" != 600 ]]; then
  echo "registration-token must have mode 600" >&2
  exit 1
fi
actual_sha="$(/usr/bin/sha256sum "$archive" | /usr/bin/awk '{print $1}')"
if [[ "$actual_sha" != "$expected_sha" ]]; then
  echo "runner archive SHA256 does not match the reviewed version" >&2
  exit 1
fi
if [[ -e "$target/.runner" ]]; then
  if /usr/bin/grep -Fq '"gitHubUrl": "https://github.com/Yoto/reolink-daily-observer"' "$target/.runner" && \
     /usr/bin/grep -Fq '"agentName": "nucbox-deploy"' "$target/.runner"; then
    echo "runner is already registered as nucbox-deploy; no changes made"
    exit 0
  fi
  echo "an existing runner registration does not match nucbox-deploy" >&2
  exit 1
fi
if [[ -d "$target" && -n "$(/usr/bin/find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "runner target is non-empty without a matching registration; refusing overwrite" >&2
  exit 1
fi
if [[ -s "$token_file" ]]; then
  token="$(/usr/bin/tr -d '\r\n' < "$token_file")"
else
  echo "registration-token is empty" >&2
  exit 1
fi
/usr/bin/install -d -o gha-deploy -g gha-deploy -m 0700 "$target"

package="$target/.runner-package.tgz"
/usr/bin/install -o gha-deploy -g gha-deploy -m 0600 "$archive" "$package"
/usr/sbin/runuser -u gha-deploy -- /usr/bin/tar -xzf "$package" -C "$target"
/usr/bin/rm -f "$package"

(cd "$target" && /usr/sbin/runuser -u gha-deploy -- ./config.sh \
  --unattended \
  --url https://github.com/Yoto/reolink-daily-observer \
  --token "$token" \
  --name nucbox-deploy \
  --labels nucbox,deploy \
  --work _work)
unset token

(cd "$target" && ./svc.sh install gha-deploy)
(cd "$target" && ./svc.sh start)
echo "registered and started the nucbox-deploy runner"
