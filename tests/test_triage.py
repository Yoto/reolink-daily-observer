from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

import pytest

from app.config import AppSettings, SettingsError, load_settings
from app.daily_report import DailyRunStats, generate_daily_report, render_daily_report
from app.genai.base import FrameInput, GenAIProvider, StructuredResult
from app.models import (
    APIUsage,
    AttentionItem,
    Event,
    EventAnalysis,
    Observation,
    ProcessingMetadata,
    TriageResult,
    VideoMetadata,
)
from app.triage import HistoryEntry, run_triage


def build_event(event_id: str, *, hour: int = 12, summary: str = "観察記録") -> Event:
    analysis = EventAnalysis(
        observations=[
            Observation(start_sec=0, end_sec=5, description="人物が通路を移動した。")
        ],
        summary=summary,
    )
    return Event(
        **analysis.model_dump(),
        event_id=event_id,
        source_file=f"2026-08-16/{event_id}.mp4",
        recording_start=datetime(2026, 8, 16, hour, 0, tzinfo=timezone.utc),
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


class ScriptedProvider(GenAIProvider):
    """Return a prepared object per response model and record every prompt."""

    name = "scripted"

    def __init__(self, responses: dict[str, object]) -> None:
        self.model = "scripted-model"
        self._responses = responses
        self.prompts: list[str] = []

    def generate_structured(
        self,
        *,
        prompt: str,
        response_model: type,
        frames: Sequence[FrameInput] = (),
    ) -> StructuredResult:
        self.prompts.append(prompt)
        payload = self._responses.get(response_model.__name__)
        if payload is None:
            raise AssertionError(f"no scripted response for {response_model.__name__}")
        if isinstance(payload, Exception):
            raise payload
        return StructuredResult(
            value=response_model.model_validate(payload),
            usage=APIUsage(input_tokens=10, output_tokens=5, request_count=1),
            elapsed_sec=0.01,
        )


def triage_payload(*specs: tuple[str, int, str | None, str]) -> dict[str, object]:
    return {
        "items": [
            {
                "event_id": event_id,
                "assessment": "判断根拠。",
                "person_type": person_type,
                "notable": notable,
                "anomaly_score": score,
            }
            for event_id, score, notable, person_type in specs
        ],
        "day_notes": [],
    }


def triage_item(
    event_id: str,
    score: int,
    *,
    notable: str | None = None,
    person_type: str = "resident",
    routine_explanation: str | None = None,
    occurrence_id: str | None = None,
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "assessment": "判断根拠。",
        "person_type": person_type,
        "routine_explanation": routine_explanation,
        "occurrence_id": occurrence_id,
        "notable": notable,
        "anomaly_score": score,
    }


def test_threshold_and_notable_both_select_attention_items() -> None:
    events = [build_event(f"e{index}") for index in range(4)]
    provider = ScriptedProvider(
        {
            "TriageResult": triage_payload(
                ("e0", 1, None, "resident"),
                ("e1", 8, None, "unknown"),
                ("e2", 3, "普段見られない箱が置かれている", "visitor"),
                ("e3", 6, None, "unknown"),
            )
        }
    )
    settings = AppSettings()

    outcome = run_triage(
        target_date=date(2026, 8, 16),
        events=events,
        settings=settings,
        provider=provider,
    )

    assert [item.event_id for item in outcome.attention_items] == ["e1", "e2"]
    assert outcome.summary.evaluated_count == 4
    assert outcome.summary.attention_count == 2
    assert outcome.summary.person_type_totals == {
        "resident": 1,
        "unknown": 2,
        "visitor": 1,
    }
    # Local records, not the model response, supply identity and timestamps.
    assert outcome.attention_items[0].source_file == "2026-08-16/e1.mp4"
    assert outcome.attention_items[0].recording_time == events[1].recording_start


def test_disabling_notable_promotion_leaves_only_the_threshold() -> None:
    events = [build_event("e0"), build_event("e1")]
    provider = ScriptedProvider(
        {
            "TriageResult": triage_payload(
                ("e0", 2, "気になる点", "resident"),
                ("e1", 9, None, "unknown"),
            )
        }
    )
    settings = AppSettings.model_validate({"triage": {"notable_always_attention": False}})

    outcome = run_triage(
        target_date=date(2026, 8, 16),
        events=events,
        settings=settings,
        provider=provider,
    )

    assert [item.event_id for item in outcome.attention_items] == ["e1"]


def test_unknown_event_ids_are_dropped_and_missing_ones_are_tolerated() -> None:
    events = [build_event("e0"), build_event("e1")]
    provider = ScriptedProvider(
        {
            "TriageResult": triage_payload(
                ("e0", 9, None, "unknown"),
                ("hallucinated", 10, None, "unknown"),
            )
        }
    )

    outcome = run_triage(
        target_date=date(2026, 8, 16),
        events=events,
        settings=AppSettings(),
        provider=provider,
    )

    assert [item.event_id for item in outcome.all_items] == ["e0"]
    assert outcome.summary.evaluated_count == 1


def test_triage_failure_is_recorded_without_losing_the_day() -> None:
    provider = ScriptedProvider({"TriageResult": RuntimeError("provider is down")})

    outcome = run_triage(
        target_date=date(2026, 8, 16),
        events=[build_event("e0")],
        settings=AppSettings(),
        provider=provider,
    )

    assert outcome.summary.failed is True
    assert outcome.attention_items == []


def test_disabled_triage_makes_no_request() -> None:
    provider = ScriptedProvider({})
    settings = AppSettings.model_validate({"triage": {"enabled": False}})

    outcome = run_triage(
        target_date=date(2026, 8, 16),
        events=[build_event("e0")],
        settings=settings,
        provider=provider,
    )

    assert provider.prompts == []
    assert outcome.summary.enabled is False


def test_scene_and_history_reach_the_triage_prompt() -> None:
    provider = ScriptedProvider({"TriageResult": triage_payload(("e0", 1, None, "resident"))})
    settings = AppSettings.model_validate(
        {
            "scene": {
                "location": "郊外の戸建て住宅",
                "routine_patterns": ["平日朝に自転車で外出する"],
            }
        }
    )

    run_triage(
        target_date=date(2026, 8, 16),
        events=[build_event("e0")],
        settings=settings,
        provider=provider,
        history=[HistoryEntry(date="2026-08-15", overview="平常の一日だった。")],
    )

    prompt = provider.prompts[0]
    assert "郊外の戸建て住宅" in prompt
    assert "平日朝に自転車で外出する" in prompt
    assert "2026-08-15" in prompt


def test_prompt_states_that_scene_is_missing_when_unconfigured() -> None:
    provider = ScriptedProvider({"TriageResult": triage_payload(("e0", 1, None, "resident"))})

    run_triage(
        target_date=date(2026, 8, 16),
        events=[build_event("e0")],
        settings=AppSettings(),
        provider=provider,
    )

    assert "設定されていません" in provider.prompts[0]


def test_scene_file_is_merged_and_inline_values_win(tmp_path: Path) -> None:
    scene = tmp_path / "scene.yaml"
    scene.write_text(
        "location: 外部ファイルの場所\ncamera_view: 駐車スペース\n", encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        f"scene_file: {scene.name}\nscene:\n  location: インラインの場所\n",
        encoding="utf-8",
    )

    settings = load_settings(config, environ={})

    assert settings.scene.location == "インラインの場所"
    assert settings.scene.camera_view == "駐車スペース"


def test_scene_file_accepts_a_wrapping_scene_key(tmp_path: Path) -> None:
    scene = tmp_path / "scene.yaml"
    scene.write_text("scene:\n  location: 入れ子形式\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(f"scene_file: {scene.name}\n", encoding="utf-8")

    assert load_settings(config, environ={}).scene.location == "入れ子形式"


def test_missing_scene_file_warns_but_still_loads(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("scene_file: config/scene.yaml\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        settings = load_settings(config, environ={})

    assert settings.scene.is_configured is False
    assert "scene file" in caplog.text


def test_malformed_scene_file_is_a_configuration_error(tmp_path: Path) -> None:
    scene = tmp_path / "scene.yaml"
    scene.write_text("- not\n- a mapping\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(f"scene_file: {scene.name}\n", encoding="utf-8")

    with pytest.raises(SettingsError, match="must be a mapping"):
        load_settings(config, environ={})


def test_report_renders_attention_first_and_counts_it(tmp_path: Path) -> None:
    events = [build_event("e0", hour=3), build_event("e1")]
    provider = ScriptedProvider(
        {
            "TriageResult": triage_payload(
                ("e0", 8, "深夜帯に敷地内で滞留している", "unknown"),
                ("e1", 1, None, "resident"),
            ),
            "DailyNarrative": {
                "overview": "深夜帯に1件の要確認イベントがあった。",
                "time_periods": [],
                "recurring_patterns": [],
                "representative_events": [],
            },
        }
    )
    settings = AppSettings()

    triage = run_triage(
        target_date=date(2026, 8, 16),
        events=events,
        settings=settings,
        provider=provider,
    )
    report = generate_daily_report(
        target_date=date(2026, 8, 16),
        events=events,
        stats=DailyRunStats(detected_files=2),
        settings=settings,
        provider=provider,
        triage=triage,
    )

    assert report.triage_summary.attention_count == 1
    assert report.attention_items[0].event_id == "e0"

    artifacts = render_daily_report(
        report, output_directory=tmp_path, settings=settings
    )
    markdown = artifacts["markdown"].read_text(encoding="utf-8")
    assert "要確認 1件" in markdown
    assert "深夜帯に敷地内で滞留している" in markdown
    # The attention section must precede the general narrative sections.
    assert markdown.index("## 要確認") < markdown.index("## 解析状況")

    html = artifacts["html"].read_text(encoding="utf-8")
    assert "要確認 1件" in html

    payload = json.loads(artifacts["json"].read_text(encoding="utf-8"))
    assert payload["triage_summary"]["evaluated_count"] == 2
    assert payload["attention_items"][0]["person_type"] == "unknown"


def test_report_states_when_nothing_needs_attention(tmp_path: Path) -> None:
    events = [build_event("e0")]
    provider = ScriptedProvider(
        {
            "TriageResult": triage_payload(("e0", 1, None, "resident")),
            "DailyNarrative": {
                "overview": "平常の範囲で推移した。",
                "time_periods": [],
                "recurring_patterns": [],
                "representative_events": [],
            },
        }
    )
    settings = AppSettings()

    triage = run_triage(
        target_date=date(2026, 8, 16),
        events=events,
        settings=settings,
        provider=provider,
    )
    report = generate_daily_report(
        target_date=date(2026, 8, 16),
        events=events,
        stats=DailyRunStats(detected_files=1),
        settings=settings,
        provider=provider,
        triage=triage,
    )
    artifacts = render_daily_report(
        report, output_directory=tmp_path, settings=settings
    )

    markdown = artifacts["markdown"].read_text(encoding="utf-8")
    assert "要確認 0件" in markdown
    assert "該当なし" in markdown
    assert "判定した1件" in markdown


def test_attention_items_must_reference_known_events() -> None:
    from pydantic import ValidationError

    from app.models import DailyReport, ProcessingSummary, ReportProcessingMetadata

    with pytest.raises(ValidationError, match="unknown event IDs"):
        DailyReport(
            date=date(2026, 8, 16),
            event_count=0,
            overview="概要",
            attention_items=[
                AttentionItem(event_id="ghost", assessment="根拠", anomaly_score=9)
            ],
            source_event_ids=[],
            processing_summary=ProcessingSummary(video_files=0, event_json_count=0),
            processing=ReportProcessingMetadata(
                provider="mock", model="m", prompt_version="daily_report_v2"
            ),
        )


def test_scene_file_resolves_against_the_config_directory(tmp_path: Path) -> None:
    """A compose deployment mounts ./config at /config, so the reference is
    relative to the configuration file rather than the working directory."""

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "scene.yaml").write_text("location: マウント先\n", encoding="utf-8")
    config = config_dir / "config.yaml"
    config.write_text("scene_file: scene.yaml\n", encoding="utf-8")

    assert load_settings(config, environ={}).scene.location == "マウント先"


def test_scene_file_can_be_redirected_by_environment(tmp_path: Path) -> None:
    external = tmp_path / "elsewhere.yaml"
    external.write_text("location: 環境変数指定\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("scene_file: scene.yaml\n", encoding="utf-8")

    settings = load_settings(config, environ={"ANALYZER_SCENE_FILE": str(external)})

    assert settings.scene.location == "環境変数指定"


def test_routine_explained_events_are_capped_below_the_threshold() -> None:
    """Unresolved detail in an ordinary activity must not reach attention."""

    events = [build_event("e0"), build_event("e1")]
    provider = ScriptedProvider(
        {
            "TriageResult": {
                "items": [
                    triage_item(
                        "e0",
                        8,
                        routine_explanation="routine_patterns の早朝の新聞配達に該当",
                    ),
                    triage_item("e1", 8, person_type="unknown"),
                ],
                "day_notes": [],
            }
        }
    )

    outcome = run_triage(
        target_date=date(2026, 8, 16),
        events=events,
        settings=AppSettings(),
        provider=provider,
    )

    capped = {item.event_id: item.anomaly_score for item in outcome.all_items}
    assert capped["e0"] == 4
    assert capped["e1"] == 8
    assert [item.event_id for item in outcome.attention_items] == ["e1"]
    assert outcome.summary.routine_explained_count == 1


def test_a_routine_event_with_a_notable_finding_is_not_capped() -> None:
    """A documented routine explains the activity, not an unexplained finding."""

    provider = ScriptedProvider(
        {
            "TriageResult": {
                "items": [
                    triage_item(
                        "e0",
                        9,
                        notable="施錠部への干渉が記録されている",
                        routine_explanation="expected_visitors の配達に該当",
                    )
                ],
                "day_notes": [],
            }
        }
    )

    outcome = run_triage(
        target_date=date(2026, 8, 16),
        events=[build_event("e0")],
        settings=AppSettings(),
        provider=provider,
    )

    assert outcome.all_items[0].anomaly_score == 9
    assert outcome.summary.routine_explained_count == 0


def test_one_occurrence_across_several_clips_is_reported_once() -> None:
    events = [build_event(f"e{index}", hour=15 + index) for index in range(3)]
    provider = ScriptedProvider(
        {
            "TriageResult": {
                "items": [
                    triage_item("e0", 5, person_type="visitor", occurrence_id="visit-pm"),
                    triage_item(
                        "e1",
                        8,
                        person_type="visitor",
                        occurrence_id="visit-pm",
                        notable="見慣れない車両が敷地内に停車している",
                    ),
                    triage_item("e2", 6, person_type="visitor", occurrence_id="visit-pm"),
                ],
                "day_notes": [],
            }
        }
    )

    outcome = run_triage(
        target_date=date(2026, 8, 16),
        events=events,
        settings=AppSettings(),
        provider=provider,
    )

    assert len(outcome.attention_items) == 1
    leader = outcome.attention_items[0]
    assert leader.event_id == "e1"
    assert leader.related_event_ids == ["e0", "e2"]
    assert outcome.summary.grouped_count == 2
    # Every event is still evaluated and counted, only the reporting is folded.
    assert outcome.summary.evaluated_count == 3


def test_grouping_can_be_disabled() -> None:
    events = [build_event("e0"), build_event("e1")]
    provider = ScriptedProvider(
        {
            "TriageResult": {
                "items": [
                    triage_item("e0", 8, occurrence_id="visit-pm"),
                    triage_item("e1", 8, occurrence_id="visit-pm"),
                ],
                "day_notes": [],
            }
        }
    )
    settings = AppSettings.model_validate({"triage": {"group_related_events": False}})

    outcome = run_triage(
        target_date=date(2026, 8, 16),
        events=events,
        settings=settings,
        provider=provider,
    )

    assert len(outcome.attention_items) == 2
    assert outcome.summary.grouped_count == 0


def test_related_events_are_rendered_and_validated(tmp_path: Path) -> None:
    events = [build_event("e0", hour=15), build_event("e1", hour=16)]
    provider = ScriptedProvider(
        {
            "TriageResult": {
                "items": [
                    triage_item("e0", 4, person_type="visitor", occurrence_id="visit"),
                    triage_item(
                        "e1",
                        8,
                        person_type="unknown",
                        occurrence_id="visit",
                        notable="用件が読み取れないまま滞留している",
                    ),
                ],
                "day_notes": [],
            },
            "DailyNarrative": {
                "overview": "午後に1件の要確認イベントがあった。",
                "time_periods": [],
                "recurring_patterns": [],
                "representative_events": [],
            },
        }
    )
    settings = AppSettings()

    triage = run_triage(
        target_date=date(2026, 8, 16),
        events=events,
        settings=settings,
        provider=provider,
    )
    report = generate_daily_report(
        target_date=date(2026, 8, 16),
        events=events,
        stats=DailyRunStats(detected_files=2),
        settings=settings,
        provider=provider,
        triage=triage,
    )
    artifacts = render_daily_report(
        report, output_directory=tmp_path, settings=settings
    )

    markdown = artifacts["markdown"].read_text(encoding="utf-8")
    assert "要確認 1件" in markdown
    assert "同じ出来事の関連動画: 1件" in markdown
    assert "e0" in markdown


def test_an_item_cannot_relate_to_itself() -> None:
    with pytest.raises(ValueError, match="must not repeat"):
        AttentionItem(
            event_id="e0",
            assessment="根拠",
            anomaly_score=5,
            related_event_ids=["e0"],
        )
