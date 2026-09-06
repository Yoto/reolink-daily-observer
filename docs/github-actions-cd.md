# GitHub Actions CD

The production deploy is triggered by a successful `test.yml` `push` run on
the repository's `main` branch. GitHub schedules that `workflow_run` job on a
self-hosted runner labelled `nucbox,deploy`; the job does not check out code on
the runner. It passes only the 40-character `head_sha` to the fixed,
root-owned helper:

```text
gha-deploy runner
  └─ sudo -n -u reolink /usr/local/sbin/reolink-deploy <head_sha>
       ├─ fetch public main and verify the exact SHA
       ├─ verify a successful completed test.yml push run for that SHA
       ├─ fast-forward /opt/reolink-analyzer
       ├─ docker compose build analyzer viewer
       └─ docker compose up --wait viewer nginx
```

The helper accepts no repository or path arguments. It runs as `reolink`, uses
the fixed system Docker socket, and acquires
`/opt/reolink-analyzer/.deploy.lock` without waiting. The analyzer systemd
service uses the same lock, so a scheduled analyzer run and deployment cannot
overlap. The service override changes the observed old `ftpuser`/`camera`
configuration to `reolink`/`reolink`; installing it does not start or restart
the service.

The helper refuses a dirty tracked checkout, a detached or non-`main` branch,
a non-fast-forward target, a target that tracks `.env` or
`config/scene.yaml`, and any SHA without the matching public Actions success.
Ignored production `.env` and `config/scene.yaml` remain in place. Build
failure stops before `up`; there is no automatic rollback, and the checkout
may already have advanced to the requested tested commit.

## One-time installation

Review the files in `deploy/`, then as `root` run:

```bash
bash deploy/install-cd.sh
bash deploy/install-runner.sh
```

`install-cd.sh` performs a read-only production preflight first and refuses to
change anything when `/opt/reolink-analyzer` is not a clean `main` checkout or
when its analyzer service is active. It creates `gha-deploy` at
`/home/gha-deploy`, checks that it has no `docker`, `camera`, or
`reolink-analysis` supplemental group, installs the helper and systemd
override, validates the narrow sudoers entry with `visudo`, and reloads the
systemd manager without running the analyzer.

The runner installer consumes the official runner archive and a short-lived
registration token from `/home/codex/camera/cd-bootstrap`. It checks the pinned
SHA256, refuses to overwrite an existing registration, and starts only the
runner service after registering it. Runner registration is separate from the
application deploy helper; the runner has no system Docker access.

The repository's production environment uses the `all_external_contributors`
approval policy. Because a self-hosted runner is a host authority, never
approve a fork workflow change that selects the `nucbox,deploy` runner. The
labels route jobs; they are not an OS sandbox. The production workflow runs
only the reviewed `main` workflow after a successful push test, and the
approved application Compose files are trusted production code with Docker
authority.
