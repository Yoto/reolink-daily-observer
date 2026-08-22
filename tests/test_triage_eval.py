from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from app.config import AppSettings
from app.genai.base import FrameInput, GenAIProvider, StructuredResult
from app.models import (
    APIUsage,
    Event,
    EventAnalysis,
    Observation,
    ProcessingMetadata,
    TriageResult,
    VideoMetadata,
)
from app.triage_eval import (
    EventExpectation,
    TriageEvalCase,
    create_case_from_event,
    load_case,
    run_suite,
)


def build_event(event_id: str = "newspaper-1") -> Event:
    analysis = EventAnalysis(
        observations=[
            Observation(
                start_sec=0,
                end_sec=8,
                description="人物が物体を持って玄関付近に立ち寄り、短時間で退出した。",
            )
        ],
        summary="早朝に人物が玄関付近へ短時間立ち寄った。",
    )
    return Event(
        **analysis.model_dump(),
        event_id=event_id,
        source_file=f"2026-08-16/{event_id}.mp4",
        recording_start=datetime(2026, 8, 16, 3, 10, tzinfo=timezone.utc),
        time_confidence="filename",
        duration_sec=10,
        video_metadata=VideoMetadata(width=1280, height=720, fps=25, codec="h264"),
        processing=ProcessingMetadata(
            provider="mock",
            model="test-vlm",
            prompt_version="event_observation_v2",
            frame_interval_sec=1,
            frames_analyzed=10,
        ),
    )


class TriageProvider(GenAIProvider):
    name = "scripted"
    model = "scripted-v1"

    def __init__(self, *, attention: bool = False) -> None:
        self.attention = attention

    def generate_structured(
        self,
        *,
        prompt: str,
        response_model: type,
        frames: Sequence[FrameInput] = (),
    ) -> StructuredResult:
        assert response_model is TriageResult
        payload = {
            "items": [
                {
                    "event_id": "newspaper-1",
                    "assessment": "早朝の定型的な短時間訪問。",
                    "person_type": "visitor",
                    "routine_explanation": (
                        None if self.attention else "routine_patterns の新聞配達"
                    ),
                    "occurrence_id": None,
                    "notable": "説明のつかない訪問" if self.attention else None,
                    "anomaly_score": 8,
                }
            ],
            "day_notes": [],
        }
        return StructuredResult(
            value=response_model.model_validate(payload),
            usage=APIUsage(input_tokens=20, output_tokens=10, request_count=1),
            elapsed_sec=0.01,
        )


def write_case(path: Path) -> None:
    case = TriageEvalCase(
        id="newspaper-delivery",
        description="定型的な新聞配達は確認対象にしない",
        target_date=date(2026, 8, 16),
        observed_frequency="daily",
        events=[build_event()],
        expectations=[
            EventExpectation(
                event_id="newspaper-1",
                attention=False,
                person_type="visitor",
                routine_explanation="present",
                notable="absent",
                anomaly_score_max=4,
            )
        ],
    )
    path.write_text(case.model_dump_json(indent=2), encoding="utf-8")


def test_case_created_from_an_event_is_self_contained(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    event_path.write_text(build_event().model_dump_json(indent=2), encoding="utf-8")

    output = create_case_from_event(
        event_path=event_path,
        output_directory=tmp_path / "cases",
        case_id="newspaper-delivery",
        description="新聞配達",
        attention=False,
        person_type="visitor",
        routine_explanation="present",
        notable="absent",
        anomaly_score_max=4,
        observed_frequency="daily",
    )

    loaded = load_case(output)
    assert loaded.events == [build_event()]
    assert loaded.expectations[0].attention is False
    assert loaded.observed_frequency == "daily"


def test_suite_reuses_triage_and_applies_local_score_cap(tmp_path: Path) -> None:
    write_case(tmp_path / "case_newspaper-delivery.json")

    result = run_suite(
        cases_directory=tmp_path,
        settings=AppSettings.model_validate(
            {"scene": {"routine_patterns": ["早朝に新聞配達が来る"]}}
        ),
        provider=TriageProvider(),
    )

    assert result.passed_count == 1
    assert result.failed_count == 0
    assert result.usage.request_count == 1


def test_suite_reports_each_expectation_mismatch(tmp_path: Path) -> None:
    write_case(tmp_path / "case_newspaper-delivery.json")

    result = run_suite(
        cases_directory=tmp_path,
        settings=AppSettings(),
        provider=TriageProvider(attention=True),
    )

    assert result.failed_count == 1
    fields = {failure.field for failure in result.cases[0].failures}
    assert fields == {
        "attention",
        "routine_explanation",
        "notable",
        "anomaly_score_max",
    }


def test_invalid_case_does_not_prevent_the_rest_of_the_suite(tmp_path: Path) -> None:
    write_case(tmp_path / "case_newspaper-delivery.json")
    (tmp_path / "case_broken.json").write_text(
        json.dumps({"id": "broken"}), encoding="utf-8"
    )

    result = run_suite(
        cases_directory=tmp_path,
        settings=AppSettings(),
        provider=TriageProvider(),
    )

    assert result.case_count == 2
    assert result.passed_count == 1
    broken = next(case for case in result.cases if case.id == "broken")
    assert broken.error
