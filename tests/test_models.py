from __future__ import annotations

import json
from datetime import date, datetime, time, timezone

import pytest
from pydantic import ValidationError

from app.config import AppSettings, SettingsError, load_settings
from app.models import (
    APIUsage,
    DailyReport,
    Event,
    EventAnalysis,
    Observation,
    ProcessingMetadata,
    ProcessingSummary,
    ReportProcessingMetadata,
    RepresentativeEvent,
    TimePeriodSummary,
    VideoMetadata,
)


def event_analysis() -> EventAnalysis:
    return EventAnalysis(
        observations=[
            Observation(
                start_sec=0,
                end_sec=3,
                description="A person enters from the right side of the frame.",
                visible_evidence=["The person is visible in consecutive frames."],
            ),
            Observation(
                start_sec=3,
                end_sec=8,
                description="The person walks toward the left edge and exits.",
            ),
        ],
        summary="One person crosses the camera view from right to left.",
        uncertainties=["The person's precise gaze direction is not visible."],
    )


def processing() -> ProcessingMetadata:
    return ProcessingMetadata(
        provider="mock",
        model="test-vlm",
        prompt_version="event_observation_v1",
        frame_interval_sec=1,
        frames_analyzed=10,
        usage=APIUsage(input_tokens=30, output_tokens=20, request_count=1),
    )


def test_event_analysis_is_observation_only_and_normalizes_order() -> None:
    analysis = event_analysis()
    payload = analysis.model_dump(mode="json")
    assert set(payload) == {
        "entities",
        "observations",
        "interactions",
        "scene_changes",
        "summary",
        "uncertainties",
    }

    payload["observations"] = list(reversed(payload["observations"]))
    normalized = EventAnalysis.model_validate(payload)
    assert [item.start_sec for item in normalized.observations] == [0, 3]


def test_event_calculates_end_time_and_round_trips_json() -> None:
    start = datetime(2026, 8, 16, 14, 32, 10, tzinfo=timezone.utc)
    event = Event(
        **event_analysis().model_dump(),
        event_id="20260816-0012",
        source_file="2026-08-16/camera_20260816143210.mp4",
        recording_start=start,
        time_confidence="filename",
        duration_sec=10,
        video_metadata=VideoMetadata(width=3840, height=2160, fps=25, codec="h264"),
        processing=processing(),
    )

    assert event.recording_end == datetime(2026, 8, 16, 14, 32, 20, tzinfo=timezone.utc)
    serialized = event.model_dump_json()
    assert Event.model_validate_json(serialized) == event
    lowered = serialized.lower()
    for excluded_name in ("risk_score", "novelty_score", "threat_level", "anomaly"):
        assert excluded_name not in lowered


