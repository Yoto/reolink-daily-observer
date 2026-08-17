from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.config import AppSettings
from app.daily_report import (
    DailyNarrative,
    DailyRunStats,
    generate_daily_report,
    render_daily_report,
)
from app.event_analyzer import EventAnalyzer, chunk_frames
from app.genai.base import (
    FrameInput,
    GenAIProvider,
    GenAIResponseError,
    StructuredResult,
)
from app.models import (
    APIUsage,
    Event,
    EventAnalysis,
    Observation,
    ProcessingMetadata,
    ProcessingSummary,
    ReportProcessingMetadata,
    SCHEMA_VERSION,
    VideoMetadata,
)
from app.run_daily import main
from app.run_daily import _process_day
from app.genai.mock import MockProvider
from app.state import StateStore
from app.video import ExtractedFrame, VideoMetadata as ProbedVideoMetadata


def test_chunk_frames_honors_bytes_count_and_overlap(tmp_path: Path) -> None:
    frames = []
    for index in range(6):
        path = tmp_path / f"{index}.jpg"
        path.write_bytes(b"x" * 10)
        frames.append(ExtractedFrame(path=path, timestamp_sec=float(index), index=index))

    chunks = chunk_frames(frames, max_images=3, max_raw_bytes=25, overlap=1)

    assert [[frame.index for frame in chunk] for chunk in chunks] == [
        [0, 1],
        [1, 2],
        [2, 3],
        [3, 4],
        [4, 5],
    ]


def test_event_chunk_failure_preserves_prior_and_failed_response_usage(
    tmp_path: Path,
) -> None:
    class FailingChunkProvider(GenAIProvider):
        name = "failing"
        model = "failing-v1"

        def __init__(self) -> None:
            self.calls = 0

        def generate_structured(self, *, prompt, response_model, frames=()):
            self.calls += 1
            if self.calls == 1:
                return StructuredResult(
                    value=EventAnalysis(
                        observations=[
                            Observation(start_sec=0, end_sec=0, description="人物が見える。")
                        ],
                        summary="人物が見える。",
                    ),
                    usage=APIUsage(input_tokens=10, output_tokens=5, request_count=1),
                    elapsed_sec=0.01,
                )
            raise GenAIResponseError(
                "schema repair exhausted",
                usage=APIUsage(input_tokens=7, output_tokens=2, request_count=2),
            )

    settings = AppSettings.model_validate(
        {
            "paths": {"temp": tmp_path / "temp"},
            "genai": {
                "provider": "mock",
                "model": "mock-observer-v1",
                "max_images_per_request": 1,
                "chunk_overlap_frames": 0,
            },
        }
    )
    frames = []
    for index in range(2):
        path = tmp_path / f"frame_{index:06d}.jpg"
        path.write_bytes(b"jpeg")
        frames.append(ExtractedFrame(path=path, timestamp_sec=float(index), index=index))

    analyzer = EventAnalyzer(settings, FailingChunkProvider())
    with pytest.raises(GenAIResponseError) as captured:
        analyzer._observe(  # noqa: SLF001 - verifies failure accounting contract
            frames=frames,
            metadata=ProbedVideoMetadata(
                duration_sec=2, width=1280, height=720, fps=25, codec="h264"
            ),
            source_file="sample.mp4",
        )

    assert captured.value.usage.input_tokens == 17
    assert captured.value.usage.output_tokens == 7
    assert captured.value.usage.request_count == 3


class SpyDailyProvider(GenAIProvider):
    name = "spy"
    model = "spy-v1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[FrameInput, ...]]] = []

    def generate_structured(self, *, prompt, response_model, frames=()):
        self.calls.append((prompt, tuple(frames)))
        return StructuredResult(
            value=DailyNarrative(
                overview="event JSONだけから作成した概要。",
                time_periods=[],
                recurring_patterns=[],
                representative_events=[],
            ),
            usage=APIUsage(input_tokens=10, output_tokens=5, request_count=1),
            elapsed_sec=0.01,
        )


