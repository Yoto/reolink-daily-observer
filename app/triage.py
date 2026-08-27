Failed to create stream fd: Operation not permitted
Failed to create stream fd: Operation not permitted
Failed to create stream fd: Operation not permitted
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
    prompt = _triage_prompt(
        prompt_template=prompt_template,
        target_date=target_date,
        events=events,
        settings=settings,
        history=history,
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
    usage = result.usage
    missing_before_retry = _missing_event_ids(triage_result.items, events)
    for attempt in range(1, settings.triage.missing_retry_attempts + 1):
        if not missing_before_retry:
            break
        retry_events = [
            event for event in events if event.event_id in missing_before_retry
        ]
        LOGGER.warning(
            "retrying %d missing triage judgement(s) attempt=%d/%d",
            len(retry_events),
            attempt,
            settings.triage.missing_retry_attempts,
        )
        retry_prompt = _triage_prompt(
            prompt_template=prompt_template,
            target_date=target_date,
            events=retry_events,
            settings=settings,
            history=history,
        )
        try:
            retry_result = provider.generate_structured(
                prompt=retry_prompt, response_model=TriageResult
            )
        except Exception as exc:  # noqa: BLE001 - preserve the first response
            LOGGER.exception(
                "missing triage judgement retry failed attempt=%d/%d",
                attempt,
                settings.triage.missing_retry_attempts,
            )
            failed_usage = getattr(exc, "usage", APIUsage())
            if isinstance(failed_usage, APIUsage):
                usage = usage.plus(failed_usage)
            break
        usage = usage.plus(retry_result.usage)
        triage_result = triage_result.model_copy(
            update={
                "items": [*triage_result.items, *retry_result.value.items],
                "day_notes": [
                    *triage_result.day_notes,
                    *retry_result.value.day_notes,
                ],
            }
        )
        missing_before_retry = _missing_event_ids(triage_result.items, events)
    items, capped, missing_ids = _canonicalize_items(
        triage_result.items, events, settings
    )
    grouped_items, folded = _group_occurrences(items, settings)
    attention = _select_attention(
        grouped_items, settings, required_event_ids=missing_ids
    )
    missing = len(missing_ids)
    LOGGER.info(
        "triage complete evaluated=%d attention=%d grouped=%d "
        "routine_score_capped=%d missing=%d threshold=%d elapsed_sec=%.2f",
        len(items) - missing,
        len(attention),
        folded,
        capped,
        missing,
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
            evaluated_count=len(items) - missing,
            attention_count=len(attention),
            missing_count=missing,
            grouped_count=folded,
            routine_explained_count=capped,
            score_threshold=threshold,
            person_type_totals=_person_type_totals(items),
            usage=usage,
            failed=missing > 0,
        ),
    )


def _triage_prompt(
    *,
    prompt_template: str,
    target_date: Date,
    events: Sequence[Event],
    settings: AppSettings,
    history: Sequence[HistoryEntry],
) -> str:
    return prompt_template.format(
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


def _missing_event_ids(
    items: Sequence[AttentionItem], events: Sequence[Event]
) -> set[str]:
    expected = {event.event_id for event in events}
    returned = {item.event_id for item in items if item.event_id in expected}
    return expected - returned


def _canonicalize_items(
    items: Sequence[AttentionItem], events: Sequence[Event], settings: AppSettings
) -> tuple[list[AttentionItem], int, set[str]]:
    """Bind each judgement to a real event and restore trusted local fields.

    Returns the canonical items, the number whose score was capped because a
    documented routine explained them, and the IDs missing from the model
    response. Missing judgements become deterministic attention items so an
    event can never silently disappear from the report. Their IDs are returned
    so the normal display limit cannot hide them.
    """

    by_id = {event.event_id: event for event in events}
    canonical: list[AttentionItem] = []
    seen: set[str] = set()
    capped = 0
    cap = settings.triage.routine_explained_score_cap
    for item in items:
        source = by_id.get(item.event_id)
        if source is None:
            LOGGER.warning("triage returned unknown event_id=%s", item.event_id)
            continue
        if item.event_id in seen:
            continue
        seen.add(item.event_id)
        notable = item.notable.strip() if item.notable else None
        routine = item.routine_explanation.strip() if item.routine_explanation else None
        score = item.anomaly_score
        # An event with an ordinary explanation and nothing flagged should not
        # reach the attention threshold on the strength of unresolved detail.
        if routine and not notable and score > cap:
            LOGGER.debug(
                "capping routine-explained event_id=%s score %d -> %d",
                item.event_id,
                score,
                cap,
            )
            score = cap
            capped += 1
        canonical.append(
            item.model_copy(
                update={
                    "notable": notable or None,
                    "routine_explanation": routine or None,
                    "anomaly_score": score,
                    # Identity and timestamps come from the event record, never
                    # from the response, so a report cannot mislabel a file.
                    "recording_time": source.recording_start,
                    "source_file": source.source_file,
                    "related_event_ids": [],
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
        for event_id in sorted(missing):
            source = by_id[event_id]
            canonical.append(
                AttentionItem(
                    event_id=event_id,
                    assessment=(
                        "triage応答にこのイベントの判定が含まれなかったため、"
                        "内容を確認する必要がある。"
                    ),
                    person_type=PersonType.UNKNOWN,
                    notable="triage判定結果が欠落したため未評価",
                    anomaly_score=settings.triage.attention_score_threshold,
                    recording_time=source.recording_start,
                    source_file=source.source_file,
                )
            )
    return canonical, capped, missing


def _group_occurrences(
    items: Sequence[AttentionItem], settings: AppSettings
) -> tuple[list[AttentionItem], int]:
    """Collapse events sharing an occurrence into their most notable member.

    One visit can span several clips. Reporting each clip separately inflates
    the count without telling the resident anything new, so the highest-scoring
    member represents the occurrence and the rest become related events.
    """

    if not settings.triage.group_related_events:
        return list(items), 0

    groups: dict[str, list[AttentionItem]] = defaultdict(list)
    ungrouped: list[AttentionItem] = []
    for item in items:
        if item.occurrence_id:
            groups[item.occurrence_id].append(item)
        else:
            ungrouped.append(item)

    representatives: list[AttentionItem] = []
    folded = 0
    for members in groups.values():
        if len(members) == 1:
            representatives.append(members[0])
            continue
        ordered = sorted(
            members,
            key=lambda item: (
                -item.anomaly_score,
                item.notable is None,
                item.recording_time.timestamp() if item.recording_time else 0.0,
            ),
        )
        leader, rest = ordered[0], ordered[1:]
        folded += len(rest)
        representatives.append(
            leader.model_copy(
                update={
                    "related_event_ids": sorted(item.event_id for item in rest),
                }
            )
        )
    return representatives + ungrouped, folded


def _select_attention(
    items: Sequence[AttentionItem],
    settings: AppSettings,
    *,
    required_event_ids: set[str] | frozenset[str] = frozenset(),
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
    required = [item for item in selected if item.event_id in required_event_ids]
    ordinary = [item for item in selected if item.event_id not in required_event_ids]
    remaining = max(settings.triage.max_attention_items - len(required), 0)
    result = required + ordinary[:remaining]
    result.sort(
        key=lambda item: (
            -item.anomaly_score,
            item.recording_time.timestamp() if item.recording_time else 0.0,
            item.event_id,
        )
    )
    return result


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
