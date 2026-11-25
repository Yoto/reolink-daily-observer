"""Safely expire analyzed source recordings from recognized date directories."""

from __future__ import annotations

import argparse
import calendar
import logging
import os
import stat
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.config import AppSettings, load_settings
from app.models import DailyReport, Event
from app.state import FileFingerprint, fingerprint_digest


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DayDirectory:
    day: date
    path: Path
    nested: bool


@dataclass(slots=True)
class RetentionResult:
    cutoff: date
    candidate_days: int = 0
    deleted_days: int = 0
    deleted_files: int = 0
    deleted_bytes: int = 0
    skipped_days: int = 0
    errors: int = 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reolink-daily retention",
        description="Delete analyzed camera recordings older than the retention period.",
    )
    parser.add_argument("--config", type=Path, help="YAML configuration path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report eligible recordings without deleting them",
    )
    args = parser.parse_args(argv)
    settings = _settings(args.config)
    if not settings.retention.enabled:
        LOGGER.info("retention disabled")
        return 0

    result = run_retention(settings, dry_run=args.dry_run)
    LOGGER.info(
        "retention complete dry_run=%s cutoff=%s candidate_days=%d "
        "deleted_days=%d deleted_files=%d deleted_bytes=%d skipped_days=%d errors=%d",
        args.dry_run,
        result.cutoff,
        result.candidate_days,
        result.deleted_days,
        result.deleted_files,
        result.deleted_bytes,
        result.skipped_days,
        result.errors,
    )
    return 1 if result.errors else 0


def run_retention(
    settings: AppSettings,
    *,
    dry_run: bool = False,
    today: date | None = None,
) -> RetentionResult:
    input_root = settings.paths.input.resolve(strict=True)
    current_day = today or datetime.now(ZoneInfo(settings.timezone)).date()
    cutoff = subtract_calendar_months(current_day, settings.retention.months)
    result = RetentionResult(cutoff=cutoff)

    for candidate in discover_day_directories(input_root):
        # Keep the entire cutoff date. At the daily run time, recordings from
        # later on that date have not yet reached the full retention period.
        if candidate.day >= cutoff:
            continue
        result.candidate_days += 1
        try:
            videos = list_video_files(candidate.path, settings.input.extensions)
            if not videos:
                continue
            if settings.retention.require_processed:
                reason = incomplete_processing_reason(
                    candidate.day,
                    videos,
                    input_root=input_root,
                    output_root=settings.paths.output,
                )
                if reason is not None:
                    result.skipped_days += 1
                    LOGGER.warning(
                        "retention skipped date=%s reason=%s", candidate.day, reason
                    )
                    continue

            LOGGER.info(
                "retention eligible date=%s files=%d bytes=%d dry_run=%s",
                candidate.day,
                len(videos),
                sum(video.stat(follow_symlinks=False).st_size for video in videos),
                dry_run,
            )

            deleted_for_day = 0
            for video in videos:
                try:
                    file_stat = video.stat(follow_symlinks=False)
                    if not stat.S_ISREG(file_stat.st_mode):
                        continue
                    if not dry_run:
                        video.unlink()
                    result.deleted_files += 1
                    result.deleted_bytes += file_stat.st_size
                    deleted_for_day += 1
                except OSError as exc:
                    result.errors += 1
                    LOGGER.error(
                        "retention delete failed date=%s file=%s error=%s",
                        candidate.day,
                        video.name,
                        exc,
                    )

            if deleted_for_day and not dry_run and _remove_empty_directory(candidate.path):
                result.deleted_days += 1
                if candidate.nested:
                    _remove_empty_ancestors(candidate.path.parent, input_root)
        except (OSError, ValueError, ValidationError) as exc:
            result.errors += 1
            LOGGER.error("retention failed date=%s error=%s", candidate.day, exc)

    return result