def test_daily_narrative_rejects_empty_recurring_pattern() -> None:
    with pytest.raises(ValidationError):
        DailyNarrative(
            overview="概要。",
            recurring_patterns=[""],
        )


def _event(event_id: str = "20260816-abcdef123456") -> Event:
    start = datetime(2026, 8, 16, 14, 32, 10, tzinfo=ZoneInfo("Asia/Tokyo"))
    return Event(
        schema_version=SCHEMA_VERSION,
        event_id=event_id,
        source_file="Security camera_00_20260816143210.mp4",
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
            prompt_version="event_observation_v1",
            frame_interval_sec=1,
            frames_analyzed=2,
        ),
    )


def test_daily_generation_sends_no_frames_and_renders_three_formats(
    tmp_path: Path,
) -> None:
    settings = AppSettings.model_validate(
        {
            "paths": {
                "input": tmp_path / "input",
                "output": tmp_path / "output",
                "state": tmp_path / "state",
                "temp": tmp_path / "temp",
            }
        }
    )
    provider = SpyDailyProvider()
    event = _event()

    report = generate_daily_report(
        target_date=date(2026, 8, 16),
        events=[event],
        stats=DailyRunStats(detected_files=1),
        settings=settings,
        provider=provider,
    )
    artifacts = render_daily_report(
        report, output_directory=tmp_path / "artifacts", settings=settings
    )

    assert len(provider.calls) == 1
    prompt, frames = provider.calls[0]
    assert frames == ()
    assert event.source_file in prompt
    assert "event JSON" in prompt
    assert set(artifacts) == {"json", "markdown", "html"}
    payload = json.loads(artifacts["json"].read_text(encoding="utf-8"))
    assert payload["overview"] == "event JSONだけから作成した概要。"
    assert "防犯カメラ日報" in artifacts["html"].read_text(encoding="utf-8")
    assert "防犯カメラ日報" in artifacts["markdown"].read_text(encoding="utf-8")

    unsafe_report = report.model_copy(
        update={"overview": "<script>alert('model text')</script>"}
    )
    escaped = render_daily_report(
        unsafe_report,
        output_directory=tmp_path / "escaped-artifacts",
        settings=settings,
    )["html"].read_text(encoding="utf-8")
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped


def test_daily_cli_offline_e2e_and_second_run_reuses_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_root = tmp_path / "input"
    day = input_root / "2026" / "08" / "16"
    day.mkdir(parents=True)
    for stamp in ("20260816060000", "20260816143210"):
        (day / f"Security camera_00_{stamp}.mp4").write_bytes(b"fake mp4")

    output = tmp_path / "output"
    state = tmp_path / "state"
    temporary = tmp_path / "temp"
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "timezone: Asia/Tokyo",
                "paths:",
                f"  input: {input_root.as_posix()}",
                f"  output: {output.as_posix()}",
                f"  state: {state.as_posix()}",
                f"  temp: {temporary.as_posix()}",
                "input:",
                "  stable_seconds: 0",
                "  stability_recheck_sec: 0",
                "genai:",
                "  provider: mock",
                "  model: mock-observer-v1",
            ]
        ),
        encoding="utf-8",
    )

    calls = {"probe": 0, "extract": 0}

    def fake_probe(path: Path, **kwargs) -> ProbedVideoMetadata:
        calls["probe"] += 1
        return ProbedVideoMetadata(
            duration_sec=2, width=3840, height=2160, fps=25, codec="h264"
        )

    def fake_extract(video_path: Path, output_dir: Path, **kwargs):
        calls["extract"] += 1
        values = []
        for index in range(2):
            frame = Path(output_dir) / f"frame_{index:06d}.jpg"
            frame.write_bytes(b"jpeg")
            values.append(ExtractedFrame(frame, float(index), index))
        return values

    monkeypatch.setattr("app.event_analyzer.probe_video", fake_probe)
    monkeypatch.setattr("app.event_analyzer.extract_frames", fake_extract)
    monkeypatch.delenv("GENAI_PROVIDER", raising=False)
    monkeypatch.delenv("GENAI_MODEL", raising=False)

    assert main(["--config", str(config), "--date", "2026-08-16"]) == 0
    assert calls == {"probe": 2, "extract": 2}
    events = sorted((output / "2026-08-16" / "events").glob("*.json"))
    assert len(events) == 2
    assert (output / "2026-08-16" / "daily_report.json").is_file()
    assert (output / "2026-08-16" / "daily_report.md").is_file()
    assert (output / "2026-08-16" / "daily_report.html").is_file()

    # Exact fingerprint/model/prompt/schema cache prevents ffmpeg/VLM reuse.
    assert main(["--config", str(config), "--date", "2026-08-16"]) == 0
    assert calls == {"probe": 2, "extract": 2}
    report = json.loads(
        (output / "2026-08-16" / "daily_report.json").read_text(encoding="utf-8")
    )
    assert report["processing_summary"]["cache_reused"] == 2
    assert report["processing"]["cache_reused"] is True

    # The canonical filename is stable, but switching model and switching back
    # must not make an old SQLite key accept an artifact produced by another model.
    original_config = config.read_text(encoding="utf-8")
    config.write_text(
        original_config.replace("mock-observer-v1", "mock-observer-v2"),
        encoding="utf-8",
    )
    assert main(["--config", str(config), "--date", "2026-08-16"]) == 0
    assert calls == {"probe": 4, "extract": 4}

    config.write_text(original_config, encoding="utf-8")
    assert main(["--config", str(config), "--date", "2026-08-16"]) == 0
    assert calls == {"probe": 6, "extract": 6}
    final_event = json.loads(events[0].read_text(encoding="utf-8"))
    assert final_event["processing"]["model"] == "mock-observer-v1"


