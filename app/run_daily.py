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
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.config import AppSettings, load_settings
from app.daily_report import DailyRunStats, generate_daily_report, render_daily_report
from app.event_analyzer import (
    EVENT_ANALYSIS_PIPELINE_VERSION,
    EventAnalyzer,
    PreparedEvent,
)
from app.genai import create_provider
from app.genai.base import BatchOutcome, BatchRequest, GenAIProvider, failure_usage
from app.io_utils import atomic_write_json
from app.models import APIUsage, DailyReport, Event, FailedEvent, SCHEMA_VERSION
from app.state import CacheKey, FileFingerprint, ProcessingClaim, StateStore
from app.triage import HistoryEntry, TriageOutcome, run_triage
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


@dataclass(slots=True)
class _Pending:
    """One clip prepared for a deferred batch, waiting for its result."""

    position: int
    video: Path
    prepared: PreparedEvent
    claim: ProcessingClaim
    event_path: Path
    recording_start: datetime | None
    started: float


class _UnstableInput(Exception):
    """The source file changed, vanished, or is still being uploaded."""


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
    parser.add_argument(
        "--sync",
        action="store_true",
        help="analyze events with immediate requests instead of the batch queue",
    )
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
            batched = (
                settings.genai.batch.enabled
                and provider.supports_batch
                and not args.sync
            )
            LOGGER.info(
                "event stage mode=%s provider=%s clips=%d",
                "batch" if batched else "sync",
                provider.name,
                len(videos),
            )
            run_day = _process_day_batched if batched else _process_day
            results = run_day(
                videos=videos,
                target_date=target_date,
                event_directory=event_directory,
                settings=settings,
                provider=provider,
                analyzer=analyzer,
                state=state,
                force=args.force,
            )

        triage = run_triage(
            target_date=target_date,
            events=results.events,
            settings=settings,
            provider=provider,
            history=_load_history(
                output_root=settings.paths.output,
                target_date=target_date,
                days=settings.triage.history_days,
            ),
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
            triage=triage,
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
        run_usage = results.new_usage.plus(report_run_usage).plus(triage.summary.usage)
        LOGGER.info(
            "daily complete target_date=%s success=%d cached=%d unstable=%d failed=%d "
            "triage_evaluated=%d attention=%d "
            "requests=%d input_tokens=%s output_tokens=%s total_tokens=%s "
            "estimated_cost=%s report_cached=%s report_provider=%s report_model=%s "
            "daily_report_processing_sec=%.3f artifacts=%s",
            target_date,
            len(results.events),
            results.cached,
            results.unstable,
            len(results.failures),
            triage.summary.evaluated_count,
            triage.summary.attention_count,
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
        degraded = report_fallback or triage.summary.failed
        return 2 if results.failures or results.unstable or degraded else 0
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
    """Analyze the day one clip at a time, each with immediate requests."""

    results = DayResults(detected=len(videos))
    snapshots = _stability_snapshots(videos, settings)

    for position, video in enumerate(videos, start=1):
        recording_start = None
        try:
            recording_start = parse_recording_time(video.name, settings.timezone)
            previous_snapshot = _require_stable(video, settings, snapshots)
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
            _log_event(
                position=position,
                total=len(videos),
                event=event,
                video=video,
                cached=cached,
                elapsed=time.perf_counter() - event_started,
            )
        except _UnstableInput:
            results.unstable += 1
            LOGGER.warning(
                "skipping unstable/empty file file=%s stable_seconds=%d",
                video.name,
                settings.input.stable_seconds,
            )
        except Exception as exc:
            _record_failure(
                results, exc, video=video, recording_start=recording_start
            )
            LOGGER.exception(
                "event failed index=%d/%d file=%s", position, len(videos), video.name
            )
            if not settings.processing.continue_on_error:
                raise
    _sort_events(results.events)
    return results


def _process_day_batched(
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
    """Prepare every clip, submit one deferred batch, then complete them all.

    The clips of a day are independent and the run already targets yesterday,
    so the bulk queue costs the schedule nothing and halves the bill. The price
    is that frames for every uncached clip are extracted before anything is
    sent, and stay on disk until the batch has been serialized.

    A batch that never finishes cannot be resumed by a later run: the batch id
    exists only in this process and in the log. That is why every submission is
    logged with its id before anything else can fail.
    """

    results = DayResults(detected=len(videos))
    snapshots = _stability_snapshots(videos, settings)
    pending: list[_Pending] = []
    try:
        for position, video in enumerate(videos, start=1):
            recording_start = None
            claim: ProcessingClaim | None = None
            try:
                recording_start = parse_recording_time(video.name, settings.timezone)
                previous_snapshot = _require_stable(video, settings, snapshots)
                fingerprint = FileFingerprint.from_path(
                    video, input_root=settings.paths.input
                )
                event_id = _event_id(target_date, fingerprint.source_path)
                started = time.perf_counter()
                key = _cache_key(fingerprint, settings, provider)
                resolved = _resolve_or_claim(
                    video=video,
                    event_id=event_id,
                    key=key,
                    fingerprint=fingerprint,
                    settings=settings,
                    state=state,
                    force=force,
                )
                if isinstance(resolved, Event):
                    results.events.append(resolved)
                    results.cached += 1
                    _log_event(
                        position=position,
                        total=len(videos),
                        event=resolved,
                        video=video,
                        cached=True,
                        elapsed=time.perf_counter() - started,
                    )
                    continue
                claim = resolved
                prepared = analyzer.prepare_file(
                    video,
                    event_id=event_id,
                    source_file=video.name,
                    recording_start=recording_start,
                    analysis_signature=key.prompt_version,
                    source_fingerprint=_fingerprint_digest(fingerprint),
                    expected_snapshot=previous_snapshot,
                )
                pending.append(
                    _Pending(
                        position=position,
                        video=video,
                        prepared=prepared,
                        claim=claim,
                        event_path=event_directory / f"event_{event_id}.json",
                        recording_start=recording_start,
                        started=started,
                    )
                )
            except _UnstableInput:
                results.unstable += 1
                LOGGER.warning(
                    "skipping unstable/empty file file=%s stable_seconds=%d",
                    video.name,
                    settings.input.stable_seconds,
                )
            except Exception as exc:
                _mark_failed(state, claim, exc, recording_start)
                _record_failure(
                    results, exc, video=video, recording_start=recording_start
                )
                LOGGER.exception(
                    "event preparation failed index=%d/%d file=%s",
                    position,
                    len(videos),
                    video.name,
                )
                if not settings.processing.continue_on_error:
                    raise

        outcomes = _submit_batch(
            pending, provider=provider, settings=settings, state=state, results=results
        )
        for item in pending:
            # The frames are inside the request bodies now; nothing below reads
            # them, and a day of JPEGs is not worth holding through synthesis.
            item.prepared.release()
        # A batch that could not be produced at all has already failed every
        # clip in it, so there is nothing left to complete.
        for item in pending if outcomes is not None else ():
            try:
                event = analyzer.complete(item.prepared, outcomes)
                _store_event(
                    event,
                    claim=item.claim,
                    event_path=item.event_path,
                    settings=settings,
                    state=state,
                )
            except Exception as exc:
                _mark_failed(state, item.claim, exc, item.recording_start)
                _record_failure(
                    results,
                    exc,
                    video=item.video,
                    recording_start=item.recording_start,
                )
                LOGGER.exception(
                    "event failed index=%d/%d file=%s",
                    item.position,
                    len(videos),
                    item.video.name,
                )
                if not settings.processing.continue_on_error:
                    raise
                continue
            results.events.append(event)
            results.new_usage = results.new_usage.plus(event.processing.usage)
            _log_event(
                position=item.position,
                total=len(videos),
                event=event,
                video=item.video,
                cached=False,
                elapsed=time.perf_counter() - item.started,
            )
    finally:
        for item in pending:
            item.prepared.release()
    _sort_events(results.events)
    return results


def _submit_batch(
    pending: Sequence[_Pending],
    *,
    provider: GenAIProvider,
    settings: AppSettings,
    state: StateStore,
    results: DayResults,
) -> dict[str, BatchOutcome[Any]] | None:
    """Send every prepared chunk as one batch and wait for the whole day.

    Returns ``None`` when the batch could not be produced at all, which fails
    every clip in it; a batch that merely contains failed requests returns
    normally and is reported per clip.
    """

    if not pending:
        return {}
    requests: list[BatchRequest[Any]] = [
        request for item in pending for request in item.prepared.requests
    ]
    LOGGER.info(
        "submitting event batch clips=%d requests=%d frame_bytes=%d",
        len(pending),
        len(requests),
        sum(item.prepared.frame_bytes for item in pending),
    )
    try:
        return provider.generate_structured_batch(requests)
    except Exception as exc:
        LOGGER.exception("event batch failed clips=%d", len(pending))
        for item in pending:
            _mark_failed(state, item.claim, exc, item.recording_start)
            _record_failure(
                results, exc, video=item.video, recording_start=item.recording_start
            )
        if not settings.processing.continue_on_error:
            raise
        return None


def _stability_snapshots(
    videos: Sequence[Path], settings: AppSettings
) -> dict[Path, FileSnapshot | None]:
    """Snapshot every file once, then wait, so a single pause covers them all."""

    snapshots: dict[Path, FileSnapshot | None] = {}
    for video in videos:
        try:
            snapshots[video] = snapshot_file(video)
        except OSError:
            snapshots[video] = None
    if videos and settings.input.stability_recheck_sec:
        LOGGER.info(
            "verifying upload stability files=%d recheck_sec=%.1f stable_seconds=%d",
            len(videos),
            settings.input.stability_recheck_sec,
            settings.input.stable_seconds,
        )
        time.sleep(settings.input.stability_recheck_sec)
    return snapshots


def _require_stable(
    video: Path,
    settings: AppSettings,
    snapshots: dict[Path, FileSnapshot | None],
) -> FileSnapshot:
    previous_snapshot = snapshots.get(video)
    if previous_snapshot is None or not file_is_stable(
        video, settings.input.stable_seconds, previous_snapshot=previous_snapshot
    ):
        raise _UnstableInput(video.name)
    return previous_snapshot


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
    key = _cache_key(fingerprint, settings, provider)
    resolved = _resolve_or_claim(
        video=video,
        event_id=event_id,
        key=key,
        fingerprint=fingerprint,
        settings=settings,
        state=state,
        force=force,
    )
    if isinstance(resolved, Event):
        return resolved, True
    claim = resolved

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
        _store_event(
            event,
            claim=claim,
            event_path=event_path,
            settings=settings,
            state=state,
        )
        return event, False
    except Exception as exc:
        _mark_failed(state, claim, exc, recording_start)
        raise


def _cache_key(
    fingerprint: FileFingerprint, settings: AppSettings, provider: GenAIProvider
) -> CacheKey:
    return CacheKey(
        fingerprint=fingerprint,
        provider=provider.name,
        model=provider.model,
        prompt_version=_cache_prompt_version(settings),
        schema_version=SCHEMA_VERSION,
    )


def _resolve_or_claim(
    *,
    video: Path,
    event_id: str,
    key: CacheKey,
    fingerprint: FileFingerprint,
    settings: AppSettings,
    state: StateStore,
    force: bool,
) -> Event | ProcessingClaim:
    """Return a reusable event, or the claim that authorizes reprocessing."""

    cache = None if force else state.get_cached_record(key)
    if cache is not None and cache.event_json_path:
        try:
            return _load_cached_event(
                cache.event_json_path,
                event_id=event_id,
                source_file=video.name,
                key=key,
                fingerprint=fingerprint,
                settings=settings,
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
            return _load_cached_event(
                claim.record.event_json_path,
                event_id=event_id,
                source_file=video.name,
                key=key,
                fingerprint=fingerprint,
                settings=settings,
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
    return claim


def _store_event(
    event: Event,
    *,
    claim: ProcessingClaim,
    event_path: Path,
    settings: AppSettings,
    state: StateStore,
) -> None:
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


def _mark_failed(
    state: StateStore,
    claim: ProcessingClaim | None,
    exc: BaseException,
    recording_start: datetime | None,
) -> None:
    if claim is None:
        return
    state.mark_failed(
        claim.record.id,
        error=exc,
        usage=failure_usage(exc),
        recording_time=recording_start,
    )


def _record_failure(
    results: DayResults,
    exc: BaseException,
    *,
    video: Path,
    recording_start: datetime | None,
) -> None:
    results.new_usage = results.new_usage.plus(failure_usage(exc))
    results.failures.append(
        FailedEvent(
            source_file=video.name,
            recording_time=recording_start,
            error=_brief_error(exc),
        )
    )


def _log_event(
    *,
    position: int,
    total: int,
    event: Event,
    video: Path,
    cached: bool,
    elapsed: float,
) -> None:
    LOGGER.info(
        "event complete index=%d/%d event_id=%s file=%s duration_sec=%.3f "
        "frames=%d chunks=%d cached=%s provider=%s model=%s requests=%d "
        "processing_sec=%.3f",
        position,
        total,
        event.event_id,
        video.name,
        event.duration_sec,
        event.processing.frames_analyzed,
        event.processing.chunk_count,
        cached,
        event.processing.provider,
        event.processing.model,
        0 if cached else event.processing.usage.request_count,
        elapsed,
    )


def _sort_events(events: list[Event]) -> None:
    events.sort(
        key=lambda event: (
            event.recording_start is None,
            event.recording_start or datetime.max.replace(tzinfo=ZoneInfo("UTC")),
            event.source_file,
        )
    )


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


def _load_history(
    *, output_root: Path, target_date: date, days: int
) -> list[HistoryEntry]:
    """Read prior daily reports as a compact baseline for novelty judgement.

    Only the narrative digest of each day is used. Full event records would
    dominate the prompt without adding much signal, and a missing or unreadable
    day is simply skipped so a first run still works.
    """

    if days <= 0:
        return []
    entries: list[HistoryEntry] = []
    for offset in range(1, days + 1):
        day = target_date - timedelta(days=offset)
        path = output_root / day.isoformat() / "daily_report.json"
        if not path.is_file():
            continue
        try:
            report = DailyReport.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            LOGGER.debug("skipping unreadable history for %s: %s", day, exc)
            continue
        entries.append(
            HistoryEntry(
                date=day.isoformat(),
                overview=report.overview,
                recurring_patterns=tuple(report.recurring_patterns),
                attention_notes=tuple(
                    item.notable for item in report.attention_items if item.notable
                ),
            )
        )
    entries.reverse()
    return entries


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
        "resolution_reduction": settings.frames.resolution_reduction.model_dump(
            mode="json"
        ),
        "jpeg_quality": settings.frames.jpeg_quality,
        "max_images": settings.genai.max_images_per_request,
        "max_inline_bytes": settings.genai.max_inline_image_bytes,
        "overlap": settings.genai.chunk_overlap_frames,
        "max_output_tokens": settings.genai.max_output_tokens,
        "timezone": settings.timezone,
        # The scene description is part of the observation prompt, so a changed
        # household description must invalidate cached event JSON.
        "scene": settings.scene.prompt_payload(),
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