def subtract_calendar_months(value: date, months: int) -> date:
    if months < 1:
        raise ValueError("months must be positive")
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def discover_day_directories(input_root: Path) -> list[DayDirectory]:
    """Return only strict flat or nested date directories without following links."""

    discovered: list[DayDirectory] = []
    with os.scandir(input_root) as root_entries:
        for entry in root_entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            flat_day = _parse_date(entry.name)
            if flat_day is not None:
                discovered.append(DayDirectory(flat_day, Path(entry.path), nested=False))
                continue
            if len(entry.name) != 4 or not entry.name.isdecimal():
                continue
            year = int(entry.name)
            with os.scandir(entry.path) as month_entries:
                for month_entry in month_entries:
                    if (
                        not month_entry.is_dir(follow_symlinks=False)
                        or len(month_entry.name) != 2
                        or not month_entry.name.isdecimal()
                    ):
                        continue
                    month = int(month_entry.name)
                    with os.scandir(month_entry.path) as day_entries:
                        for day_entry in day_entries:
                            if (
                                not day_entry.is_dir(follow_symlinks=False)
                                or len(day_entry.name) != 2
                                or not day_entry.name.isdecimal()
                            ):
                                continue
                            try:
                                parsed = date(year, month, int(day_entry.name))
                            except ValueError:
                                continue
                            discovered.append(
                                DayDirectory(parsed, Path(day_entry.path), nested=True)
                            )
    return sorted(discovered, key=lambda item: (item.day, item.path.as_posix()))


def list_video_files(directory: Path, extensions: Sequence[str]) -> list[Path]:
    allowed = {extension.lower() for extension in extensions}
    videos: list[Path] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            if Path(entry.name).suffix.lower() in allowed:
                videos.append(Path(entry.path))
    return sorted(videos, key=lambda item: item.name.casefold())


def incomplete_processing_reason(
    day: date,
    videos: Sequence[Path],
    *,
    input_root: Path,
    output_root: Path,
) -> str | None:
    report_path = output_root / day.isoformat() / "daily_report.json"
    try:
        output_day = report_path.parent
        event_directory = output_day / "events"
        if output_day.is_symlink() or event_directory.is_symlink() or report_path.is_symlink():
            return "report artifacts must not be symlinks"
        report_stat = report_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(report_stat.st_mode):
            return "daily report is not a regular file"
        report = DailyReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        return f"daily report unavailable or invalid: {exc.__class__.__name__}"
    summary = report.processing_summary
    if report.date != day:
        return "daily report date does not match directory"
    if summary.failed_count or summary.unstable_skipped:
        return "daily report contains failed or unstable recordings"
    if summary.video_files != len(videos) or summary.event_json_count != len(videos):
        return "daily report recording count does not match current files"

    events_by_source: dict[str, Event] = {}
    try:
        with os.scandir(event_directory) as entries:
            for entry in entries:
                if (
                    not entry.is_file(follow_symlinks=False)
                    or Path(entry.name).suffix.lower() != ".json"
                ):
                    continue
                event = Event.model_validate_json(
                    Path(entry.path).read_text(encoding="utf-8")
                )
                if event.source_file in events_by_source:
                    return "duplicate event source file"
                events_by_source[event.source_file] = event
    except (OSError, ValidationError) as exc:
        return f"event metadata unavailable or invalid: {exc.__class__.__name__}"

    for video in videos:
        event = events_by_source.get(video.name)
        if event is None or event.processing.source_fingerprint is None:
            return "recording has no completed event metadata"
        fingerprint = FileFingerprint.from_path(video, input_root=input_root)
        if event.processing.source_fingerprint != fingerprint_digest(fingerprint):
            return "recording changed after event processing"
    return None


def _parse_date(value: str) -> date | None:
    if len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _remove_empty_directory(path: Path) -> bool:
    try:
        path.rmdir()
    except OSError:
        return False
    return True


def _remove_empty_ancestors(path: Path, input_root: Path) -> None:
    while path != input_root:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def _settings(explicit_path: Path | None) -> AppSettings:
    if explicit_path is not None:
        return load_settings(explicit_path)
    raw = os.getenv("CONFIG_PATH") or os.getenv("ANALYZER_CONFIG")
    return load_settings(Path(raw) if raw else None)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    raise SystemExit(main())