def test_one_video_failure_does_not_stop_remaining_files(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    bad = input_root / "bad_20260816060000.mp4"
    good = input_root / "good_20260816070000.mp4"
    bad.write_bytes(b"broken")
    good.write_bytes(b"video")
    settings = AppSettings.model_validate(
        {
            "paths": {
                "input": input_root,
                "output": tmp_path / "output",
                "state": tmp_path / "state",
                "temp": tmp_path / "temp",
            },
            "input": {"stable_seconds": 0, "stability_recheck_sec": 0},
            "genai": {"provider": "mock", "model": "mock-observer-v1"},
        }
    )
    settings.paths.output.mkdir()
    provider = MockProvider()

    class SometimesFailingAnalyzer:
        def analyze_file(
            self,
            video,
            *,
            event_id,
            source_file,
            recording_start,
            analysis_signature,
            source_fingerprint,
            expected_snapshot,
        ):
            if video.name.startswith("bad"):
                raise GenAIResponseError(
                    "synthetic corrupt video",
                    usage=APIUsage(input_tokens=3, output_tokens=1, request_count=2),
                )
            payload = _event(event_id).model_dump(mode="python")
            payload.update(
                source_file=source_file,
                recording_start=recording_start,
                recording_end=None,
            )
            payload["processing"]["analysis_signature"] = analysis_signature
            payload["processing"]["source_fingerprint"] = source_fingerprint
            return Event.model_validate(payload)

    events_dir = settings.paths.output / "2026-08-16" / "events"
    events_dir.mkdir(parents=True)
    with StateStore(
        settings.paths.state_database, output_root=settings.paths.output
    ) as state:
        result = _process_day(
            videos=[bad, good],
            target_date=date(2026, 8, 16),
            event_directory=events_dir,
            settings=settings,
            provider=provider,
            analyzer=SometimesFailingAnalyzer(),  # type: ignore[arg-type]
            state=state,
            force=False,
        )
        failed_records = state.list_records(status="failed")

    assert len(result.events) == 1
    assert result.events[0].source_file == good.name
    assert len(result.failures) == 1
    assert result.failures[0].source_file == bad.name
    assert "synthetic corrupt video" in result.failures[0].error
    assert result.new_usage.request_count == 2
    assert result.new_usage.input_tokens == 3
    assert len(failed_records) == 1
    assert failed_records[0].usage.request_count == 2
    assert failed_records[0].usage.output_tokens == 1
    assert len(list(events_dir.glob("*.json"))) == 1
