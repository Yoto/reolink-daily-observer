"""Persisted report contracts consumed by the viewer.

The viewer deliberately owns these small read models instead of importing the
analyzer implementation. JSON is the boundary between the two containers.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def safe_source_file(value: str) -> str:
    """Normalize and validate an analyzer-produced relative video path."""

    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        normalized in {"", "."}
        or path.is_absolute()
        or ".." in path.parts
        or _WINDOWS_DRIVE.match(normalized)
    ):
        raise ValueError("source_file must be a safe relative path")
    if path.suffix.lower() != ".mp4":
        raise ValueError("source_file must identify an MP4 file")
    return path.as_posix()


class ViewModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AttentionItem(ViewModel):
    event_id: str
    assessment: str
    person_type: str = "not_applicable"
    routine_explanation: str | None = None
    notable: str | None = None
    anomaly_score: int = Field(ge=0, le=10)
    recording_time: datetime | None = None
    source_file: str | None = None
    related_event_ids: list[str] = Field(default_factory=list)

    _source_is_safe = field_validator("source_file")(
        lambda value: None if value is None else safe_source_file(value)
    )


class TimePeriod(ViewModel):
    label: str
    start_local: time
    end_local: time
    summary: str
    event_ids: list[str] = Field(default_factory=list)


class RepresentativeEvent(ViewModel):
    event_id: str
    recording_time: datetime | None = None
    source_file: str
    description: str

    _source_is_safe = field_validator("source_file")(safe_source_file)


class FamilyAttentionItem(ViewModel):
    event_id: str
    recording_time: datetime | None = None
    source_file: str
    title: str
    reason: str

    _source_is_safe = field_validator("source_file")(safe_source_file)


class FamilyScene(ViewModel):
    event_id: str
    recording_time: datetime | None = None
    source_file: str
    description: str

    _source_is_safe = field_validator("source_file")(safe_source_file)


class FailedEvent(ViewModel):
    source_file: str
    recording_time: datetime | None = None
    error: str

    _source_is_safe = field_validator("source_file")(safe_source_file)


class ProcessingSummary(ViewModel):
    video_files: int = Field(ge=0)
    event_json_count: int = Field(ge=0)
    cache_reused: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    unstable_skipped: int = Field(default=0, ge=0)
    failures: list[FailedEvent] = Field(default_factory=list)


class TriageSummary(ViewModel):
    enabled: bool = True
    evaluated_count: int = Field(default=0, ge=0)
    attention_count: int = Field(default=0, ge=0)
    missing_count: int = Field(default=0, ge=0)
    grouped_count: int = Field(default=0, ge=0)
    routine_explained_count: int = Field(default=0, ge=0)
    score_threshold: int = Field(default=7, ge=0, le=10)
    person_type_totals: dict[str, int] = Field(default_factory=dict)
    failed: bool = False


class DailyReport(ViewModel):
    schema_version: Literal["1.1"]
    date: date
    timezone: str
    title: str
    event_count: int = Field(ge=0)
    overview: str
    attention_items: list[AttentionItem] = Field(default_factory=list)
    day_notes: list[str] = Field(default_factory=list)
    time_periods: list[TimePeriod] = Field(default_factory=list)
    recurring_patterns: list[str] = Field(default_factory=list)
    entity_totals: dict[str, int] = Field(default_factory=dict)
    representative_events: list[RepresentativeEvent] = Field(default_factory=list)
    processing_summary: ProcessingSummary
    triage_summary: TriageSummary = Field(default_factory=TriageSummary)


class FamilyReport(ViewModel):
    schema_version: Literal["1.0"]
    date: date
    timezone: str
    title: str
    event_count: int = Field(ge=0)
    overview: str
    attention_items: list[FamilyAttentionItem] = Field(default_factory=list)
    time_periods: list[TimePeriod] = Field(default_factory=list)
    scenes: list[FamilyScene] = Field(default_factory=list)
    closing_comment: str
    processing_summary: ProcessingSummary
    triage_summary: TriageSummary = Field(default_factory=TriageSummary)
