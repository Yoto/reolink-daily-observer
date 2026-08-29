from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from viewer.main import create_app


def report_payload(day: str, *, source_file: str = "camera clip.mp4") -> dict:
    return {
        "schema_version": "1.1",
        "date": day,
        "timezone": "Asia/Tokyo",
        "title": "防犯カメラ日報",
        "event_count": 1,
        "overview": "静かな一日でした。",
        "attention_items": [
            {
                "event_id": "event-1",
                "assessment": "玄関前に人がいました。",
                "notable": "玄関前にしばらく立ち止まっていました。",
                "person_type": "visitor",
                "anomaly_score": 7,
                "recording_time": f"{day}T07:42:00+09:00",
                "source_file": source_file,
                "related_event_ids": ["event-2"],
            }
        ],
        "representative_events": [
            {
                "event_id": "event-1",
                "recording_time": f"{day}T07:42:00+09:00",
                "source_file": source_file,
                "description": "訪問者が通過しました。",
            },
            {
                "event_id": "event-3",
                "recording_time": f"{day}T15:10:00+09:00",
                "source_file": "routine clip.mp4",
                "description": "午後に配達と思われる訪問がありました。",
            }
        ],
        "time_periods": [
            {
                "label": "朝",
                "start_local": "04:00:00",
                "end_local": "12:00:00",
                "summary": "朝はおおむね静かでした。",
                "event_ids": ["event-1"],
            },
            {
                "label": "午後",
                "start_local": "12:00:00",
                "end_local": "18:00:00",
                "summary": "午後に配達と思われる訪問がありました。",
                "event_ids": ["event-3"],
            },
        ],
        "recurring_patterns": ["午後は少しにぎやかな時間がありました。"],
        "source_event_ids": ["event-1"],
        "processing_summary": {
            "video_files": 2,
            "event_json_count": 1,
            "failed_count": 1,
            "failures": [
                {
                    "source_file": "broken clip.mp4",
                    "recording_time": f"{day}T08:10:00+09:00",
                    "error": "ffprobe failed",
                }
            ],
        },
        "triage_summary": {
            "enabled": True,
            "evaluated_count": 1,
            "attention_count": 1,
            "score_threshold": 7,
        },
        "processing": {
            "provider": "mock",
            "model": "test",
            "prompt_version": "v1",
        },
    }


def write_report(root: Path, day: str, payload: dict | None = None) -> None:
    target = root / day / "daily_report.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(payload or report_payload(day), ensure_ascii=False),
        encoding="utf-8",
    )


def test_root_redirects_to_latest_and_report_navigation(tmp_path: Path) -> None:
    write_report(tmp_path, "2026-08-27")
    write_report(tmp_path, "2026-08-29")
    client = TestClient(create_app(output_root=tmp_path))

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/report/2026-08-29"

    page = client.get("/report/2026-08-29")
    assert page.status_code == 200
    assert 'href="/report/2026-08-27"' in page.text
    assert 'href="/videos/2026/08/29/camera%20clip.mp4"' in page.text
    assert 'href="/report/2026-08-29/details"' in page.text
    assert "確認をお願いしたい動画が1件あります" in page.text
    assert "玄関前に人がいました。" in page.text
    assert 'src="/videos/2026/08/29/camera%20clip.mp4#t=0.1"' in page.text
    assert 'src="/videos/2026/08/29/routine%20clip.mp4#t=0.1"' in page.text
    assert "今日の場面" in page.text
    assert "午後は少しにぎやかな時間がありました。" in page.text
    assert "異常度" not in page.text
    assert "イベントID" not in page.text
    assert "キャッシュ" not in page.text
    assert "broken clip.mp4" not in page.text
    assert "ffprobe failed" not in page.text
    assert "日報に反映できなかった映像があります" in page.text

    details = client.get("/report/2026-08-29/details")
    assert details.status_code == 200
    assert 'href="/report/2026-08-29"' in details.text
    assert "異常度 7/10" in details.text
    assert "同じ出来事の関連動画: 1件（event-2）" in details.text
    assert "broken clip.mp4" in details.text
    assert "ffprobe failed" in details.text