def test_event_rejects_untrusted_extra_fields_and_inconsistent_time() -> None:
    payload = {
        **event_analysis().model_dump(mode="json"),
        "event_id": "event-1",
        "source_file": "clip.mp4",
        "recording_start": None,
        "recording_end": None,
        "time_confidence": "unknown",
        "duration_sec": 10,
        "video_metadata": {"width": 1280, "height": 720, "fps": 25, "codec": "h264"},
        "processing": processing().model_dump(mode="json"),
        "unexpected_score": 1,
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Event.model_validate(payload)

    payload.pop("unexpected_score")
    payload["time_confidence"] = "filename"
    with pytest.raises(ValidationError, match="requires time_confidence='unknown'"):
        Event.model_validate(payload)


def test_timed_observations_cannot_exceed_video_duration() -> None:
    analysis = event_analysis().model_copy(deep=True)
    analysis.observations[-1].end_sec = 25
    with pytest.raises(ValidationError, match="extends beyond"):
        Event(
            **analysis.model_dump(),
            event_id="event-1",
            source_file="clip.mp4",
            duration_sec=10,
            video_metadata={"width": 1280, "height": 720, "fps": 25, "codec": "h264"},
            processing=processing(),
        )


def test_api_usage_derives_total_and_aggregates() -> None:
    first = APIUsage(input_tokens=10, output_tokens=4, request_count=1)
    second = APIUsage(
        input_tokens=3,
        output_tokens=2,
        total_tokens=6,
        request_count=2,
        api_processing_sec=0.5,
    )
    assert first.total_tokens == 14
    combined = first.plus(second)
    assert combined.input_tokens == 13
    assert combined.output_tokens == 6
    assert combined.total_tokens == 20
    assert combined.request_count == 3


def test_api_usage_aggregation_preserves_details_and_unknown_metrics() -> None:
    known = APIUsage(
        input_tokens=10,
        output_tokens=4,
        request_count=1,
        estimated_cost=0.01,
        cost_currency="USD",
        details={"response_id": "response-1"},
    )
    assert APIUsage().plus(known).details == {"response_id": "response-1"}

    missing_metadata = APIUsage(request_count=1, api_processing_sec=0.5)
    combined = known.plus(missing_metadata)
    assert combined.input_tokens is None
    assert combined.output_tokens is None
    assert combined.total_tokens is None
    assert combined.estimated_cost is None
    assert combined.cost_currency is None
    assert combined.details == {"responses": [{"response_id": "response-1"}]}


def test_daily_report_references_are_consistent() -> None:
    report = DailyReport(
        date=date(2026, 8, 16),
        event_count=1,
        overview="One video recorded a person crossing the camera view.",
        time_periods=[
            TimePeriodSummary(
                label="Afternoon",
                start_local=time(12),
                end_local=time(18),
                summary="A person crossed the scene.",
                event_ids=["event-1"],
            )
        ],
        recurring_patterns=["A person moved continuously across the scene."],
        representative_events=[
            RepresentativeEvent(
                event_id="event-1",
                recording_time=None,
                source_file="clip.mp4",
                description="Recording time is unknown; one person crossed the view.",
            )
        ],
        source_event_ids=["event-1"],
        processing_summary=ProcessingSummary(
            video_files=1,
            event_json_count=1,
        ),
        processing=ReportProcessingMetadata(
            provider="mock",
            model="test-text-model",
            prompt_version="daily_report_v1",
        ),
    )
    assert report.date == date(2026, 8, 16)
    assert report.representative_events[0].recording_time is None

    payload = report.model_dump(mode="json")
    payload["representative_events"][0]["event_id"] = "missing-event"
    with pytest.raises(ValidationError, match="unknown event IDs"):
        DailyReport.model_validate(payload)


def test_load_settings_merges_yaml_placeholders_and_environment(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
timezone: Asia/Tokyo
paths:
  state: /state
frames:
  interval_sec: 1
genai:
  provider: openai
  model: ${TEST_MODEL}
  api_key: ${TEST_KEY}
  max_images_per_request: auto
prompts:
  event_synthesis_version: synthesis-from-yaml
""",
        encoding="utf-8",
    )
    settings = load_settings(
        config_path,
        environ={
            "TEST_MODEL": "yaml-model",
            "TEST_KEY": "secret-value",
            "GENAI_MODEL": "environment-model",
            "ANALYZER_FRAMES__INTERVAL_SEC": "2",
            "ANALYZER_GENAI__MAX_INLINE_IMAGE_BYTES": "9000000",
        },
    )

    assert settings.genai.model == "environment-model"
    assert settings.genai.api_key is not None
    assert settings.genai.api_key.get_secret_value() == "secret-value"
    assert settings.frames.interval_sec == 2
    assert settings.genai.max_inline_image_bytes == 9_000_000
    assert settings.paths.state_database.as_posix() == "/state/state.sqlite"
    assert settings.report.json is True
    assert settings.report.model_dump()["json"] is True
    # Secrets are masked by Pydantic representations and dumps can be audited.
    assert "secret-value" not in repr(settings)


def test_load_settings_fails_for_missing_placeholder(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("genai:\n  model: ${MISSING_MODEL}\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="MISSING_MODEL"):
        load_settings(config_path, environ={})


def test_resolution_reduction_defaults_and_bounds() -> None:
    settings = AppSettings()
    reduction = settings.frames.resolution_reduction

    assert reduction.enabled
    assert reduction.daytime_max_long_edge_px == 768
    assert reduction.saturation_threshold == 20
    assert reduction.dark_ratio_threshold == 0.2

    with pytest.raises(
        ValidationError,
        match="daytime_max_long_edge_px cannot exceed max_long_edge_px",
    ):
        AppSettings.model_validate(
            {
                "frames": {
                    "max_long_edge_px": 640,
                    "resolution_reduction": {"daytime_max_long_edge_px": 768},
                }
            }
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"timezone": "UTC"},
        {"prompts": {"event_version": "event version with spaces"}},
        {"prompts": {"event_version": "x" * 116}},
        {"report": {"json": False}},
    ],
)
def test_settings_reject_values_that_break_the_runtime_contract(payload) -> None:
    with pytest.raises(ValidationError):
        AppSettings.model_validate(payload)


def test_models_emit_valid_json() -> None:
    payload = APIUsage(input_tokens=1, output_tokens=2).model_dump(mode="json")
    assert json.loads(json.dumps(payload)) == payload
