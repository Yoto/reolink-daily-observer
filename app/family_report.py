"""Generate and persist the family-facing daily report."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from pydantic import Field, model_validator

from app.config import AppSettings
from app.genai.base import GenAIProvider
from app.io_utils import atomic_write_json
from app.models import (
    APIUsage,
    Event,
    Identifier,
    NonEmptyText,
    ProcessingSummary,
    ReportProcessingMetadata,
    SchemaModel,
    TimePeriodSummary,
    TriageSummary,
)
from app.triage import TriageOutcome


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAMILY_REPORT_SCHEMA_VERSION = "1.0"
FAMILY_REPORT_PROMPT_VERSION = "family_report_v1"
FAMILY_REPORT_PROMPT = Path("prompts/family_report_v1.txt")


class FamilyAttentionNarrative(SchemaModel):
    event_id: Identifier
    title: NonEmptyText
    reason: NonEmptyText


class FamilySceneNarrative(SchemaModel):
    event_id: Identifier
    description: NonEmptyText


class FamilyNarrative(SchemaModel):
    """Only fields whose wording/selection is entrusted to the text model."""

    overview: NonEmptyText = "一日の記録をまとめました。"
    attention_items: list[FamilyAttentionNarrative] = Field(default_factory=list)
    time_periods: list[TimePeriodSummary] = Field(default_factory=list)
    scenes: list[FamilySceneNarrative] = Field(default_factory=list)
    closing_comment: NonEmptyText = "今日の記録をまとめました。"


class FamilyAttentionItem(SchemaModel):
    event_id: Identifier
    recording_time: datetime | None = None
    source_file: NonEmptyText
    title: NonEmptyText
    reason: NonEmptyText


class FamilyScene(SchemaModel):
    event_id: Identifier
    recording_time: datetime | None = None
    source_file: NonEmptyText
    description: NonEmptyText


class FamilyReport(SchemaModel):
    schema_version: Identifier = FAMILY_REPORT_SCHEMA_VERSION
    date: date
    timezone: NonEmptyText = "Asia/Tokyo"
    title: NonEmptyText = "見守り日報"
    event_count: int = Field(ge=0)
    overview: NonEmptyText
    attention_items: list[FamilyAttentionItem] = Field(default_factory=list)
    time_periods: list[TimePeriodSummary] = Field(default_factory=list)
    scenes: list[FamilyScene] = Field(default_factory=list)
    closing_comment: NonEmptyText
    source_event_ids: list[Identifier] = Field(default_factory=list)
    processing_summary: ProcessingSummary
    triage_summary: TriageSummary = Field(default_factory=TriageSummary)
    processing: ReportProcessingMetadata

    @model_validator(mode="after")
    def validate_event_references(self) -> FamilyReport:
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("source_event_ids must be unique")
        if self.event_count != len(self.source_event_ids):
            raise ValueError("event_count must match source_event_ids")
        if self.event_count != self.processing_summary.event_json_count:
            raise ValueError("event_count must match processing_summary.event_json_count")
        known = set(self.source_event_ids)
        referenced = {item.event_id for item in self.attention_items}
        referenced.update(item.event_id for item in self.scenes)
        referenced.update(
            event_id for period in self.time_periods for event_id in period.event_ids
        )
        unknown = referenced - known
        if unknown:
            raise ValueError(f"family report references unknown event IDs: {sorted(unknown)}")
        attention_ids = [item.event_id for item in self.attention_items]
        if len(set(attention_ids)) != len(attention_ids):
            raise ValueError("attention_items must reference each event at most once")
        if self.triage_summary.attention_count != len(self.attention_items):
            raise ValueError("triage_summary.attention_count must match attention_items")
        return self


def generate_family_report(
    *,
    target_date: date,
    events: Sequence[Event],
    processing_summary: ProcessingSummary,
    settings: AppSettings,
    provider: GenAIProvider,
    triage: TriageOutcome,
    cache_path: Path,
    force: bool = False,
) -> FamilyReport:
    """Generate family wording from event JSON and triage judgements only."""

    started = time.perf_counter()
    prompt_template = _read_relative(FAMILY_REPORT_PROMPT)
    input_signature = _input_signature(
        target_date=target_date,
        events=events,
        settings=settings,
        provider=provider,
        prompt_template=prompt_template,
        triage=triage,
    )
    cached = _load_cache(
        cache_path=cache_path,
        input_signature=input_signature,
        provider=provider,
        force=force,
    )
    if cached is not None:
        return cached.model_copy(
            update={
                "processing": cached.processing.model_copy(update={"cache_reused": True})
            }
        )

    usage = APIUsage()
    report_provider = provider.name
    report_model = provider.model
    if events:
        prompt = prompt_template.format(
            date=target_date.isoformat(),
            timezone=settings.timezone,
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
                response_model=FamilyNarrative,
            )
            narrative = _canonicalize_narrative(result.value, events, triage)
            usage = result.usage
        except Exception as exc:
            LOGGER.exception(
                "family report GenAI generation failed; writing deterministic fallback"
            )
            failed_usage = getattr(exc, "usage", APIUsage())
            if isinstance(failed_usage, APIUsage):
                usage = failed_usage
            narrative = _fallback_narrative(events, triage)
            report_provider = "local-fallback"
            report_model = "deterministic-family-report"
    else:
        narrative = _fallback_narrative(events, triage)
        report_provider = "local-fallback"
        report_model = "deterministic-empty-family-report"

    by_id = {event.event_id: event for event in events}
    attention_items = [
        FamilyAttentionItem(
            event_id=item.event_id,
            recording_time=by_id[item.event_id].recording_start,
            source_file=by_id[item.event_id].source_file,
            title=item.title,
            reason=item.reason,
        )
        for item in narrative.attention_items
        if item.event_id in by_id
    ]
    scenes = [
        FamilyScene(
            event_id=item.event_id,
            recording_time=by_id[item.event_id].recording_start,
            source_file=by_id[item.event_id].source_file,
            description=item.description,
        )
        for item in narrative.scenes
        if item.event_id in by_id
    ]
    report = FamilyReport(
        date=target_date,
        timezone=settings.timezone,
        event_count=len(events),
        overview=narrative.overview,
        attention_items=attention_items,
        time_periods=narrative.time_periods,
        scenes=scenes,
        closing_comment=narrative.closing_comment,
        source_event_ids=[event.event_id for event in events],
        processing_summary=processing_summary,
        triage_summary=triage.summary,
        processing=ReportProcessingMetadata(
            provider=report_provider,
            model=report_model,
            prompt_version=FAMILY_REPORT_PROMPT_VERSION,
            schema_version=FAMILY_REPORT_SCHEMA_VERSION,
            input_signature=input_signature,
            cache_reused=False,
            processing_time_sec=time.perf_counter() - started,
            usage=usage,
            completed_at=datetime.now(timezone.utc),
        ),
    )
    atomic_write_json(cache_path, report)
    return report


def _load_cache(
    *,
    cache_path: Path,
    input_signature: str,
    provider: GenAIProvider,
    force: bool,
) -> FamilyReport | None:
    if force or not cache_path.is_file():
        return None
    try:
        report = FamilyReport.model_validate_json(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOGGER.warning("family report cache is unreadable; regenerating: %s", exc)
        return None
    processing = report.processing
    if (
        processing.provider == "local-fallback"
        or processing.provider != provider.name
        or processing.model != provider.model
        or processing.prompt_version != FAMILY_REPORT_PROMPT_VERSION
        or processing.schema_version != FAMILY_REPORT_SCHEMA_VERSION
        or processing.input_signature != input_signature
    ):
        return None
    LOGGER.info("reusing family report cache signature=%s", input_signature)
    return report


def _input_signature(
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
        "prompt_version": FAMILY_REPORT_PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(prompt_template.encode("utf-8")).hexdigest(),
        "schema_version": FAMILY_REPORT_SCHEMA_VERSION,
        "max_output_tokens": settings.genai.max_output_tokens,
        "language": settings.report.language,
        "events": [event.model_dump(mode="json") for event in events],
        "triage": {
            "summary": triage.summary.model_dump(mode="json", exclude={"usage"}),
            "items": [item.model_dump(mode="json") for item in triage.attention_items],
            "day_notes": list(triage.day_notes),
        },
    }
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _canonicalize_narrative(
    narrative: FamilyNarrative,
    events: Sequence[Event],
    triage: TriageOutcome,
) -> FamilyNarrative:
    by_id = {event.event_id: event for event in events}
    attention_by_id = {item.event_id: item for item in narrative.attention_items}
    canonical_attention: list[FamilyAttentionNarrative] = []
    for judgement in triage.attention_items:
        if judgement.event_id not in by_id:
            continue
        generated = attention_by_id.get(judgement.event_id)
        canonical_attention.append(
            generated
            if generated is not None
            else FamilyAttentionNarrative(
                event_id=judgement.event_id,
                title=judgement.notable or "確認してほしい動画",
                reason=judgement.assessment,
            )
        )

    seen: set[str] = set()
    canonical_scenes: list[FamilySceneNarrative] = []
    for item in narrative.scenes:
        if item.event_id not in by_id or item.event_id in seen:
            continue
        seen.add(item.event_id)
        canonical_scenes.append(item)
        if len(canonical_scenes) == 3:
            break

    periods = [
        period.model_copy(
            update={
                "event_ids": [event_id for event_id in period.event_ids if event_id in by_id]
            }
        )
        for period in narrative.time_periods
    ]
    return narrative.model_copy(
        update={
            "attention_items": canonical_attention,
            "time_periods": periods,
            "scenes": canonical_scenes,
        }
    )


def _fallback_narrative(
    events: Sequence[Event], triage: TriageOutcome
) -> FamilyNarrative:
    if not events:
        overview = "この日は日報にまとめられる記録がありませんでした。"
        closing = "記録がある日は、ここに一日の様子をまとめます。"
    else:
        overview = f"この日は{len(events)}件の記録がありました。"
        closing = "確認できた記録をまとめています。"
    attention = [
        FamilyAttentionNarrative(
            event_id=item.event_id,
            title=item.notable or "確認してほしい動画",
            reason=item.assessment,
        )
        for item in triage.attention_items
    ]
    attention_ids = {item.event_id for item in triage.attention_items}
    scenes = [
        FamilySceneNarrative(event_id=event.event_id, description=event.summary)
        for event in events
        if event.event_id not in attention_ids
    ][:3]
    return FamilyNarrative(
        overview=overview,
        attention_items=attention,
        time_periods=[],
        scenes=scenes,
        closing_comment=closing,
    )


def _triage_prompt_block(triage: TriageOutcome) -> str:
    if not triage.all_items:
        if triage.summary.failed:
            return "（triage処理が失敗したため判定結果はありません。）"
        return "（triage処理は実行されませんでした。）"
    return json.dumps(
        {
            "attention_event_ids": [item.event_id for item in triage.attention_items],
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


def _read_relative(path: Path) -> str:
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read prompt {resolved}: {exc}") from exc
