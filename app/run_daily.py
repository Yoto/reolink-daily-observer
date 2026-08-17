"""One-shot CLI for a single video or a complete camera day."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.config import AppSettings, load_settings
from app.daily_report import DailyRunStats, generate_daily_report, render_daily_report
from app.event_analyzer import EVENT_ANALYSIS_PIPELINE_VERSION, EventAnalyzer
from app.genai import create_provider
from app.genai.base import GenAIProvider
from app.io_utils import atomic_write_json
from app.models import APIUsage, Event, FailedEvent, SCHEMA_VERSION
from app.state import CacheKey, FileFingerprint, StateStore
from app.video import (
    FRAME_EXTRACTION_VERSION,
    FileSnapshot,
    file_is_stable,
    parse_recording_time,
    snapshot_file,
)


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DayResults:
    detected: int = 0
    cached: int = 0
    unstable: int = 0
    events: list[Event] = field(default_factory=list)
    failures: list[FailedEvent] = field(default_factory=list)
    new_usage: APIUsage = field(default_factory=APIUsage)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    _configure_logging()
    try:
        if arguments and arguments[0] == "analyze-video":
            return _single_command(arguments[1:])
        return _daily_command(arguments)
    except KeyboardInterrupt:
        LOGGER.error("interrupted")
        return 130
    except Exception:
        LOGGER.exception("fatal analyzer error")
        return 1


def _daily_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="reolink-daily",
        description="Analyze one date of camera MP4 files and write a daily report.",
    )
    parser.add_argument("--date", type=_iso_date, help="target date (YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="ignore event cache")
    parser.add_argument("--config", type=Path, help="YAML configuration path")
    args = parser.parse_args(argv)

    settings = _settings(args.config)
    target_date = args.date or _previous_day(settings.timezone)
    day_directory = _find_day_directory(settings.paths.input, target_date, settings)
    videos = _list_videos(day_directory, settings)
    LOGGER.info(
        "daily run target_date=%s timezone=%s input=%s detected_mp4=%d force=%s",
        target_date,
        settings.timezone,
        day_directory,
        len(videos),
        args.force,
    )

    settings.paths.output.mkdir(parents=True, exist_ok=True)
    settings.paths.state.mkdir(parents=True, exist_ok=True)
    output_directory = settings.paths.output / target_date.isoformat()
    event_directory = output_directory / "events"
    event_directory.mkdir(parents=True, exist_ok=True)

    provider = create_provider(settings)
    try:
        analyzer = EventAnalyzer(settings, provider)
        with StateStore(
            settings.paths.state_database, output_root=settings.paths.output
        ) as state:
            results = _process_day(
                videos=videos,
                target_date=target_date,
                event_directory=event_directory,
                settings=settings,
                provider=provider,
                analyzer=analyzer,
                state=state,
                force=args.force,
            )

        report_started = time.perf_counter()
        report = generate_daily_report(
            target_date=target_date,
            events=results.events,
            stats=DailyRunStats(
                detected_files=results.detected,
                cache_reused=results.cached,
                failed=tuple(results.failures),
                unstable_skipped=results.unstable,
            ),
            settings=settings,
            provider=provider,
            cache_path=output_directory / "daily_report.json",
            force=args.force,
        )
        artifacts = render_daily_report(
            report, output_directory=output_directory, settings=settings
        )
        report_run_sec = time.perf_counter() - report_started
        report_run_usage = (
            APIUsage() if report.processing.cache_reused else report.processing.usage
        )
        run_usage = results.new_usage.plus(report_run_usage)
        LOGGER.info(
            "daily complete target_date=%s success=%d cached=%d unstable=%d failed=%d "
            "requests=%d input_tokens=%s output_tokens=%s total_tokens=%s "
            "estimated_cost=%s report_cached=%s report_provider=%s report_model=%s "
            "daily_report_processing_sec=%.3f artifacts=%s",
            target_date,
            len(results.events),
            results.cached,
            results.unstable,
            len(results.failures),
            run_usage.request_count,
            run_usage.input_tokens,
            run_usage.output_tokens,
            run_usage.total_tokens,
            run_usage.estimated_cost,
            report.processing.cache_reused,
            report.processing.provider,
            report.processing.model,
            report_run_sec,
            ",".join(str(path) for path in artifacts.values()),
        )
        report_fallback = report.processing.provider == "local-fallback" and bool(
            results.events
        )
        return 2 if results.failures or results.unstable or report_fallback else 0
    finally:
        provider.close()


def _single_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="reolink-daily analyze-video",
        description="Analyze one MP4 for prompt/quality evaluation.",
    )
    parser.add_argument("video", type=Path)
    parser.add_argument("--force", action="store_true", help="ignore event cache")
    parser.add_argument("--config", type=Path, help="YAML configuration path")
    args = parser.parse_args(argv)
    settings = _settings(args.config)
    video = args.video.resolve(strict=True)
    root = settings.paths.input.resolve(strict=True)
    fingerprint = FileFingerprint.from_path(video, input_root=root)
    recording_start = parse_recording_time(video.name, settings.timezone)
    target_date = (
        recording_start.date()
        if recording_start
        else datetime.now(ZoneInfo(settings.timezone)).date()
    )
    event_id = _event_id(target_date, fingerprint.source_path)
    event_directory = settings.paths.output / target_date.isoformat() / "events"
    event_directory.mkdir(parents=True, exist_ok=True)

    provider = create_provider(settings)
    try:
        analyzer = EventAnalyzer(settings, provider)
        with StateStore(
            settings.paths.state_database, output_root=settings.paths.output
        ) as state:
            event, cached = _process_one(
                video=video,
                event_id=event_id,
                event_path=event_directory / f"event_{event_id}.json",
                fingerprint=fingerprint,
                recording_start=recording_start,
                settings=settings,
                provider=provider,
                analyzer=analyzer,
                state=state,
                force=args.force,
                expected_snapshot=None,
            )
        LOGGER.info(
            "single video complete event_id=%s cached=%s output=%s frames=%d requests=%d",
            event.event_id,
            cached,
            event_directory / f"event_{event_id}.json",
            event.processing.frames_analyzed,
            0 if cached else event.processing.usage.request_count,
        )
        print(event_directory / f"event_{event_id}.json")
        return 0
    finally:
        provider.close()


def _process_day(
    *,
    videos: Sequence[Path],
    target_date: date,
    event_directory: Path,
    settings: AppSettings,
    provider: GenAIProvider,
    analyzer: EventAnalyzer,
    state: StateStore,
    force: bool,
) -> DayResults:
    results = DayResults(detected=len(videos))
    initial_snapshots = {}
    for video in videos:
        try:
            initial_snapshots[video] = snapshot_file(video)
        except OSError:
            initial_snapshots[video] = None
    if videos and settings.input.stability_recheck_sec:
        LOGGER.info(
            "verifying upload stability files=%d recheck_sec=%.1f stable_seconds=%d",
            len(videos),
            settings.input.stability_recheck_sec,
            settings.input.stable_seconds,
        )
        time.sleep(settings.input.stability_recheck_sec)

    for position, video in enumerate(videos, start=1):
        recording_start = None
        try:
            recording_start = parse_recording_time(video.name, settings.timezone)
            previous_snapshot = initial_snapshots.get(video)
            if previous_snapshot is None or not file_is_stable(
                video,
                settings.input.stable_seconds,
                previous_snapshot=previous_snapshot,
            ):
                results.unstable += 1
                LOGGER.warning(
                    "skipping unstable/empty file file=%s stable_seconds=%d",
                    video.name,
                    settings.input.stable_seconds,
                )
                continue
            fingerprint = FileFingerprint.from_path(
                video, input_root=settings.paths.input
            )
            event_id = _event_id(target_date, fingerprint.source_path)
            event_path = event_directory / f"event_{event_id}.json"
            event_started = time.perf_counter()
            event, cached = _process_one(
                video=video,
                event_id=event_id,
                event_path=event_path,
                fingerprint=fingerprint,
                recording_start=recording_start,
                settings=settings,
                provider=provider,
                analyzer=analyzer,
                state=state,
                force=force,
                expected_snapshot=previous_snapshot,
            )
            results.events.append(event)
            if cached:
                results.cached += 1
            else:
                results.new_usage = results.new_usage.plus(event.processing.usage)
            LOGGER.info(
                "event complete index=%d/%d event_id=%s file=%s duration_sec=%.3f "
                "frames=%d chunks=%d cached=%s provider=%s model=%s requests=%d "
                "processing_sec=%.3f",
                position,
                len(videos),
                event.event_id,
                video.name,
                event.duration_sec,
                event.processing.frames_analyzed,
                event.processing.chunk_count,
                cached,
                event.processing.provider,
                event.processing.model,
                0 if cached else event.processing.usage.request_count,
                time.perf_counter() - event_started,
            )
        except Exception as exc:
            failed_usage = getattr(exc, "usage", APIUsage())
            if isinstance(failed_usage, APIUsage):
                results.new_usage = results.new_usage.plus(failed_usage)
            error_text = _brief_error(exc)
            results.failures.append(
                FailedEvent(
                    source_file=video.name,
                    recording_time=recording_start,
                    error=error_text,
                )
            )
            LOGGER.exception(
                "event failed index=%d/%d file=%s", position, len(videos), video.name
            )
            if not settings.processing.continue_on_error:
                raise
    results.events.sort(
        key=lambda event: (
            event.recording_start is None,
            event.recording_start or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
            event.source_file,
        )
    )
    return results


def _process_one(
    *,
    video: Path,
    event_id: str,
    event_path: Path,
    fingerprint: FileFingerprint,
    recording_start: datetime | None,
    settings: AppSettings,
    provider: GenAIProvider,
    analyzer: EventAnalyzer,
    state: StateStore,
    force: bool,
    expected_snapshot: FileSnapshot | None,
) -> tuple[Event, bool]:
    key = CacheKey(
        fingerprint=fingerprint,
        provider=provider.name,
        model=provider.model,
        prompt_version=_cache_prompt_version(settings),
        schema_version=SCHEMA_VERSION,
    )

    cache = None if force else state.get_cached_record(key)
    if cache is not None and cache.event_json_path:
        try:
            return (
                _load_cached_event(
                    cache.event_json_path,
                    event_id=event_id,
                    source_file=video.name,
                    key=key,
                    fingerprint=fingerprint,
                    settings=settings,
                ),
                True,
            )
        except (OSError, ValidationError, ValueError):
            LOGGER.warning(
                "cached event is unreadable or does not match its cache key; reprocessing file=%s",
                video.name,
            )
            force = True

    claim = state.claim_processing(
        key,
        event_id=event_id,
        force=force,
        require_event_file=True,
    )
    if not claim.should_process and claim.record.event_json_path:
        try:
            return (
                _load_cached_event(
                    claim.record.event_json_path,
                    event_id=event_id,
                    source_file=video.name,
                    key=key,
                    fingerprint=fingerprint,
                    settings=settings,
                ),
                True,
            )
        except (OSError, ValidationError, ValueError):
            LOGGER.warning(
                "claimed cache artifact does not match its key; reprocessing file=%s",
                video.name,
            )
            claim = state.claim_processing(
                key,
                event_id=event_id,
                force=True,
                require_event_file=True,
            )

    try:
        event = analyzer.analyze_file(
            video,
            event_id=event_id,
            source_file=video.name,
            recording_start=recording_start,
            analysis_signature=key.prompt_version,
            source_fingerprint=_fingerprint_digest(fingerprint),
            expected_snapshot=expected_snapshot,
        )
        atomic_write_json(event_path, event)
        try:
            stored_path = event_path.relative_to(settings.paths.output)
        except ValueError:
            stored_path = event_path
        state.mark_completed(
            claim.record.id,
            event_json_path=stored_path,
            usage=event.processing.usage,
            recording_time=event.recording_start,
            event_id=event.event_id,
        )
        return event, False
    except Exception as exc:
        state.mark_failed(
            claim.record.id,
            error=exc,
            usage=(
                getattr(exc, "usage", None)
                if isinstance(getattr(exc, "usage", None), APIUsage)
                else None
            ),
            recording_time=recording_start,
        )
        raise


def _load_cached_event(
    stored_path: str,
    *,
    event_id: str,
    source_file: str,
    key: CacheKey,
    fingerprint: FileFingerprint,
    settings: AppSettings,
) -> Event:
    path = Path(stored_path)
    if not path.is_absolute():
        path = settings.paths.output / path
    event = Event.model_validate_json(path.read_text(encoding="utf-8"))
    expected = {
        "event_id": event_id,
        "source_file": source_file,
        "provider": key.provider,
        "model": key.model,
        "schema_version": key.schema_version,
        "analysis_signature": key.prompt_version,
        "source_fingerprint": _fingerprint_digest(fingerprint),
    }
    actual = {
        "event_id": event.event_id,
        "source_file": event.source_file,
        "provider": event.processing.provider,
        "model": event.processing.model,
        "schema_version": event.schema_version,
        "analysis_signature": event.processing.analysis_signature,
        "source_fingerprint": event.processing.source_fingerprint,
    }
    if actual != expected or event.processing.schema_version != key.schema_version:
        raise ValueError("cached event metadata does not match requested analysis key")
    return event


def _find_day_directory(
    input_root: Path, target_date: date, settings: AppSettings
) -> Path:
    candidates = [
        input_root / f"{target_date.year:04d}" / f"{target_date.month:02d}" / f"{target_date.day:02d}",
        input_root / target_date.isoformat(),
    ]
    existing = [candidate for candidate in candidates if candidate.is_dir()]
    for candidate in existing:
        if _list_videos(candidate, settings):
            return candidate
    if existing:
        return existing[0]
    raise FileNotFoundError(
        "target date directory not found; checked: "
        + ", ".join(str(candidate) for candidate in candidates)
    )


def _list_videos(directory: Path, settings: AppSettings) -> list[Path]:
    extensions = set(settings.input.extensions)
    return sorted(
        (
            item
            for item in directory.iterdir()
            if item.is_file() and item.suffix.lower() in extensions
        ),
        key=lambda item: item.name.casefold(),
    )


def _cache_prompt_version(settings: AppSettings) -> str:
    semantic_inputs = {
        "event_prompt": settings.prompts.event_version,
        "synthesis_prompt": settings.prompts.event_synthesis_version,
        "event_prompt_sha256": _configured_file_digest(settings.prompts.event),
        "synthesis_prompt_sha256": _configured_file_digest(
            settings.prompts.event_synthesis
        ),
        "event_pipeline_version": EVENT_ANALYSIS_PIPELINE_VERSION,
        "frame_extraction_version": FRAME_EXTRACTION_VERSION,
        "interval": settings.frames.interval_sec,
        "max_edge": settings.frames.max_long_edge_px,
        "jpeg_quality": settings.frames.jpeg_quality,
        "max_images": settings.genai.max_images_per_request,
        "max_inline_bytes": settings.genai.max_inline_image_bytes,
        "overlap": settings.genai.chunk_overlap_frames,
        "max_output_tokens": settings.genai.max_output_tokens,
        "timezone": settings.timezone,
    }
    digest = hashlib.sha256(
        json.dumps(semantic_inputs, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return f"{settings.prompts.event_version}-{digest}"


def _configured_file_digest(path: Path) -> str:
    resolved = path if path.is_absolute() else Path(__file__).resolve().parent.parent / path
    return hashlib.sha256(resolved.read_bytes()).hexdigest()


def _event_id(target_date: date, source_path: str) -> str:
    digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:12]
    return f"{target_date:%Y%m%d}-{digest}"


def _fingerprint_digest(fingerprint: FileFingerprint) -> str:
    value = {
        "source_path": fingerprint.source_path,
        "size_bytes": fingerprint.size_bytes,
        "mtime_ns": fingerprint.mtime_ns,
        "content_hash": fingerprint.content_hash,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _settings(explicit_path: Path | None) -> AppSettings:
    path = explicit_path
    if path is None:
        raw = os.getenv("CONFIG_PATH") or os.getenv("ANALYZER_CONFIG")
        path = Path(raw) if raw else None
    return load_settings(path)


def _previous_day(timezone_name: str) -> date:
    return (datetime.now(ZoneInfo(timezone_name)) - timedelta(days=1)).date()


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _brief_error(exc: BaseException) -> str:
    return " ".join((str(exc) or exc.__class__.__name__).split())[:1000]


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
