"""Small, private regression suite for the text-only triage stage.

Fixtures contain already-produced event JSON, never video frames.  They live in
the persistent state directory by default so household observations do not need
to be committed to the public source repository.
"""

from __future__ import annotations

import re
from datetime import date as Date
from pathlib import Path
from typing import Annotated, Literal, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.config import AppSettings
from app.genai.base import GenAIProvider
from app.io_utils import atomic_write_json
from app.models import APIUsage, Event, Identifier, PersonType
from app.triage import HistoryEntry, TriageOutcome, run_triage


NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Presence = Literal["present", "absent"]


class EvalModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        use_enum_values=True,
    )


class HistoryFixture(EvalModel):
    date: NonEmpty
    overview: NonEmpty
    recurring_patterns: tuple[NonEmpty, ...] = ()
    attention_notes: tuple[NonEmpty, ...] = ()

    @classmethod
    def from_entry(cls, entry: HistoryEntry) -> HistoryFixture:
        return cls(
            date=entry.date,
            overview=entry.overview,
            recurring_patterns=entry.recurring_patterns,
            attention_notes=entry.attention_notes,
        )

    def to_entry(self) -> HistoryEntry:
        return HistoryEntry(
            date=self.date,
            overview=self.overview,
            recurring_patterns=self.recurring_patterns,
            attention_notes=self.attention_notes,
        )


class EventExpectation(EvalModel):
    event_id: Identifier
    attention: bool
    person_type: PersonType | None = None
    routine_explanation: Presence | None = None
    routine_explanation_contains: NonEmpty | None = None
    notable: Presence | None = None
    anomaly_score_min: int | None = Field(default=None, ge=0, le=10)
    anomaly_score_max: int | None = Field(default=None, ge=0, le=10)

    @model_validator(mode="after")
    def score_range_is_ordered(self) -> EventExpectation:
        if (
            self.routine_explanation == "absent"
            and self.routine_explanation_contains is not None
        ):
            raise ValueError(
                "routine_explanation_contains conflicts with an absent routine"
            )
        if (
            self.anomaly_score_min is not None
            and self.anomaly_score_max is not None
            and self.anomaly_score_min > self.anomaly_score_max
        ):
            raise ValueError("anomaly_score_min cannot exceed anomaly_score_max")
        return self


