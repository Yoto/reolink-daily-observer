from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

from app.config import AppSettings
from app.io_utils import atomic_write_json
from app.models import (
    DailyReport,
    Event,
    Observation,
    ProcessingMetadata,
    ProcessingSummary,
    ReportProcessingMetadata,
    VideoMetadata,
)
from app.retention import run_retention, subtract_calendar_months
from app.state import FileFingerprint, fingerprint_digest


def _settings(tmp_path: Path, *, require_processed: bool = True) -> AppSettings:
    input_root = tmp_path / "input"
    output_root = tmp_path / "output"
    input_root.mkdir()
    output_root.mkdir()
    return AppSettings.model_validate(
        {
            "paths": {
                "input": input_root,
                "output": output_root,
                "state": tmp_path / "state",
                "temp": tmp_path / "temp",
            },
            "retention": {
                "enabled": True,
                "months": 3,
                "require_processed": require_processed,
            },
        }
    )


def _write_completed_video(
    settings: AppSettings,
    day: date,
    *,
    flat: bool = False,
    name: str = "camera.mp4",
) -> Path:
    day_directory = (
        settings.paths.input / day.isoformat()
        if flat
        else settings.paths.input
        / f"{day.year:04d}"
        / f"{day.month:02d}"
        / f"{day.day:02d}"
    )
    day_directory.mkdir(parents=True)
    video = day_directory / name
    video.write_bytes(b"camera recording")
    fingerprint = FileFingerprint.from_path(video, input_root=settings.paths.input)
    event_id = f"{day:%Y%m%d}-abcdef123456"
    started = datetime.combine(
        day,
        datetime.min.time(),
        tzinfo=ZoneInfo("Asia/Tokyo"),
    )
    event = Event(
        event_id=event_id,
        source_file=name,
        recording_start=started,
        time_confidence="filename",
        duration_sec=2,
        video_metadata=VideoMetadata(width=1280, height=720, fps=25, codec="h264"),
        observations=[
            Observation(start_sec=0, end_sec=1, description="人物が移動した。")
        ],
        summary="人物が移動した。",
        processing=ProcessingMetadata(
            provider="mock",
            model="mock-v1",
            prompt_version="event_observation_v2",
            source_fingerprint=fingerprint_digest(fingerprint),
            frame_interval_sec=1,
            frames_analyzed=2,
        ),
    )
    output_directory = settings.paths.output / day.isoformat()
    atomic_write_json(output_directory / "events" / f"event_{event_id}.json", event)
    report = DailyReport(
        date=day,
        event_count=1,
        overview="一件の録画を処理した。",
        source_event_ids=[event_id],
        processing_summary=ProcessingSummary(video_files=1, event_json_count=1),
        processing=ReportProcessingMetadata(
            provider="mock",
            model="mock-v1",
            prompt_version="daily_report_v2",
        ),
    )
    atomic_write_json(output_directory / "daily_report.json", report)
    return video


def test_retention_deletes_only_fully_elapsed_processed_days(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    expired = _write_completed_video(settings, date(2026, 8, 25))
    cutoff_day = _write_completed_video(settings, date(2026, 8, 26))
    recent = _write_completed_video(settings, date(2026, 11, 25))

    result = run_retention(settings, today=date(2026, 11, 26))

    assert result.cutoff == date(2026, 8, 26)
    assert result.deleted_files == 1
    assert result.deleted_days == 1
    assert result.errors == 0
    assert not expired.exists()
    assert cutoff_day.exists()
    assert recent.exists()


def test_retention_dry_run_supports_flat_layout_without_mutation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    expired = _write_completed_video(settings, date(2026, 7, 1), flat=True)

    result = run_retention(
        settings,
        today=date(2026, 11, 26),
        dry_run=True,
    )

    assert result.deleted_files == 1
    assert result.deleted_bytes == len(b"camera recording")
    assert result.deleted_days == 0
    assert expired.exists()


def test_retention_skips_missing_report_and_changed_recording(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    changed = _write_completed_video(settings, date(2026, 7, 1))
    changed.write_bytes(b"replacement recording with different metadata")
    missing_report_day = settings.paths.input / "2026" / "07" / "02"
    missing_report_day.mkdir(parents=True)
    unprocessed = missing_report_day / "unprocessed.mp4"
    unprocessed.write_bytes(b"not processed")

    result = run_retention(settings, today=date(2026, 11, 26))

    assert result.deleted_files == 0
    assert result.skipped_days == 2
    assert result.errors == 0
    assert changed.exists()
    assert unprocessed.exists()


def test_retention_ignores_symlinks_and_non_video_files(tmp_path: Path) -> None:
    settings = _settings(tmp_path, require_processed=False)
    day_directory = settings.paths.input / "2026" / "07" / "01"
    day_directory.mkdir(parents=True)
    note = day_directory / "keep.txt"
    note.write_text("keep", encoding="utf-8")
    external = tmp_path / "external.mp4"
    external.write_bytes(b"outside")
    (day_directory / "linked.mp4").symlink_to(external)
    video = day_directory / "delete.mp4"
    video.write_bytes(b"delete")

    result = run_retention(settings, today=date(2026, 11, 26))

    assert result.deleted_files == 1
    assert not video.exists()
    assert external.exists()
    assert note.exists()
    assert day_directory.exists()


def test_subtract_calendar_months_clamps_month_end() -> None:
    assert subtract_calendar_months(date(2026, 5, 31), 3) == date(2026, 2, 28)
    assert subtract_calendar_months(date(2024, 5, 31), 3) == date(2024, 2, 29)


def test_scheduled_wrapper_runs_retention_after_degraded_analysis(
    tmp_path: Path,
) -> None:
    fake_docker = tmp_path / "docker"
    call_log = tmp_path / "calls"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$*" in
  *"run --rm analyzer"*) exit 2 ;;
  *"run --rm retention retention"*) exit 0 ;;
esac
exit 99
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("", encoding="utf-8")
    (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    script = Path(__file__).parents[1] / "scripts" / "run-scheduled.sh"
    completed = subprocess.run(
        [script],
        env={
            **os.environ,
            "CALL_LOG": str(call_log),
            "REOLINK_DOCKER_BIN": str(fake_docker),
            "REOLINK_PROJECT_DIR": str(project),
        },
        check=False,
    )

    assert completed.returncode == 2
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert calls[0].endswith("run --rm analyzer")
    assert calls[1].endswith("run --rm retention retention")


def test_scheduled_wrapper_skips_retention_after_fatal_analysis(
    tmp_path: Path,
) -> None:
    fake_docker = tmp_path / "docker"
    call_log = tmp_path / "calls"
    fake_docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CALL_LOG"
exit 1
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("", encoding="utf-8")
    (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    script = Path(__file__).parents[1] / "scripts" / "run-scheduled.sh"
    completed = subprocess.run(
        [script],
        env={
            **os.environ,
            "CALL_LOG": str(call_log),
            "REOLINK_DOCKER_BIN": str(fake_docker),
            "REOLINK_PROJECT_DIR": str(project),
        },
        check=False,
    )

    assert completed.returncode == 1
    assert len(call_log.read_text(encoding="utf-8").splitlines()) == 1
