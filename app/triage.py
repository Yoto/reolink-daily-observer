"""Judgement pass that turns objective event records into things to check.

Observation and judgement are deliberately separate stages. Event JSON stays a
neutral record of what the camera saw, and this module decides which of those
records a resident should actually look at. Because the input is text only,
triage can be re-run against a changed scene description or a changed threshold
without sending a single image back to the provider.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as Date
from pathlib import Path
from typing import Sequence

from app.config import AppSettings
from app.genai.base import GenAIProvider
from app.models import (
    APIUsage,
    AttentionItem,
    Event,
    PersonType,
    TriageResult,
    TriageSummary,
)


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """A compact digest of one previous day, used as a novelty baseline."""

    date: str
    overview: str
    recurring_patterns: tuple[str, ...] = ()
    attention_notes: tuple[str, ...] = ()

    def payload(self) -> dict[str, object]:
        value: dict[str, object] = {"date": self.date, "overview": self.overview}
        if self.recurring_patterns:
            value["recurring_patterns"] = list(self.recurring_patterns)
        if self.attention_notes:
            value["attention_notes"] = list(self.attention_notes)
        return value


@dataclass(slots=True)
class TriageOutcome:
    """Filtered attention items plus the deterministic accounting for them."""

    attention_items: list[AttentionItem] = field(default_factory=list)
    day_notes: list[str] = field(default_factory=list)
    all_items: list[AttentionItem] = field(default_factory=list)
    summary: TriageSummary = field(default_factory=TriageSummary)


def run_triage(
    *,
    target_date: Date,
    events: Sequence[Event],
    settings: AppSettings,
    provider: GenAIProvider,
    history: Sequence[HistoryEntry] = (),
) -> TriageOutcome:
    """Score every event, then keep the ones worth a human's attention."""

    threshold = settings.triage.attention_score_threshold
    if not settings.triage.enabled or not events:
        return TriageOutcome(
            summary=TriageSummary(
                enabled=settings.triage.enabled,
                evaluated_count=0,
                attention_count=0,
                score_threshold=threshold,
            )
        )

    prompt_template = _read_relative(settings.prompts.triage)
    prompt = prompt_template.format(
        date=target_date.isoformat(),
        timezone=settings.timezone,
        event_count=len(events),
        scene_json=_scene_block(settings),
        history_json=_history_block(history),
        events_json=json.dumps(
            [_triage_event_payload(event) for event in events],
            ensure_ascii=False,
            indent=2,
        ),
    )

    started = time.perf_counter()
    try:
        result = provider.generate_structured(
            prompt=prompt, response_model=TriageResult
        )
    except Exception as exc:  # noqa: BLE001 - the day must still produce a report
        LOGGER.exception("triage failed; the report will be produced without it")
        failed_usage = getattr(exc, "usage", APIUsage())
        if not isinstance(failed_usage, APIUsage):
            failed_usage = APIUsage()
        return TriageOutcome(
            summary=TriageSummary(
                enabled=True,
                provider=provider.name,
                model=provider.model,
                prompt_version=settings.prompts.triage_version,
                evaluated_count=0,
                attention_count=0,
                score_threshold=threshold,
                usage=failed_usage,
                failed=True,
            )
        )

    triage_result = result.value
    items = _canonicalize_items(triage_result.items, events)
    attention = _select_attention(items, settings)
    LOGGER.info(
        "triage complete evaluated=%d attention=%d threshold=%d elapsed_sec=%.2f",
        len(items),
        len(attention),
        threshold,
        time.perf_counter() - started,
    )
    return TriageOutcome(
        attention_items=attention,
        day_notes=[note for note in triage_result.day_notes if note.strip()],
        all_items=items,
        summary=TriageSummary(
            enabled=True,
            provider=provider.name,
            model=provider.model,
            prompt_version=settings.prompts.triage_version,
            evaluated_count=len(items),
            attention_count=len(attention),
            score_threshold=threshold,
            person_type_totals=_person_type_totals(items),
            usage=result.usage,
            failed=False,
        ),
    )


def _canonicalize_items(
    items: Sequence[AttentionItem], events: Sequence[Event]
) -> list[AttentionItem]:
    """Bind each judgement to a real event and restore trusted local fields."""

    by_id = {event.event_id: event for event in events}
    canonical: list[AttentionItem] = []
    seen: set[str] = set()
    for item in items:
        source = by_id.get(item.event_id)
        if source is None:
            LOGGER.warning("triage returned unknown event_id=%s", item.event_id)
            continue
        if item.event_id in seen:
            continue
        seen.add(item.event_id)
        notable = item.notable.strip() if item.notable else None
        canonical.append(
            item.model_copy(
                update={
                    "notable": notable or None,
                    # Identity and timestamps come from the event record, never
                    # from the response, so a report cannot mislabel a file.
                    "recording_time": source.recording_start,
                    "source_file": source.source_file,
                }
            )
        )
    missing = set(by_id) - seen
    if missing:
        LOGGER.warning(
            "triage did not evaluate %d event(s): %s",
            len(missing),
            ", ".join(sorted(missing)),
        )
    return canonical


def _select_attention(
    items: Sequence[AttentionItem], settings: AppSettings
) -> list[AttentionItem]:
    threshold = settings.triage.attention_score_threshold
    selected = [
        item
        for item in items
        if item.anomaly_score >= threshold
        or (settings.triage.notable_always_attention and item.notable)
    ]
    selected.sort(
        key=lambda item: (
            -item.anomaly_score,
            item.recording_time.timestamp() if item.recording_time else 0.0,
            item.event_id,
        )
    )
    return selected[: settings.triage.max_attention_items]


def _person_type_totals(items: Sequence[AttentionItem]) -> dict[str, int]:
    totals: defaultdict[str, int] = defaultdict(int)
    for item in items:
        value = item.person_type
        totals[value.value if isinstance(value, PersonType) else str(value)] += 1
    return dict(sorted(totals.items()))


def _triage_event_payload(event: Event) -> dict[str, object]:
    """Send the observation content only; frames and usage add no signal."""

    return {
        "event_id": event.event_id,
        "source_file": event.source_file,
        "recording_start_local": (
            event.recording_start.isoformat() if event.recording_start else None
        ),
        "duration_sec": event.duration_sec,
        "summary": event.summary,
        "entities": [
            entity.model_dump(mode="json", exclude_none=True)
            for entity in event.entities
        ],
        "observations": [
            observation.model_dump(mode="json", exclude_none=True)
            for observation in event.observations
        ],
        "interactions": [
            interaction.model_dump(mode="json", exclude_none=True)
            for interaction in event.interactions
        ],
        "uncertainties": list(event.uncertainties),
    }


def _scene_block(settings: AppSettings) -> str:
    payload = settings.scene.prompt_payload()
    if not payload:
        return (
            "（撮影場所の説明は設定されていません。"
            "平常時の生活パターンが不明なため、住人と来訪者の区別は観察記録のみから慎重に判断してください。）"
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _history_block(history: Sequence[HistoryEntry]) -> str:
    if not history:
        return "（過去の日報がありません。単日の内容だけで判断してください。）"
    return json.dumps(
        [entry.payload() for entry in history], ensure_ascii=False, indent=2
    )


def _read_relative(path: Path) -> str:
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read prompt {resolved}: {exc}") from exc


__all__ = ["HistoryEntry", "TriageOutcome", "run_triage"]
