"""Canonical, validated data models for event and daily-report artifacts.

The models in this module deliberately describe observations, not judgements.
They are shared by provider response validation, JSON persistence, report
generation, and the SQLite state store.
"""

from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime, time, timedelta
from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)


SCHEMA_VERSION = "1.1"

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class SchemaModel(BaseModel):
    """Base for all externally persisted schemas.

    Extra keys are rejected so an unexpected provider response cannot silently
    become part of the canonical data format.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
        use_enum_values=True,
    )


class Certainty(str, Enum):
    """How directly a statement is supported by visible evidence."""

    OBSERVED = "observed"
    UNCERTAIN = "uncertain"
    UNKNOWN = "unknown"
    NOT_VISIBLE = "not_visible"


class TimeConfidence(str, Enum):
    """Origin and reliability of the recording timestamp."""

    FILENAME = "filename"
    CONTAINER_METADATA = "container_metadata"
    UNKNOWN = "unknown"


class APIUsage(SchemaModel):
    """Provider usage metadata; every field may be unavailable."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    request_count: int = Field(default=0, ge=0)
    api_processing_sec: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    estimated_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cost_currency: NonEmptyText | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_and_check_token_total(self) -> APIUsage:
        known_sum: int | None = None
        if self.input_tokens is not None and self.output_tokens is not None:
            known_sum = self.input_tokens + self.output_tokens
            if self.total_tokens is None:
                object.__setattr__(self, "total_tokens", known_sum)
        if known_sum is not None and self.total_tokens is not None:
            if self.total_tokens < known_sum:
                raise ValueError("total_tokens cannot be less than input_tokens + output_tokens")
        if self.estimated_cost is not None and self.cost_currency is None:
            object.__setattr__(self, "cost_currency", "USD")
        return self

    def plus(self, other: APIUsage) -> APIUsage:
        """Return an aggregate without losing the fact that a metric is unknown."""

        def add_optional(left: int | float | None, right: int | float | None) -> Any:
            # A zero-request APIUsage is the neutral accumulator. Once both
            # sides describe real calls, a missing metric must stay unknown;
            # treating it as zero would make partial provider metadata look
            # like a complete token/cost total.
            if self.request_count == 0:
                return right
            if other.request_count == 0:
                return left
            if left is None or right is None:
                return None
            return left + right

        if self.request_count == 0:
            cost = other.estimated_cost
            currency = other.cost_currency
        elif other.request_count == 0:
            cost = self.estimated_cost
            currency = self.cost_currency
        elif (
            self.estimated_cost is None
            or other.estimated_cost is None
            or not self.cost_currency
            or not other.cost_currency
            or self.cost_currency != other.cost_currency
        ):
            # Unknown or differently denominated costs cannot be presented as
            # a complete aggregate.
            cost = None
            currency = None
        else:
            cost = self.estimated_cost + other.estimated_cost
            currency = self.cost_currency

        def detail_entries(details: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
            responses = details.get("responses")
            if set(details) == {"responses"} and isinstance(responses, list):
                if all(isinstance(item, dict) for item in responses):
                    return [dict(item) for item in responses]
            return [dict(details)] if details else []

        if self.request_count == 0:
            details = dict(other.details)
        elif other.request_count == 0:
            details = dict(self.details)
        else:
            entries = detail_entries(self.details) + detail_entries(other.details)
            details = {"responses": entries} if entries else {}
        return APIUsage(
            input_tokens=add_optional(self.input_tokens, other.input_tokens),
            output_tokens=add_optional(self.output_tokens, other.output_tokens),
            total_tokens=add_optional(self.total_tokens, other.total_tokens),
            request_count=self.request_count + other.request_count,
            api_processing_sec=add_optional(
                self.api_processing_sec, other.api_processing_sec
            ),
            estimated_cost=cost,
            cost_currency=currency,
            details=details,
        )


class VideoMetadata(SchemaModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0, allow_inf_nan=False)
    codec: NonEmptyText


class Entity(SchemaModel):
    """An open-vocabulary entity observed during an event."""

    type: NonEmptyText
    count: int = Field(ge=1)
    description: NonEmptyText | None = None
    attributes: list[NonEmptyText] = Field(default_factory=list)
    first_seen_sec: Seconds | None = None
    last_seen_sec: Seconds | None = None
    certainty: Certainty = Certainty.OBSERVED

    @model_validator(mode="after")
    def validate_seen_window(self) -> Entity:
        if (
            self.first_seen_sec is not None
            and self.last_seen_sec is not None
            and self.last_seen_sec < self.first_seen_sec
        ):
            raise ValueError("last_seen_sec must be greater than or equal to first_seen_sec")
        return self


class TimedStatement(SchemaModel):
    start_sec: Seconds
    end_sec: Seconds
    description: NonEmptyText
    certainty: Certainty = Certainty.OBSERVED

    @model_validator(mode="after")
    def validate_time_window(self) -> TimedStatement:
        if self.end_sec < self.start_sec:
            raise ValueError("end_sec must be greater than or equal to start_sec")
        return self


class Observation(TimedStatement):
    """A chronological description of visible state or change."""

    visible_evidence: list[NonEmptyText] = Field(default_factory=list)


class Interaction(TimedStatement):
    """A visible relation between people, objects, animals, or the scene."""

    participants: list[NonEmptyText] = Field(default_factory=list)


class SceneChange(TimedStatement):
    """A meaningful scene-level change not fully captured by an interaction."""


class ProcessingMetadata(SchemaModel):
    provider: NonEmptyText
    model: NonEmptyText
    prompt_version: Identifier
    schema_version: Identifier = SCHEMA_VERSION
    analysis_signature: Identifier | None = None
    source_fingerprint: Identifier | None = None
    frame_interval_sec: float = Field(gt=0, allow_inf_nan=False)
    frame_max_long_edge_px: int | None = Field(default=None, gt=0)
    frames_analyzed: int = Field(ge=0)
    chunk_count: int = Field(default=1, ge=1)
    processing_time_sec: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    usage: APIUsage = Field(default_factory=APIUsage)
    completed_at: datetime | None = None

    @field_validator("completed_at")
    @classmethod
    def completed_at_must_have_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        return value


def _validate_relative_source(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if normalized in {"", "."} or path.is_absolute() or ".." in path.parts:
        raise ValueError("source_file must be a safe relative path")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("source_file must not contain a host drive path")
    return path.as_posix()


class EventAnalysis(SchemaModel):
    """The observation-only portion returned by the VLM.

    Deterministic source/video/processing metadata is intentionally absent so
    it can be populated by trusted local code rather than copied from a model
    response.
    """

    entities: list[Entity] = Field(default_factory=list)
    observations: list[Observation] = Field(min_length=1)
    interactions: list[Interaction] = Field(default_factory=list)
    scene_changes: list[SceneChange] = Field(default_factory=list)
    summary: NonEmptyText
    uncertainties: list[NonEmptyText] = Field(default_factory=list)

    @model_validator(mode="after")
    def observations_are_chronological(self) -> EventAnalysis:
        # Structured output guarantees the shape, but a model can still return
        # otherwise valid observations in a different array order. Canonicalize
        # that harmless variation locally instead of spending another API call.
        self.observations.sort(key=lambda item: (item.start_sec, item.end_sec))
        return self


class Event(EventAnalysis):
    """One canonical observation record for exactly one source video."""

    schema_version: Identifier = SCHEMA_VERSION
    event_id: Identifier
    source_file: NonEmptyText
    recording_start: datetime | None = None
    recording_end: datetime | None = None
    time_confidence: TimeConfidence = TimeConfidence.UNKNOWN
    duration_sec: float = Field(gt=0, allow_inf_nan=False)
    video_metadata: VideoMetadata
    processing: ProcessingMetadata

    @field_validator("source_file")
    @classmethod
    def source_file_is_relative(cls, value: str) -> str:
        return _validate_relative_source(value)

    @field_validator("recording_start", "recording_end")
    @classmethod
    def recording_times_have_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("recording timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_event_consistency(self) -> Event:
        if self.recording_start is None:
            if self.recording_end is not None:
                raise ValueError("recording_end cannot be set without recording_start")
            if self.time_confidence != TimeConfidence.UNKNOWN.value:
                raise ValueError("missing recording_start requires time_confidence='unknown'")
        else:
            if self.time_confidence == TimeConfidence.UNKNOWN.value:
                raise ValueError("known recording_start requires a timestamp source")
            if self.recording_end is None:
                object.__setattr__(
                    self,
                    "recording_end",
                    self.recording_start + timedelta(seconds=self.duration_sec),
                )
            elif self.recording_end < self.recording_start:
                raise ValueError("recording_end must not precede recording_start")

        tolerance = max(self.processing.frame_interval_sec, 0.5)
        for statement in [
            *self.observations,
            *self.interactions,
            *self.scene_changes,
        ]:
            if statement.end_sec > self.duration_sec + tolerance:
                raise ValueError("timed statement extends beyond the source video")
        for entity in self.entities:
            for timestamp in (entity.first_seen_sec, entity.last_seen_sec):
                if timestamp is not None and timestamp > self.duration_sec + tolerance:
                    raise ValueError("entity visibility extends beyond the source video")
        return self


class PersonType(str, Enum):
    """Who a visible person appears to be, judged from behaviour."""

    RESIDENT = "resident"
    VISITOR = "visitor"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class AttentionItem(SchemaModel):
    """A judgement about one event, produced by the triage pass.

    Field order is deliberate: the free-text assessment is generated before the
    numeric score so the score is written against a stated rationale instead of
    being emitted first and rationalized afterwards. ``routine_explanation``
    comes before ``notable`` so a routine match is considered before the model
    decides whether anything is genuinely unexplained.
    """

    event_id: Identifier
    assessment: NonEmptyText
    person_type: PersonType = PersonType.NOT_APPLICABLE
    # Which documented routine, if any, accounts for this event. Present means
    # "there is an ordinary explanation", which caps the score locally.
    routine_explanation: NonEmptyText | None = None
    # A label shared by every event belonging to one real-world occurrence, so
    # a single visit split across several clips is reported once.
    occurrence_id: NonEmptyText | None = None
    notable: NonEmptyText | None = None
    anomaly_score: int = Field(ge=0, le=10)
    # Populated locally from the source events, never copied from the model.
    recording_time: datetime | None = None
    source_file: NonEmptyText | None = None
    related_event_ids: list[Identifier] = Field(default_factory=list)

    @field_validator("source_file")
    @classmethod
    def source_file_is_relative(cls, value: str | None) -> str | None:
        return None if value is None else _validate_relative_source(value)

    @field_validator("recording_time")
    @classmethod
    def recording_time_has_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("recording_time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def event_is_not_its_own_relation(self) -> AttentionItem:
        if self.event_id in self.related_event_ids:
            raise ValueError("related_event_ids must not repeat the item's own event")
        if len(set(self.related_event_ids)) != len(self.related_event_ids):
            raise ValueError("related_event_ids must be unique")
        return self


class TriageResult(SchemaModel):
    """The judgement-layer response covering every event of the day."""

    items: list[AttentionItem] = Field(default_factory=list)
    day_notes: list[NonEmptyText] = Field(default_factory=list)


class TriageSummary(SchemaModel):
    """Deterministic accounting for the triage pass."""

    enabled: bool = True
    provider: NonEmptyText | None = None
    model: NonEmptyText | None = None
    prompt_version: Identifier | None = None
    evaluated_count: int = Field(default=0, ge=0)
    attention_count: int = Field(default=0, ge=0)
    # Events absent from an otherwise valid provider response. They receive a
    # deterministic attention item locally, but were not judged by the model.
    missing_count: int = Field(default=0, ge=0)
    # Events folded into another item because they belong to one occurrence.
    grouped_count: int = Field(default=0, ge=0)
    # Events whose score was capped because a documented routine explains them.
    routine_explained_count: int = Field(default=0, ge=0)
    score_threshold: int = Field(default=7, ge=0, le=10)
    person_type_totals: dict[NonEmptyText, int] = Field(default_factory=dict)
    usage: APIUsage = Field(default_factory=APIUsage)
    failed: bool = False

    @model_validator(mode="after")
    def validate_counts(self) -> TriageSummary:
        if self.attention_count > self.evaluated_count + self.missing_count:
            raise ValueError(
                "attention_count cannot exceed evaluated_count plus missing_count"
            )
        if any(count < 0 for count in self.person_type_totals.values()):
            raise ValueError("person type totals must be non-negative")
        return self


class TimePeriodSummary(SchemaModel):
    label: NonEmptyText
    start_local: time
    end_local: time
    summary: NonEmptyText
    event_ids: list[Identifier] = Field(default_factory=list)


class RepresentativeEvent(SchemaModel):
    """A concrete example included to make the daily narrative understandable."""

    event_id: Identifier
    recording_time: datetime | None
    source_file: NonEmptyText
    description: NonEmptyText

    @field_validator("source_file")
    @classmethod
    def source_file_is_relative(cls, value: str) -> str:
        return _validate_relative_source(value)

    @field_validator("recording_time")
    @classmethod
    def recording_time_has_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("recording_time must be timezone-aware")
        return value


class FailedEvent(SchemaModel):
    source_file: NonEmptyText
    recording_time: datetime | None
    error: NonEmptyText

    @field_validator("source_file")
    @classmethod
    def source_file_is_relative(cls, value: str) -> str:
        return _validate_relative_source(value)

    @field_validator("recording_time")
    @classmethod
    def recording_time_has_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("recording_time must be timezone-aware")
        return value


class ProcessingSummary(SchemaModel):
    video_files: int = Field(ge=0)
    event_json_count: int = Field(ge=0)
    cache_reused: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    unstable_skipped: int = Field(default=0, ge=0)
    failures: list[FailedEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> ProcessingSummary:
        if self.cache_reused > self.event_json_count:
            raise ValueError("cache_reused cannot exceed event_json_count")
        accounted = self.event_json_count + self.failed_count + self.unstable_skipped
        if accounted > self.video_files:
            raise ValueError("processing counts cannot exceed video_files")
        if self.failed_count != len(self.failures):
            raise ValueError("failed_count must match the number of failures")
        return self


class ReportProcessingMetadata(SchemaModel):
    provider: NonEmptyText
    model: NonEmptyText
    prompt_version: Identifier
    schema_version: Identifier = SCHEMA_VERSION
    input_signature: Identifier | None = None
    cache_reused: bool = False
    processing_time_sec: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    usage: APIUsage = Field(default_factory=APIUsage)
    completed_at: datetime | None = None

    @field_validator("completed_at")
    @classmethod
    def completed_at_must_have_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("completed_at must be timezone-aware")
        return value


class DailyReport(SchemaModel):
    """Canonical daily report persisted as JSON and rendered by the viewer."""

    schema_version: Identifier = SCHEMA_VERSION
    date: Date
    timezone: NonEmptyText = "Asia/Tokyo"
    title: NonEmptyText = "防犯カメラ日報"
    event_count: int = Field(ge=0)
    overview: NonEmptyText
    attention_items: list[AttentionItem] = Field(default_factory=list)
    day_notes: list[NonEmptyText] = Field(default_factory=list)
    time_periods: list[TimePeriodSummary] = Field(default_factory=list)
    recurring_patterns: list[NonEmptyText] = Field(default_factory=list)
    entity_totals: dict[NonEmptyText, int] = Field(default_factory=dict)
    representative_events: list[RepresentativeEvent] = Field(default_factory=list)
    source_event_ids: list[Identifier] = Field(default_factory=list)
    processing_summary: ProcessingSummary
    triage_summary: TriageSummary = Field(default_factory=TriageSummary)
    processing: ReportProcessingMetadata

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if value == "Asia/Tokyo":
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @field_validator("entity_totals")
    @classmethod
    def entity_counts_are_nonnegative(cls, value: dict[str, int]) -> dict[str, int]:
        if any(count < 0 for count in value.values()):
            raise ValueError("entity totals must be non-negative")
        return value

    @model_validator(mode="after")
    def validate_event_references(self) -> DailyReport:
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("source_event_ids must be unique")
        if self.event_count != len(self.source_event_ids):
            raise ValueError("event_count must match source_event_ids")
        if self.event_count != self.processing_summary.event_json_count:
            raise ValueError("event_count must match processing_summary.event_json_count")

        known = set(self.source_event_ids)
        referenced = {
            event_id
            for period in self.time_periods
            for event_id in period.event_ids
        }
        referenced.update(item.event_id for item in self.representative_events)
        referenced.update(item.event_id for item in self.attention_items)
        for item in self.attention_items:
            referenced.update(item.related_event_ids)
        unknown = referenced - known
        if unknown:
            raise ValueError(f"report references unknown event IDs: {sorted(unknown)}")

        attention_ids = [item.event_id for item in self.attention_items]
        if len(set(attention_ids)) != len(attention_ids):
            raise ValueError("attention_items must reference each event at most once")
        if self.triage_summary.attention_count != len(self.attention_items):
            raise ValueError("triage_summary.attention_count must match attention_items")
        covered = (
            self.triage_summary.evaluated_count
            + self.triage_summary.missing_count
        )
        if covered > self.event_count:
            raise ValueError("triage cannot cover more events than the day contains")
        return self


# Explicit aliases keep call sites readable while allowing the persisted names to
# remain concise and stable.
EventEntity = Entity
EventObservation = Observation
FeaturedEvent = RepresentativeEvent
FailedFile = FailedEvent
DailyProcessingSummary = ProcessingSummary


__all__ = [
    "APIUsage",
    "AttentionItem",
    "Certainty",
    "DailyProcessingSummary",
    "DailyReport",
    "Entity",
    "Event",
    "EventAnalysis",
    "EventEntity",
    "EventObservation",
    "FailedFile",
    "FailedEvent",
    "FeaturedEvent",
    "Interaction",
    "Observation",
    "NonEmptyText",
    "PersonType",
    "ProcessingMetadata",
    "ProcessingSummary",
    "ReportProcessingMetadata",
    "SCHEMA_VERSION",
    "SchemaModel",
    "SceneChange",
    "TimeConfidence",
    "TimePeriodSummary",
    "TriageResult",
    "TriageSummary",
    "VideoMetadata",
    "RepresentativeEvent",
]
