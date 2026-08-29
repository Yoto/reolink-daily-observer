from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.config import AppSettings
from app.family_report import generate_family_report
from app.genai.mock import MockProvider
from app.models import (
    Event,
    Observation,
    ProcessingMetadata,
    ProcessingSummary,
    SCHEMA_VERSION,
    TriageSummary,
    VideoMetadata,
)
from app.triage import TriageOutcome
from viewer.main import create_app


def _event() -> Event:
    start = datetime(2026, 8, 29, 14, 32, 10, tzinfo=ZoneInfo("Asia/Tokyo"))
    return Event(
        schema_version=SCHEMA_VERSION,
        event_id="20260829-abcdef123456",
        source_file="Security camera_00_20260829143210.mp4",
        recording_start=start,
        recording_end=None,
        time_confidence="filename",
        duration_sec=2,
        video_metadata=VideoMetadata(width=3840, height=2160, fps=25, codec="h264"),
        entities=[],
        observations=[
            Observation(start_sec=0, end_sec=1, description="人物が画面内を移動する。")
        ],
        interactions=[],
        scene_changes=[],
        summary="人物が画面内を移動した。",
        uncertainties=[],
        processing=ProcessingMetadata(
            provider="mock",
            model="mock-v1",
            prompt_version="event_observation_v2",
            frame_interval_sec=1,
            frames_analyzed=2,
        ),
    )


def test_family_report_has_independent_cache_and_json(tmp_path: Path) -> None:
    event = _event()
    settings = AppSettings()
    provider = MockProvider()
    path = tmp_path / "family_report.json"
    summary = ProcessingSummary(video_files=1, event_json_count=1)
    triage = TriageOutcome(
        summary=TriageSummary(enabled=False, evaluated_count=0, attention_count=0)
    )

    first = generate_family_report(
        target_date=date(2026, 8, 29),
        events=[event],
        processing_summary=summary,
        settings=settings,
        provider=provider,
        triage=triage,
        cache_path=path,
    )
    second = generate_family_report(
        target_date=date(2026, 8, 29),
        events=[event],
        processing_summary=summary,
        settings=settings,
        provider=provider,
        triage=triage,
        cache_path=path,
    )

    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["processing"]["prompt_version"] == "family_report_v1"
    assert first.processing.cache_reused is False
    assert second.processing.cache_reused is True


def _daily_payload(day: str) -> dict:
    return {
        "schema_version": "1.1",
        "date": day,
        "timezone": "Asia/Tokyo",
        "title": "防犯カメラ日報",
        "event_count": 1,
        "overview": "詳細日報側の概要です。",
        "attention_items": [],
        "time_periods": [],
        "recurring_patterns": ["詳細日報側の締め文です。"],
        "representative_events": [],
        "processing_summary": {
            "video_files": 1,
            "event_json_count": 1,
            "failed_count": 0,
            "failures": [],
        },
        "triage_summary": {
            "enabled": True,
            "evaluated_count": 1,
            "attention_count": 0,
        },
    }


def _family_payload(day: str) -> dict:
    return {
        "schema_version": "1.0",
        "date": day,
        "timezone": "Asia/Tokyo",
        "title": "見守り日報",
        "event_count": 1,
        "overview": "家族向けにわかりやすくまとめた概要です。",
        "attention_items": [],
        "time_periods": [],
        "scenes": [
            {
                "event_id": "event-1",
                "recording_time": f"{day}T15:10:00+09:00",
                "source_file": "routine clip.mp4",
                "description": "午後に人の出入りがありました。",
            }
        ],
        "closing_comment": "今日は比較的静かな一日だったようです。",
        "processing_summary": {
            "video_files": 1,
            "event_json_count": 1,
            "failed_count": 0,
            "failures": [],
        },
        "triage_summary": {
            "enabled": True,
            "evaluated_count": 1,
            "attention_count": 0,
        },
    }


def test_viewer_prefers_family_json_but_details_keep_daily_json(tmp_path: Path) -> None:
    day = "2026-08-29"
    directory = tmp_path / day
    directory.mkdir(parents=True)
    (directory / "daily_report.json").write_text(
        json.dumps(_daily_payload(day), ensure_ascii=False), encoding="utf-8"
    )
    (directory / "family_report.json").write_text(
        json.dumps(_family_payload(day), ensure_ascii=False), encoding="utf-8"
    )
    client = TestClient(create_app(output_root=tmp_path))

    family = client.get(f"/report/{day}")
    details = client.get(f"/report/{day}/details")

    assert family.status_code == 200
    assert "家族向けにわかりやすくまとめた概要です。" in family.text
    assert "今日は比較的静かな一日だったようです。" in family.text
    assert "午後に人の出入りがありました。" in family.text
    assert "詳細日報側の概要です。" not in family.text
    assert 'src="/videos/2026/08/29/routine%20clip.mp4#t=5"' in family.text

    assert details.status_code == 200
    assert "詳細日報側の概要です。" in details.text
    assert "家族向けにわかりやすくまとめた概要です。" not in details.text