def test_report_states_when_nothing_needs_attention(tmp_path: Path) -> None:
    payload = report_payload("2026-08-29")
    payload["attention_items"] = []
    payload["triage_summary"]["attention_count"] = 0
    payload["processing_summary"].update(
        {"video_files": 1, "failed_count": 0, "failures": []}
    )
    write_report(tmp_path, "2026-08-29", payload)
    client = TestClient(create_app(output_root=tmp_path))

    page = client.get("/report/2026-08-29")
    assert page.status_code == 200
    assert "確認をお願いしたい動画はありませんでした" in page.text
    assert "そのほかに、特に変わった出来事は見つかりませんでした" in page.text
    assert "該当なし" not in page.text


def test_flat_camera_layout_changes_video_url(tmp_path: Path) -> None:
    write_report(tmp_path, "2026-08-29")
    client = TestClient(create_app(output_root=tmp_path, camera_date_layout="flat"))

    page = client.get("/report/2026-08-29")
    assert 'href="/videos/2026-08-29/camera%20clip.mp4"' in page.text
    assert 'src="/videos/2026-08-29/camera%20clip.mp4#t=0.1"' in page.text


def test_ai_text_is_escaped_and_openapi_is_disabled(tmp_path: Path) -> None:
    payload = report_payload("2026-08-29")
    payload["overview"] = '<img src=x onerror="alert(1)">'
    write_report(tmp_path, "2026-08-29", payload)
    client = TestClient(create_app(output_root=tmp_path))

    page = client.get("/report/2026-08-29")
    assert "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in page.text
    assert '<img src=x onerror="alert(1)">' not in page.text
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_scene_preview_keeps_its_full_frame_and_description_space(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(output_root=tmp_path))

    response = client.get("/static/viewer.css")
    compact_css = "".join(response.text.split())

    assert response.status_code == 200
    assert "object-fit:cover" not in compact_css
    assert ".scene-preview{aspect-ratio:auto;}" in compact_css
    assert ".scene-card.scene-preview{height:auto;" in compact_css


def test_traversal_source_and_mismatched_report_date_are_rejected(
    tmp_path: Path,
) -> None:
    traversal = report_payload("2026-08-29", source_file="../../etc/passwd.mp4")
    write_report(tmp_path, "2026-08-29", traversal)
    client = TestClient(create_app(output_root=tmp_path))
    assert client.get("/report/2026-08-29").status_code == 500

    write_report(tmp_path, "2026-08-30", report_payload("2026-08-29"))
    assert client.get("/report/2026-08-30").status_code == 500

    unsupported = report_payload("2026-08-31")
    unsupported["schema_version"] = "2"
    write_report(tmp_path, "2026-08-31", unsupported)
    assert client.get("/report/2026-08-31").status_code == 500


def test_date_selector_is_strict(tmp_path: Path) -> None:
    client = TestClient(create_app(output_root=tmp_path))
    assert client.get("/report", params={"date": "../../etc/passwd"}).status_code == 422
    assert (
        client.get("/report/details", params={"date": "../../etc/passwd"}).status_code
        == 422
    )
    assert client.get("/report/not-a-date").status_code == 422


def test_detail_date_selector_keeps_detail_view(tmp_path: Path) -> None:
    write_report(tmp_path, "2026-08-29")
    client = TestClient(create_app(output_root=tmp_path))

    response = client.get(
        "/report/details",
        params={"date": "2026-08-29"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/report/2026-08-29/details"


def test_report_symlinks_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(report_payload("2026-08-29")), encoding="utf-8")
    day = tmp_path / "2026-08-29"
    day.mkdir()
    (day / "daily_report.json").symlink_to(outside)

    client = TestClient(create_app(output_root=tmp_path))
    assert client.get("/report/2026-08-29").status_code == 404