class TriageEvalCase(EvalModel):
    id: Identifier
    description: NonEmpty
    target_date: Date
    observed_frequency: NonEmpty | None = None
    events: list[Event] = Field(min_length=1)
    history: list[HistoryFixture] = Field(default_factory=list)
    expectations: list[EventExpectation] = Field(min_length=1)

    @model_validator(mode="after")
    def expectations_reference_case_events(self) -> TriageEvalCase:
        event_ids = [event.event_id for event in self.events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("case event_id values must be unique")
        expected_ids = [expected.event_id for expected in self.expectations]
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError("case expectation event_id values must be unique")
        unknown = set(expected_ids) - set(event_ids)
        if unknown:
            raise ValueError(
                "expectations reference unknown event_id values: "
                + ", ".join(sorted(unknown))
            )
        return self


class ExpectationFailure(EvalModel):
    event_id: Identifier
    field: NonEmpty
    expected: str | int | bool
    actual: str | int | bool | None


class CaseResult(EvalModel):
    id: Identifier
    path: str
    passed: bool
    failures: list[ExpectationFailure] = Field(default_factory=list)
    error: str | None = None
    usage: APIUsage = Field(default_factory=APIUsage)


class SuiteResult(EvalModel):
    case_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    cases: list[CaseResult] = Field(default_factory=list)
    usage: APIUsage = Field(default_factory=APIUsage)


def default_cases_directory(settings: AppSettings) -> Path:
    return settings.paths.state / "triage-eval"


def load_case(path: Path) -> TriageEvalCase:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"cannot read triage eval case {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid YAML/JSON in triage eval case {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"triage eval case root must be a mapping: {path}")
    return TriageEvalCase.model_validate(raw)


def discover_cases(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValueError(f"triage eval cases path is not a directory: {directory}")
    suffixes = {".json", ".yaml", ".yml"}
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in suffixes
    )


def run_suite(
    *,
    cases_directory: Path,
    settings: AppSettings,
    provider: GenAIProvider,
) -> SuiteResult:
    results: list[CaseResult] = []
    total_usage = APIUsage()
    for path in discover_cases(cases_directory):
        try:
            case = load_case(path)
            outcome = run_triage(
                target_date=case.target_date,
                events=case.events,
                settings=settings,
                provider=provider,
                history=tuple(item.to_entry() for item in case.history),
            )
            failures = evaluate_case(case, outcome)
            if outcome.summary.missing_count:
                error = (
                    "triage response omitted "
                    f"{outcome.summary.missing_count} event(s)"
                )
            else:
                error = "triage request failed" if outcome.summary.failed else None
            result = CaseResult(
                id=case.id,
                path=str(path),
                passed=not failures and error is None,
                failures=failures,
                error=error,
                usage=outcome.summary.usage,
            )
        except Exception as exc:  # noqa: BLE001 - report every bad case, then continue
            result = CaseResult(
                id=_fallback_case_id(path),
                path=str(path),
                passed=False,
                error=" ".join((str(exc) or exc.__class__.__name__).split())[:1000],
            )
        results.append(result)
        total_usage = total_usage.plus(result.usage)
    passed = sum(result.passed for result in results)
    return SuiteResult(
        case_count=len(results),
        passed_count=passed,
        failed_count=len(results) - passed,
        cases=results,
        usage=total_usage,
    )


def evaluate_case(
    case: TriageEvalCase, outcome: TriageOutcome
) -> list[ExpectationFailure]:
    failures: list[ExpectationFailure] = []
    items = {item.event_id: item for item in outcome.all_items}
    attention_ids = {
        item.event_id for item in outcome.attention_items
    } | {
        event_id
        for item in outcome.attention_items
        for event_id in item.related_event_ids
    }
    for expected in case.expectations:
        item = items.get(expected.event_id)
        if item is None:
            failures.append(
                ExpectationFailure(
                    event_id=expected.event_id,
                    field="evaluated",
                    expected=True,
                    actual=False,
                )
            )
            continue
        _compare(
            failures,
            expected.event_id,
            "attention",
            expected.attention,
            expected.event_id in attention_ids,
        )
        if expected.person_type is not None:
            _compare(
                failures,
                expected.event_id,
                "person_type",
                str(expected.person_type),
                str(item.person_type),
            )
        if expected.routine_explanation is not None:
            _compare_presence(
                failures,
                expected.event_id,
                "routine_explanation",
                expected.routine_explanation,
                item.routine_explanation,
            )
        if expected.routine_explanation_contains is not None and (
            not item.routine_explanation
            or expected.routine_explanation_contains not in item.routine_explanation
        ):
            failures.append(
                ExpectationFailure(
                    event_id=expected.event_id,
                    field="routine_explanation_contains",
                    expected=expected.routine_explanation_contains,
                    actual=item.routine_explanation,
                )
            )
        if expected.notable is not None:
            _compare_presence(
                failures,
                expected.event_id,
                "notable",
                expected.notable,
                item.notable,
            )
        if (
            expected.anomaly_score_min is not None
            and item.anomaly_score < expected.anomaly_score_min
        ):
            failures.append(
                ExpectationFailure(
                    event_id=expected.event_id,
                    field="anomaly_score_min",
                    expected=expected.anomaly_score_min,
                    actual=item.anomaly_score,
                )
            )
        if (
            expected.anomaly_score_max is not None
            and item.anomaly_score > expected.anomaly_score_max
        ):
            failures.append(
                ExpectationFailure(
                    event_id=expected.event_id,
                    field="anomaly_score_max",
                    expected=expected.anomaly_score_max,
                    actual=item.anomaly_score,
                )
            )
    return failures


def create_case_from_event(
    *,
    event_path: Path,
    output_directory: Path,
    case_id: str,
    description: str,
    attention: bool,
    person_type: PersonType | None = None,
    routine_explanation: Presence | None = None,
    routine_explanation_contains: str | None = None,
    notable: Presence | None = None,
    anomaly_score_min: int | None = None,
    anomaly_score_max: int | None = None,
    observed_frequency: str | None = None,
    target_date: Date | None = None,
    include_sibling_events: bool = True,
    history: Sequence[HistoryEntry] = (),
    replace: bool = False,
) -> Path:
    event = Event.model_validate_json(event_path.read_text(encoding="utf-8"))
    resolved_date = target_date or (
        event.recording_start.date() if event.recording_start is not None else None
    )
    if resolved_date is None:
        raise ValueError("--date is required when the event has no recording_start")
    case = TriageEvalCase(
        id=case_id,
        description=description,
        target_date=resolved_date,
        observed_frequency=observed_frequency,
        events=_case_events(event_path, event, include_sibling_events),
        history=[HistoryFixture.from_entry(entry) for entry in history],
        expectations=[
            EventExpectation(
                event_id=event.event_id,
                attention=attention,
                person_type=person_type,
                routine_explanation=routine_explanation,
                routine_explanation_contains=routine_explanation_contains,
                notable=notable,
                anomaly_score_min=anomaly_score_min,
                anomaly_score_max=anomaly_score_max,
            )
        ],
    )
    output = output_directory / f"case_{case.id}.json"
    if output.exists() and not replace:
        raise FileExistsError(f"triage eval case already exists: {output}")
    atomic_write_json(output, case)
    return output


def _case_events(
    event_path: Path, target: Event, include_sibling_events: bool
) -> list[Event]:
    by_id = {target.event_id: target}
    if include_sibling_events:
        for path in sorted(event_path.parent.glob("event_*.json")):
            candidate = Event.model_validate_json(path.read_text(encoding="utf-8"))
            by_id[candidate.event_id] = candidate
    return sorted(
        by_id.values(),
        key=lambda event: (
            event.recording_start is None,
            event.recording_start.isoformat() if event.recording_start else "",
            event.event_id,
        ),
    )


def _compare(
    failures: list[ExpectationFailure],
    event_id: str,
    field: str,
    expected: str | int | bool,
    actual: str | int | bool | None,
) -> None:
    if actual != expected:
        failures.append(
            ExpectationFailure(
                event_id=event_id,
                field=field,
                expected=expected,
                actual=actual,
            )
        )


def _compare_presence(
    failures: list[ExpectationFailure],
    event_id: str,
    field: str,
    expected: Presence,
    value: str | None,
) -> None:
    actual: Presence = "present" if value else "absent"
    _compare(failures, event_id, field, expected, actual)


def _fallback_case_id(path: Path) -> str:
    candidate = re.sub(
        r"[^A-Za-z0-9._-]+", "-", path.stem.removeprefix("case_")
    ).strip("._-")
    return candidate[:128] or "invalid-case"


def write_suite_result(path: Path, result: SuiteResult) -> None:
    atomic_write_json(path, result)


__all__ = [
    "CaseResult",
    "EventExpectation",
    "ExpectationFailure",
    "HistoryFixture",
    "SuiteResult",
    "TriageEvalCase",
    "create_case_from_event",
    "default_cases_directory",
    "discover_cases",
    "evaluate_case",
    "load_case",
    "run_suite",
    "write_suite_result",
]
