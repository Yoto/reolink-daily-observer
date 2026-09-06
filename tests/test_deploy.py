from __future__ import annotations

from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "reolink_deploy", Path(__file__).parents[1] / "deploy" / "reolink-deploy.py"
)
assert _SPEC and _SPEC.loader
deploy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deploy)


def test_validate_sha_rejects_runner_input() -> None:
    with pytest.raises(deploy.DeploymentError, match="exactly 40"):
        deploy.validate_sha("git rev-parse HEAD")
    with pytest.raises(deploy.DeploymentError):
        deploy.validate_sha("A" * 40)
    assert deploy.validate_sha("a" * 40) == "a" * 40


def test_ci_gate_requires_exact_successful_push_run(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "workflow_runs": [
                        {"head_sha": "a" * 40, "head_branch": "main", "event": "pull_request", "status": "completed", "conclusion": "success"},
                        {"head_sha": "b" * 40, "head_branch": "main", "event": "push", "status": "completed", "conclusion": "failure"},
                        {"head_sha": "c" * 40, "head_branch": "main", "event": "push", "status": "completed", "conclusion": "success"},
                    ]
                }
            ).encode()

    monkeypatch.setattr(deploy.urllib.request, "build_opener", lambda *_args, **_kwargs: SimpleNamespace(open=lambda *_a, **_k: Response()))
    deploy.verify_successful_ci("c" * 40)
    with pytest.raises(deploy.DeploymentError, match="no successful"):
        deploy.verify_successful_ci("a" * 40)


def test_build_failure_never_starts_services(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> str:
        calls.append(argv)
        if "build" in argv:
            raise deploy.DeploymentError("docker build failed (exit 1)")
        return ""

    monkeypatch.setattr(deploy, "assert_running_as_reolink", lambda: None)
    monkeypatch.setattr(deploy, "acquire_lock", lambda _path: nullcontext())
    monkeypatch.setattr(deploy, "preflight", lambda *_args: None)
    monkeypatch.setattr(deploy, "fetch_and_validate", lambda *_args: None)
    monkeypatch.setattr(deploy, "ensure_target_safe", lambda *_args: None)
    monkeypatch.setattr(deploy, "update_checkout", lambda *_args: None)
    monkeypatch.setattr(deploy, "check_services", lambda *_args: None)
    monkeypatch.setattr(deploy, "run_checked", fake_run)

    with pytest.raises(deploy.DeploymentError, match="build failed"):
        deploy.deploy("a" * 40, repo=tmp_path, lock_path=tmp_path / ".deploy.lock", ci_check=lambda _: None)

    assert any("build" in call for call in calls)
    assert not any("up" in call for call in calls)


def test_preflight_rejects_dirty_or_non_main_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(argv: list[str], *, allow_output: bool = False, **_: object) -> str:
        if "config" in argv:
            return deploy.REPOSITORY_URL
        if "symbolic-ref" in argv:
            return "feature/cd"
        if "status" in argv:
            return " M tracked-file"
        return ""

    monkeypatch.setattr(deploy, "run_checked", fake_run)
    with pytest.raises(deploy.DeploymentError, match="not on main"):
        deploy.preflight(tmp_path, "a" * 40)

    def main_run(argv: list[str], *, allow_output: bool = False, **_: object) -> str:
        if "config" in argv:
            return deploy.REPOSITORY_URL
        if "symbolic-ref" in argv:
            return "main"
        if "status" in argv:
            return " M tracked-file"
        return ""

    monkeypatch.setattr(deploy, "run_checked", main_run)
    with pytest.raises(deploy.DeploymentError, match="tracked changes"):
        deploy.preflight(tmp_path, "a" * 40)


def test_stale_fetched_sha_is_rejected_before_fast_forward(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(argv: list[str], *, allow_output: bool = False, **_: object) -> str:
        if "rev-parse" in argv:
            return "b" * 40
        return ""

    monkeypatch.setattr(deploy, "run_checked", fake_run)
    with pytest.raises(deploy.DeploymentError, match="current fetched main"):
        deploy.fetch_and_validate(tmp_path, "a" * 40)


def test_ci_failure_prevents_checkout_update_and_docker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    updated = False

    monkeypatch.setattr(deploy, "assert_running_as_reolink", lambda: None)
    monkeypatch.setattr(deploy, "acquire_lock", lambda _path: nullcontext())
    monkeypatch.setattr(deploy, "preflight", lambda *_args: None)
    monkeypatch.setattr(deploy, "fetch_and_validate", lambda *_args: None)
    monkeypatch.setattr(deploy, "ensure_target_safe", lambda *_args: None)

    def fake_update(*_args: object) -> None:
        nonlocal updated
        updated = True

    monkeypatch.setattr(deploy, "update_checkout", fake_update)
    monkeypatch.setattr(deploy, "run_checked", lambda argv, **_: calls.append(argv) or "")

    def failed_ci(_: str) -> None:
        raise deploy.DeploymentError("no successful completed test.yml push run exists for this commit")

    with pytest.raises(deploy.DeploymentError, match="no successful"):
        deploy.deploy("a" * 40, repo=tmp_path, lock_path=tmp_path / ".deploy.lock", ci_check=failed_ci)
    assert not updated
    assert calls == []


def test_effective_user_must_be_reolink(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deploy.os, "geteuid", lambda: 10005)
    monkeypatch.setattr(deploy.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_name="gha-deploy"))
    with pytest.raises(deploy.DeploymentError, match="reolink user"):
        deploy.assert_running_as_reolink()
