"""Generate and persist one canonical daily report from event JSON data."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from pydantic import Field

from app.config import AppSettings
from app.genai.base import GenAIProvider
from app.io_utils import atomic_write_json
from app.models import (
    APIUsage,
    DailyReport,
    Event,
    FailedEvent,
    NonEmptyText,
    ProcessingSummary,
    ReportProcessingMetadata,
    RepresentativeEvent,
    SCHEMA_VERSION,
    SchemaModel,
    TimePeriodSummary,
)
from app.triage import TriageOutcome


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class DailyNarrative(SchemaModel):
    """Only the narrative fields entrusted to the text model."""

    overview: NonEmptyText
    time_periods: list[TimePeriodSummary] = Field(default_factory=list)
    recurring_patterns: list[NonEmptyText] = Field(default_factory=list)
    representative_events: list[RepresentativeEvent] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DailyRunStats:
    detected_files: int
    cache_reused: int = 0
    failed: tuple[FailedEvent, ...] = ()
    unstable_skipped: int = 0


def generate_daily_report(
    *,
    target_date: date,
    events: Sequence[Event],
    stats: DailyRunStats,
    settings: AppSettings,
    provider: GenAIProvider,
    triage: TriageOutcome | None = None,
    cache_path: Path | None = None,
    force: bool = False,
) -> DailyReport:
    """Send only canonical event JSON to the provider, never images or MP4s."""

    started = time.perf_counter()
    triage = triage or TriageOutcome()
    usage = APIUsage()
    report_provider = provider.name
    report_model = provider.model
    prompt_template = _read_relative(settings.prompts.daily_report)
    input_signature = _report_input_signature(
        target_date=target_date,
        events=events,
        settings=settings,
        provider=provider,
        prompt_template=prompt_template,
        triage=triage,
    )
    cached_processing: ReportProcessingMetadata | None = None

    if events:
        cached_report = _load_report_cache(
            cache_path=cache_path,
            input_signature=input_signature,
            provider=provider,
            prompt_version=settings.prompts.daily_report_version,
            force=force,
        )
        if cached_report is not None:
            narrative = _canonicalize_narrative(
                DailyNarrative(
                    overview=cached_report.overview,
                    time_periods=cached_report.time_periods,
                    recurring_patterns=cached_report.recurring_patterns,
                    representative_events=cached_report.representative_events,
                ),
                events,
            )
            cached_processing = cached_report.processing.model_copy(
                update={"cache_reused": True}
            )
            usage = cached_processing.usage
            report_provider = cached_processing.provider
            report_model = cached_processing.model
            LOGGER.info("reusing daily narrative cache signature=%s", input_signature)
        else:
            prompt = prompt_template.format(
                date=target_date.isoformat(),
                timezone=settings.timezone,
                success_count=len(events),
                attention_count=len(triage.attention_items),
                triage_json=_triage_prompt_block(triage),
                events_json=json.dumps(
                    [event.model_dump(mode="json") for event in events],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            try:
                result = provider.generate_structured(
                    prompt=prompt,
                    response_model=DailyNarrative,
                )
                narrative = _canonicalize_narrative(result.value, events)
                usage = result.usage
            except Exception as exc:
                LOGGER.exception(
                    "daily report GenAI generation failed; writing deterministic fallback"
                )
                failed_usage = getattr(exc, "usage", APIUsage())
                if isinstance(failed_usage, APIUsage):
                    usage = failed_usage
                narrative = _fallback_narrative(events)
                report_provider = "local-fallback"
                report_model = "deterministic-event-index"
    else:
        narrative = _fallback_narrative(events)
        report_provider = "local-fallback"
        report_model = "deterministic-empty-report"

    source_ids = [event.event_id for event in events]
    processing_summary = ProcessingSummary(
        video_files=stats.detected_files,
        event_json_count=len(events),
        cache_reused=stats.cache_reused,
        failed_count=len(stats.failed),
        unstable_skipped=stats.unstable_skipped,
        failures=list(stats.failed),
    )
    return DailyReport(
        schema_version=SCHEMA_VERSION,
        date=target_date,
        timezone=settings.timezone,
        title="防犯カメラ日報",
        event_count=len(events),
        overview=narrative.overview,
        attention_items=list(triage.attention_items),
        day_notes=list(triage.day_notes),
        time_periods=narrative.time_periods,
        recurring_patterns=narrative.recurring_patterns,
        entity_totals=_entity_totals(events),
        representative_events=narrative.representative_events,
        source_event_ids=source_ids,
        processing_summary=processing_summary,
        triage_summary=triage.summary,
        processing=(
            cached_processing
            if cached_processing is not None
            else ReportProcessingMetadata(
                provider=report_provider,
                model=report_model,
                prompt_version=settings.prompts.daily_report_version,
                schema_version=SCHEMA_VERSION,
                input_signature=input_signature,
                cache_reused=False,
                processing_time_sec=time.perf_counter() - started,
                usage=usage,
                completed_at=datetime.now(timezone.utc),
            )
        ),
    )


def _load_report_cache(
    *,
    cache_path: Path | None,
    input_signature: str,
    provider: GenAIProvider,
    prompt_version: str,
    force: bool,
) -> DailyReport | None:
    if force or cache_path is None or not cache_path.is_file():
        return None
    try:
        report = DailyReport.model_validate_json(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.warning("daily report cache is unreadable; regenerating: %s", exc)
        return None
    processing = report.processing
    if (
        processing.provider == "local-fallback"
        or processing.provider != provider.name
        or processing.model != provider.model
        or processing.prompt_version != prompt_version
        or processing.schema_version != SCHEMA_VERSION
        or processing.input_signature != input_signature
    ):
        return None
    return report


def _report_input_signature(
    *,
    target_date: date,
    events: Sequence[Event],
    settings: AppSettings,
    provider: GenAIProvider,
    prompt_template: str,
    triage: TriageOutcome,
) -> str:
    value = {
        "date": target_date.isoformat(),
        "timezone": settings.timezone,
        "provider": provider.name,
        "model": provider.model,
        "prompt_version": settings.prompts.daily_report_version,
        "prompt_sha256": hashlib.sha256(prompt_template.encode("utf-8")).hexdigest(),
        "schema_version": SCHEMA_VERSION,
        "max_output_tokens": settings.genai.max_output_tokens,
        "language": settings.report.language,
        "events": [event.model_dump(mode="json") for event in events],
        "triage": {
            "summary": triage.summary.model_dump(mode="json", exclude={"usage"}),
            "items": [
                item.model_dump(mode="json") for item in triage.attention_items
            ],
            "day_notes": list(triage.day_notes),
        },
    }
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def write_daily_report(
    report: DailyReport,
    *,
    output_directory: Path,
) -> Path:
    """Persist the JSON contract consumed by the cache, history, and viewer."""

    output_directory.mkdir(parents=True, exist_ok=True)
    target = output_directory / "daily_report.json"
    atomic_write_json(target, report)
    return target


def _canonicalize_narrative(
    narrative: DailyNarrative, events: Sequence[Event]
) -> DailyNarrative:
    """Prevent a report response from changing source identity or timestamp."""

    by_id = {event.event_id: event for event in events}
    representative: list[RepresentativeEvent] = []
    seen: set[str] = set()
    for item in narrative.representative_events:
        source = by_id.get(item.event_id)
        if source is None or item.event_id in seen:
            continue
        seen.add(item.event_id)
        representative.append(
            RepresentativeEvent(
                event_id=source.event_id,
                recording_time=source.recording_start,
                source_file=source.source_file,
                description=item.description,
            )
        )

    periods: list[TimePeriodSummary] = []
    for period in narrative.time_periods:
        periods.append(
            period.model_copy(
                update={
                    "event_ids": [
                        event_id for event_id in period.event_ids if event_id in by_id
                    ]
                }
            )
        )
    return narrative.model_copy(
        update={"representative_events": representative, "time_periods": periods}
    )


def _fallback_narrative(events: Sequence[Event]) -> DailyNarrative:
    if not events:
        overview = "解析対象として完了したイベントはありませんでした。"
    else:
        overview = (
            f"{len(events)}件のevent JSONを集約しました。"
            "日報本文のGenAI生成を利用できなかったため、個別の観察記録はeventsディレクトリで確認してください。"
        )
    return DailyNarrative(
        overview=overview,
        time_periods=[],
        recurring_patterns=[],
        representative_events=[],
    )


def _triage_prompt_block(triage: TriageOutcome) -> str:
    """Give the narrative model every judgement, not only the flagged ones."""

    if not triage.all_items:
        if triage.summary.failed:
            return "（triage処理が失敗したため判定結果はありません。）"
        return "（triage処理は実行されませんでした。）"
    return json.dumps(
        {
            "score_threshold": triage.summary.score_threshold,
            "attention_event_ids": [
                item.event_id for item in triage.attention_items
            ],
            "day_notes": list(triage.day_notes),
            "items": [
                item.model_dump(
                    mode="json",
                    include={
                        "event_id",
                        "assessment",
                        "person_type",
                        "routine_explanation",
                        "occurrence_id",
                        "notable",
                        "anomaly_score",
                    },
                )
                for item in triage.all_items
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _entity_totals(events: Sequence[Event]) -> dict[str, int]:
    totals: defaultdict[str, int] = defaultdict(int)
    for event in events:
        for entity in event.entities:
            totals[entity.type] += entity.count
    return dict(sorted(totals.items()))


def _read_relative(path: Path) -> str:
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read prompt {resolved}: {exc}") from exc
