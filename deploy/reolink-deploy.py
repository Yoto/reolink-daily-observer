#!/usr/bin/python3 -I
"""Deploy one successful main commit to the production checkout.

This file is installed as a root-owned executable at /usr/local/sbin.  It is
deliberately self-contained: the runner supplies only a commit SHA, and no
repository-controlled code is imported or executed before the SHA has passed
the GitHub Actions gate.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
from typing import Any, Callable, ContextManager, Iterable
import urllib.parse
import urllib.request


REPOSITORY = "Yoto/reolink-daily-observer"
REPOSITORY_URL = "https://github.com/Yoto/reolink-daily-observer.git"
REPOSITORY_PATH = Path("/opt/reolink-analyzer")
LOCK_PATH = REPOSITORY_PATH / ".deploy.lock"
DOCKER = "/usr/bin/docker"
GIT = "/usr/bin/git"
DOCKER_SOCKET = "unix:///var/run/docker.sock"
ERROR_LOG = Path("/var/lib/reolink/deploy-last-error.log")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


def safe_env() -> dict[str, str]:
    """Return only the environment needed by fixed git/docker subprocesses."""

    return {
        "HOME": pwd.getpwnam("reolink").pw_dir,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "GIT_TERMINAL_PROMPT": "0",
        "DOCKER_HOST": DOCKER_SOCKET,
    }


class DeploymentError(RuntimeError):
    """An expected, user-actionable deployment failure."""


def record_failure(label: str, returncode: int, stdout: str, stderr: str) -> None:
    """Keep failed command output private for the administrator to inspect."""

    try:
        fd = os.open(ERROR_LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            stream = os.fdopen(fd, "w", encoding="utf-8")
            fd = -1
            with stream:
                stream.write(
                    f"stage: {label}\nexit: {returncode}\n\nstdout:\n{stdout}\nstderr:\n{stderr}"
                )
        finally:
            if fd != -1:
                os.close(fd)
    except OSError:
        # The public error remains safe and actionable even if diagnostics
        # cannot be written (for example, during early installation).
        pass


def validate_sha(value: str) -> str:
    if not SHA_PATTERN.fullmatch(value):
        raise DeploymentError("commit SHA must be exactly 40 lowercase hexadecimal characters")
    return value


def run_checked(
    argv: Iterable[str],
    *,
    cwd: Path | None = None,
    allow_output: bool = False,
    failure_label: str | None = None,
) -> str:
    """Run a fixed executable with a clean environment and hide command output."""

    effective_cwd = cwd or REPOSITORY_PATH
    completed = subprocess.run(
        list(argv),
        cwd=str(effective_cwd),
        env=safe_env(),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode:
        label = failure_label or (Path(completed.args[0]).name if completed.args else "command")
        record_failure(label, completed.returncode, completed.stdout, completed.stderr)
        raise DeploymentError(f"{label} failed; diagnostics at {ERROR_LOG}")
    return completed.stdout if allow_output else ""


def assert_running_as_reolink() -> None:
    try:
        username = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError as exc:
        raise DeploymentError("could not identify the effective user") from exc
    if username != "reolink":
        raise DeploymentError("helper must run as the reolink user")


def acquire_lock(path: Path) -> ContextManager[Any]:
    """Return a non-blocking lock context for deploy and scheduled analyzer runs."""

    class Lock:
        def __enter__(self) -> Any:
            self.handle = path.open("a+")
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self.handle.close()
                raise DeploymentError("another deploy or analyzer run holds the deployment lock") from exc
            return self.handle

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()

    return Lock()


def successful_ci_run(run: dict[str, Any], sha: str) -> bool:
    return (
        run.get("head_sha") == sha
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
    )


def verify_successful_ci(sha: str) -> None:
    query = urllib.parse.urlencode(
        {"head_sha": sha, "event": "push", "status": "completed", "per_page": "100"}
    )
    url = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/test.yml/runs?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "reolink-deploy",
        },
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=15) as response:
            payload = json.load(response)
    except Exception as exc:  # network and JSON errors must not bypass the gate
        raise DeploymentError("could not verify the GitHub Actions test run") from exc
    runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
    if not any(isinstance(run, dict) and successful_ci_run(run, sha) for run in runs):
        raise DeploymentError("no successful completed test.yml push run exists for this commit")


def compose_command(repo: Path, *args: str) -> list[str]:
    return [
        DOCKER,
        "-H",
        DOCKER_SOCKET,
        "compose",
        "--project-directory",
        str(repo),
        "--env-file",
        str(repo / ".env"),
        "-f",
        str(repo / "docker-compose.yml"),
        *args,
    ]


def preflight(repo: Path, requested_sha: str) -> None:
    if not repo.is_dir():
        raise DeploymentError("production checkout does not exist")
    branch = run_checked(
        [GIT, "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo,
        allow_output=True,
    ).strip()
    if branch != "main":
        raise DeploymentError("production checkout is not on main")
    dirty = run_checked(
        [GIT, "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=no", "--ignore-submodules=all"],
        cwd=repo,
        allow_output=True,
    ).strip()
    if dirty:
        raise DeploymentError("production checkout has tracked changes")


def ensure_target_safe(repo: Path, requested_sha: str) -> None:
    tracked_sensitive = run_checked(
        [GIT, "-C", str(repo), "ls-tree", "-r", "--name-only", requested_sha, "--", ".env", "config/scene.yaml"],
        cwd=repo,
        allow_output=True,
    ).splitlines()
    if set(tracked_sensitive) & {".env", "config/scene.yaml"}:
        raise DeploymentError("target commit tracks a production-sensitive file")


def fetch_and_validate(repo: Path, requested_sha: str) -> None:
    run_checked(
        [
            GIT,
            "-C",
            str(repo),
            "fetch",
            "--no-tags",
            "--prune",
            "--force",
            REPOSITORY_URL,
            "main:refs/remotes/cd/main",
        ],
        cwd=repo,
    )
    fetched = run_checked(
        [GIT, "-C", str(repo), "rev-parse", "refs/remotes/cd/main^{commit}"], cwd=repo, allow_output=True
    ).strip()
    if fetched != requested_sha:
        raise DeploymentError("requested SHA is not the current fetched main commit")
    ancestor = subprocess.run(
        [GIT, "-C", str(repo), "merge-base", "--is-ancestor", "HEAD", requested_sha],
        env=safe_env(),
        cwd=str(repo),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ancestor.returncode:
        raise DeploymentError("requested commit is not a fast-forward from production")


def update_checkout(repo: Path, requested_sha: str) -> None:
    # Ignored .env and config/scene.yaml are preserved; tracked copies were
    # rejected by preflight before this point. Disable repository hooks while
    # advancing the checkout so this step cannot execute checkout-provided code.
    run_checked(
        [GIT, "-C", str(repo), "-c", "core.hooksPath=/dev/null", "merge", "--ff-only", requested_sha],
        cwd=repo,
        failure_label="git fast-forward",
    )
    deployed = run_checked([GIT, "-C", str(repo), "rev-parse", "HEAD"], cwd=repo, allow_output=True).strip()
    if deployed != requested_sha:
        raise DeploymentError("production checkout did not reach the requested SHA")


def check_services(repo: Path) -> None:
    raw = run_checked(compose_command(repo, "ps", "--format", "json", "viewer", "nginx"), cwd=repo, allow_output=True)
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in raw.splitlines() if line.strip()]
    services = parsed if isinstance(parsed, list) else [parsed]
    found: dict[str, dict[str, Any]] = {
        str(item.get("Service")): item
        for item in services
        if isinstance(item, dict) and item.get("Service")
    }
    for service in ("viewer", "nginx"):
        item = found.get(service)
        state = str(item.get("State", "")).lower() if item else ""
        health = str(item.get("Health", "")).lower() if item else ""
        if state != "running" or health != "healthy":
            raise DeploymentError(f"{service} did not become healthy")


def deploy(
    requested_sha: str,
    *,
    repo: Path = REPOSITORY_PATH,
    lock_path: Path = LOCK_PATH,
    ci_check: Callable[[str], None] = verify_successful_ci,
) -> None:
    requested_sha = validate_sha(requested_sha)
    assert_running_as_reolink()
    with acquire_lock(lock_path):
        preflight(repo, requested_sha)
        fetch_and_validate(repo, requested_sha)
        ensure_target_safe(repo, requested_sha)
        ci_check(requested_sha)
        update_checkout(repo, requested_sha)
        # Keep this as one build transaction.  If it fails, up is never run;
        # the checkout may already have advanced and is intentionally left so
        # the operator can inspect the failed build and retry the same SHA.
        run_checked(compose_command(repo, "build", "analyzer", "viewer"), cwd=repo, failure_label="docker build")
        run_checked(
            compose_command(repo, "up", "-d", "--wait", "--wait-timeout", "120", "viewer", "nginx"),
            cwd=repo,
            failure_label="docker up",
        )
        check_services(repo)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: reolink-deploy <40-character-main-sha>", file=sys.stderr)
        return 2
    try:
        deploy(argv[1])
    except DeploymentError as exc:
        print(f"deployment failed: {exc}", file=sys.stderr)
        return 1
    print(f"deployed {argv[1]}")
    print("built analyzer and viewer; started viewer and nginx; health checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
