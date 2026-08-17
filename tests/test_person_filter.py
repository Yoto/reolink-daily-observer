from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Sequence

import pytest

from app.config import AppSettings
from app.models import APIUsage, FrameFilterMetadata, FrameFilterStatus
from app.person_filter import (
    FrameDetection,
    PersonDetectionError,
    PersonDetector,
    PersonFrameFilter,
    SelectionRules,
    merge_runs,
    person_runs,
    score_distribution,
    select_frame_indices,
)
from app.run_daily import _cache_prompt_version, main
from app.state import CacheKey, FileFingerprint, StateStore
from app.video import ExtractedFrame, VideoMetadata as ProbedVideoMetadata


DAY = 0.4
NIGHT = 0.01


class StubDetector(PersonDetector):
    """Returns pre-set scores so the selection rules can be tested alone."""

    name = "stub"
    model = "stub-detector-v1"

    def __init__(
        self,
        scores: Sequence[float],
        *,
        saturation: float = DAY,
        error: Exception | None = None,
    ) -> None:
        self.scores = list(scores)
        self.saturation = saturation
        self.error = error
        self.calls = 0
        self.closed = False

    def detect(self, frames: Sequence[Path]) -> tuple[FrameDetection, ...]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return tuple(
            FrameDetection(score=score, saturation=self.saturation)
            for score in self.scores[: len(frames)]
        )

    def close(self) -> None:
        self.closed = True


def _rules(
    *,
    threshold: float = 0.2,
    merge_gap_frames: int = 3,
    padding_frames: int = 2,
    keep_first_last: bool = True,
) -> SelectionRules:
    return SelectionRules(
        threshold=threshold,
        merge_gap_frames=merge_gap_frames,
        padding_frames=padding_frames,
        keep_first_last=keep_first_last,
    )


# --- selection rules -------------------------------------------------------


def test_every_frame_with_a_person_is_kept() -> None:
    scores = [0.9] * 6

    assert select_frame_indices(scores, _rules()) == (0, 1, 2, 3, 4, 5)


def test_no_detection_keeps_only_the_first_and_last_frame() -> None:
    scores = [0.0] * 6

    assert select_frame_indices(scores, _rules()) == (0, 5)


def test_no_detection_selects_nothing_when_first_last_is_disabled() -> None:
    scores = [0.0] * 6

    assert select_frame_indices(scores, _rules(keep_first_last=False)) == ()


def test_a_short_detection_gap_does_not_split_the_sequence() -> None:
    # Three seconds of misses in the middle of one approach: the walk between
    # the two detected stretches has to survive.
    scores = [0.9, 0.9, 0.05, 0.05, 0.05, 0.9, 0.9]

    kept = select_frame_indices(
        scores, _rules(merge_gap_frames=3, padding_frames=0, keep_first_last=False)
    )

    assert kept == (0, 1, 2, 3, 4, 5, 6)


def test_a_long_detection_gap_stays_two_separate_stretches() -> None:
    scores = [0.9, 0.9, 0.0, 0.0, 0.0, 0.0, 0.9, 0.9]

    kept = select_frame_indices(
        scores, _rules(merge_gap_frames=3, padding_frames=0, keep_first_last=False)
    )

    assert kept == (0, 1, 6, 7)


def test_padding_keeps_the_approach_and_the_departure() -> None:
    scores = [0.0] * 4 + [0.9] + [0.0] * 5

    kept = select_frame_indices(
        scores, _rules(merge_gap_frames=0, padding_frames=2, keep_first_last=False)
    )

    assert kept == (2, 3, 4, 5, 6)


def test_padding_is_clamped_to_the_video(
) -> None:
    scores = [0.9] + [0.0] * 3 + [0.9]

    kept = select_frame_indices(
        scores, _rules(merge_gap_frames=0, padding_frames=5, keep_first_last=False)
    )

    assert kept == (0, 1, 2, 3, 4)


def test_a_person_only_at_the_start_still_keeps_the_final_frame() -> None:
    scores = [0.9, 0.9] + [0.0] * 8

    kept = select_frame_indices(scores, _rules(merge_gap_frames=0, padding_frames=2))

    # 0..3 is the detection plus its trailing padding; 9 is the unconditional
    # last frame, which is where a delivered parcel would be visible.
    assert kept == (0, 1, 2, 3, 9)


def test_a_person_only_at_the_end_still_keeps_the_first_frame() -> None:
    scores = [0.0] * 8 + [0.9, 0.9]

    kept = select_frame_indices(scores, _rules(merge_gap_frames=0, padding_frames=2))

    assert kept == (0, 6, 7, 8, 9)


def test_an_empty_score_sequence_selects_nothing() -> None:
    assert select_frame_indices([], _rules()) == ()


def test_runs_and_merges_are_inclusive_ranges() -> None:
    scores = [0.0, 0.5, 0.5, 0.0, 0.0, 0.5]

    runs = person_runs(scores, 0.2)

    assert runs == [(1, 2), (5, 5)]
    assert merge_runs(runs, 2) == [(1, 5)]
    assert merge_runs(runs, 1) == [(1, 2), (5, 5)]


def test_seconds_are_rounded_up_to_whole_frames() -> None:
    rules = SelectionRules.from_seconds(
        threshold=0.2,
        merge_gap_sec=3,
        padding_sec=2,
        keep_first_last=True,
        interval_sec=2.0,
    )

    # A 2s sample interval cannot express a 3s gap exactly; shortening it would
    # silently make the configured window narrower than it reads.
    assert rules.merge_gap_frames == 2
    assert rules.padding_frames == 1


def test_selection_rules_reject_impossible_values() -> None:
    with pytest.raises(ValueError, match="threshold"):
        SelectionRules(threshold=1.5, merge_gap_frames=0, padding_frames=0)
    with pytest.raises(ValueError, match="padding_frames"):
        SelectionRules(threshold=0.2, merge_gap_frames=0, padding_frames=-1)
    with pytest.raises(ValueError, match="interval_sec"):
        SelectionRules.from_seconds(
            threshold=0.2,
            merge_gap_sec=3,
            padding_sec=2,
            keep_first_last=True,
            interval_sec=0,
        )


def test_score_distribution_uses_fixed_bins() -> None:
    distribution = score_distribution([0.0, 0.05, 0.5, 0.95, 1.0])

    assert distribution.count == 5
    assert distribution.max_score == 1.0
    assert distribution.median_score == 0.5
    assert sum(distribution.histogram) == 5
    assert distribution.histogram[0] == 2
    assert distribution.histogram[5] == 1
    # 1.0 belongs to the top bin rather than falling off the end.
    assert distribution.histogram[9] == 2
    assert score_distribution([]).count == 0


# --- the filter stage ------------------------------------------------------


def _settings(tmp_path: Path, **person_filter: object) -> AppSettings:
    return AppSettings.model_validate(
        {
            "paths": {"temp": tmp_path / "temp"},
            "person_filter": {"enabled": True, "dry_run": False, **person_filter},
        }
    )


def _frames(tmp_path: Path, count: int) -> list[ExtractedFrame]:
    directory = tmp_path / "frames"
    directory.mkdir(exist_ok=True)
    values = []
    for index in range(count):
        path = directory / f"frame_{index:06d}.jpg"
        path.write_bytes(b"jpeg")
        values.append(ExtractedFrame(path=path, timestamp_sec=float(index), index=index))
    return values


def test_the_filter_sends_only_the_selected_frames(tmp_path: Path) -> None:
    scores = [0.0] * 5 + [0.9] * 2 + [0.0] * 5
    frames = _frames(tmp_path, len(scores))
    detector = StubDetector(scores)

    outcome = PersonFrameFilter(
        _settings(tmp_path, merge_gap_sec=0, padding_sec=1), detector
    ).select(frames)

    assert [frame.index for frame in outcome.frames] == [0, 4, 5, 6, 7, 11]
    assert outcome.metadata.status == FrameFilterStatus.APPLIED
    assert outcome.metadata.frames_extracted == 12
    assert outcome.metadata.frames_selected == 6
    assert outcome.metadata.frames_sent == 6
    assert outcome.metadata.reduction_ratio == pytest.approx(0.5)
    assert outcome.metadata.detector == "stub"
    assert outcome.metadata.model == "stub-detector-v1"
    assert outcome.metadata.detection_sec is not None
    assert sum(outcome.metadata.score_histogram) == 12


def test_dry_run_reports_the_saving_without_dropping_a_frame(tmp_path: Path) -> None:
    scores = [0.0] * 5 + [0.9] * 2 + [0.0] * 5
    frames = _frames(tmp_path, len(scores))
    detector = StubDetector(scores)

    outcome = PersonFrameFilter(
        _settings(tmp_path, dry_run=True, merge_gap_sec=0, padding_sec=1), detector
    ).select(frames)

    assert [frame.index for frame in outcome.frames] == list(range(12))
    assert outcome.metadata.status == FrameFilterStatus.DRY_RUN
    assert outcome.metadata.frames_sent == 12
    # The selection is still measured, which is the whole point of the mode.
    assert outcome.metadata.frames_selected == 6
    assert outcome.metadata.reduction_ratio == pytest.approx(0.5)


def test_a_disabled_filter_sends_every_frame_without_detecting(tmp_path: Path) -> None:
    frames = _frames(tmp_path, 4)
    detector = StubDetector([0.0] * 4)
    settings = AppSettings.model_validate(
        {"paths": {"temp": tmp_path / "temp"}, "person_filter": {"enabled": False}}
    )

    outcome = PersonFrameFilter(settings, detector).select(frames)

    assert detector.calls == 0
    assert len(outcome.frames) == 4
    assert outcome.metadata.status == FrameFilterStatus.DISABLED
    assert outcome.metadata.frames_selected == 4
    assert outcome.metadata.reduction_ratio == 0.0


def test_a_detection_failure_sends_every_frame_and_is_recorded(tmp_path: Path) -> None:
    frames = _frames(tmp_path, 4)
    detector = StubDetector([], error=PersonDetectionError("inference exploded"))

    outcome = PersonFrameFilter(_settings(tmp_path), detector).select(frames)

    # Failing open costs money; failing closed costs evidence that cannot be
    # recovered, and would leave no trace in the report.
    assert len(outcome.frames) == 4
    assert outcome.metadata.status == FrameFilterStatus.FAILED
    assert "inference exploded" in (outcome.metadata.error or "")
    assert outcome.metadata.frames_sent == 4


def test_a_short_result_from_the_detector_is_treated_as_a_failure(
    tmp_path: Path,
) -> None:
    frames = _frames(tmp_path, 4)
    detector = StubDetector([0.9, 0.9])

    outcome = PersonFrameFilter(_settings(tmp_path), detector).select(frames)

    assert outcome.metadata.status == FrameFilterStatus.FAILED
    assert len(outcome.frames) == 4


def test_monochrome_footage_switches_to_the_night_threshold(tmp_path: Path) -> None:
    scores = [0.15] * 6
    frames = _frames(tmp_path, len(scores))
    settings = _settings(
        tmp_path,
        score_threshold_day=0.20,
        score_threshold_night=0.10,
        merge_gap_sec=0,
        padding_sec=0,
        keep_first_last_frame=False,
    )

    daylight = PersonFrameFilter(settings, StubDetector(scores, saturation=DAY)).select(
        frames
    )
    infrared = PersonFrameFilter(
        settings, StubDetector(scores, saturation=NIGHT)
    ).select(frames)

    assert daylight.metadata.lighting == "day"
    assert daylight.metadata.score_threshold == 0.20
    assert daylight.metadata.frames_selected == 1  # the guard keeps one frame
    assert infrared.metadata.lighting == "night"
    assert infrared.metadata.score_threshold == 0.10
    assert infrared.metadata.frames_selected == 6


def test_at_least_one_frame_is_always_sent(tmp_path: Path) -> None:
    frames = _frames(tmp_path, 5)
    detector = StubDetector([0.0] * 5)

    outcome = PersonFrameFilter(
        _settings(tmp_path, keep_first_last_frame=False), detector
    ).select(frames)

    # An event cannot be produced from nothing, so the first frame survives
    # even when the rules select none.
    assert [frame.index for frame in outcome.frames] == [0]


def test_closing_the_filter_releases_the_detector(tmp_path: Path) -> None:
    detector = StubDetector([0.9])
    frame_filter = PersonFrameFilter(_settings(tmp_path), detector)

    frame_filter.close()

    assert detector.closed is True


# --- cache invalidation ----------------------------------------------------


def test_changing_a_filter_setting_invalidates_cached_events() -> None:
    baseline = AppSettings.model_validate({"person_filter": {"enabled": True}})
    signature = _cache_prompt_version(baseline)

    for change in (
        {"score_threshold_day": 0.5},
        {"score_threshold_night": 0.5},
        {"merge_gap_sec": 10},
        {"padding_sec": 10},
        {"keep_first_last_frame": False},
        {"dry_run": False},
        {"enabled": False},
    ):
        altered = AppSettings.model_validate(
            {"person_filter": {"enabled": True, **change}}
        )
        assert _cache_prompt_version(altered) != signature, change


def test_settings_that_cannot_change_the_selection_reuse_the_cache() -> None:
    baseline = AppSettings.model_validate({"person_filter": {"enabled": True}})
    tuned = AppSettings.model_validate(
        {"person_filter": {"enabled": True, "batch_size": 2, "threads": 4}}
    )

    assert _cache_prompt_version(tuned) == _cache_prompt_version(baseline)


def test_a_disabled_filter_does_not_depend_on_its_own_thresholds() -> None:
    disabled = AppSettings.model_validate({"person_filter": {"enabled": False}})
    retuned = AppSettings.model_validate(
        {"person_filter": {"enabled": False, "score_threshold_day": 0.9}}
    )

    assert _cache_prompt_version(retuned) == _cache_prompt_version(disabled)


# --- recorded measurements -------------------------------------------------


def _fingerprint() -> FileFingerprint:
    return FileFingerprint(
        source_path="2026-08-16/clip.mp4", size_bytes=100, mtime_ns=123
    )


def _cache_key() -> CacheKey:
    return CacheKey(
        fingerprint=_fingerprint(),
        provider="mock",
        model="mock-observer-v1",
        prompt_version="event_observation_v2-abc123",
        schema_version="1.1",
    )


def test_the_state_record_keeps_the_filter_accounting(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "event.json").write_text("{}", encoding="utf-8")
    metadata = FrameFilterMetadata(
        status=FrameFilterStatus.DRY_RUN,
        detector="huggingface",
        model="hustvl/yolos-tiny",
        lighting="night",
        saturation=0.01,
        score_threshold=0.1,
        frames_extracted=60,
        frames_selected=24,
        frames_sent=60,
        reduction_ratio=0.6,
        score_max=0.93,
        score_median=0.02,
        score_histogram=[36, 0, 0, 0, 0, 0, 0, 0, 4, 20],
        detection_sec=4.5,
    )

    with StateStore(tmp_path / "state.sqlite", output_root=output) as state:
        claim = state.claim_processing(_cache_key(), event_id="20260816-abc")
        state.mark_completed(
            claim.record.id,
            event_json_path="event.json",
            usage=APIUsage(input_tokens=10, output_tokens=5, request_count=1),
            frame_filter=metadata,
        )
        reloaded = state.get_record(claim.record.id)
        row = state._connection.execute(  # noqa: SLF001 - checks the queryable columns
            "SELECT frames_extracted, frames_selected, frames_sent, filter_status,"
            " filter_lighting, filter_reduction_ratio, filter_detection_sec"
            " FROM processing_records WHERE id = ?",
            (claim.record.id,),
        ).fetchone()

    assert reloaded.frame_filter == metadata
    # The scalar columns exist so a month of runs can be aggregated in SQL
    # without unpacking JSON for every row.
    assert row["frames_extracted"] == 60
    assert row["frames_selected"] == 24
    assert row["frames_sent"] == 60
    assert row["filter_status"] == "dry_run"
    assert row["filter_lighting"] == "night"
    assert row["filter_reduction_ratio"] == pytest.approx(0.6)
    assert row["filter_detection_sec"] == pytest.approx(4.5)


def test_reprocessing_clears_the_previous_filter_measurements(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    (output / "event.json").write_text("{}", encoding="utf-8")
    key = _cache_key()

    with StateStore(tmp_path / "state.sqlite", output_root=output) as state:
        claim = state.claim_processing(key, event_id="20260816-abc")
        state.mark_completed(
            claim.record.id,
            event_json_path="event.json",
            frame_filter=FrameFilterMetadata(
                status=FrameFilterStatus.APPLIED,
                frames_extracted=60,
                frames_selected=24,
                frames_sent=24,
                reduction_ratio=0.6,
            ),
        )
        reclaimed = state.claim_processing(key, event_id="20260816-abc", force=True)

    assert reclaimed.record.frame_filter == FrameFilterMetadata()


def test_an_existing_database_gains_the_filter_columns(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    output = tmp_path / "output"
    output.mkdir()
    # A version 1 database, written before the filter existed.
    legacy = sqlite3.connect(database)
    legacy.executescript(
        """
        CREATE TABLE processing_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            source_filename TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            event_id TEXT,
            recording_time TEXT,
            status TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            event_json_path TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            request_count INTEGER NOT NULL DEFAULT 0,
            api_processing_sec REAL,
            estimated_cost REAL,
            cost_currency TEXT,
            usage_json TEXT NOT NULL DEFAULT '{}',
            error_type TEXT,
            error_message TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            processed_at TEXT,
            UNIQUE (
                source_path, size_bytes, mtime_ns, content_hash,
                provider, model, prompt_version, schema_version
            )
        );
        PRAGMA user_version = 1;
        """
    )
    legacy.execute(
        """
        INSERT INTO processing_records (
            source_path, source_filename, size_bytes, mtime_ns, status,
            provider, model, prompt_version, schema_version,
            created_at, updated_at
        ) VALUES (
            '2026-08-16/clip.mp4', 'clip.mp4', 100, 123, 'completed',
            'mock', 'mock-observer-v1', 'event_observation_v2', '1.1',
            '2026-08-16T00:00:00+00:00', '2026-08-16T00:00:00+00:00'
        )
        """
    )
    legacy.commit()
    legacy.close()

    # Opening it must migrate in place rather than discard an existing cache.
    with StateStore(database, output_root=output) as state:
        records = state.list_records()

    assert len(records) == 1
    assert records[0].frame_filter == FrameFilterMetadata()


def test_the_mock_pipeline_still_runs_with_the_filter_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    input_root = tmp_path / "input"
    day = input_root / "2026" / "08" / "16"
    day.mkdir(parents=True)
    (day / "Security camera_00_20260816143210.mp4").write_bytes(b"fake mp4")
    output = tmp_path / "output"
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "timezone: Asia/Tokyo",
                "paths:",
                f"  input: {input_root.as_posix()}",
                f"  output: {output.as_posix()}",
                f"  state: {(tmp_path / 'state').as_posix()}",
                f"  temp: {(tmp_path / 'temp').as_posix()}",
                "input:",
                "  stable_seconds: 0",
                "  stability_recheck_sec: 0",
                "person_filter:",
                "  enabled: true",
                "  dry_run: false",
                "  merge_gap_sec: 0",
                "  padding_sec: 0",
                "genai:",
                "  provider: mock",
                "  model: mock-observer-v1",
            ]
        ),
        encoding="utf-8",
    )

    def fake_extract(video_path: Path, output_dir: Path, **kwargs: object):
        values = []
        for index in range(6):
            frame = Path(output_dir) / f"frame_{index:06d}.jpg"
            frame.write_bytes(b"jpeg")
            values.append(ExtractedFrame(frame, float(index), index))
        return values

    monkeypatch.setattr(
        "app.event_analyzer.probe_video",
        lambda path, **kwargs: ProbedVideoMetadata(
            duration_sec=6, width=3840, height=2160, fps=25, codec="h264"
        ),
    )
    monkeypatch.setattr("app.event_analyzer.extract_frames", fake_extract)
    # Only the middle two frames contain a person.
    monkeypatch.setattr(
        "app.person_filter.filter.create_detector",
        lambda settings: StubDetector([0.0, 0.0, 0.9, 0.9, 0.0, 0.0]),
    )
    monkeypatch.delenv("GENAI_PROVIDER", raising=False)
    monkeypatch.delenv("GENAI_MODEL", raising=False)

    assert main(["--config", str(config), "--date", "2026-08-16"]) == 0

    event_files = sorted((output / "2026-08-16" / "events").glob("*.json"))
    assert len(event_files) == 1
    payload = json.loads(event_files[0].read_text(encoding="utf-8"))
    frame_filter = payload["processing"]["frame_filter"]
    assert frame_filter["status"] == "applied"
    assert frame_filter["frames_extracted"] == 6
    # Frames 2 and 3 plus the unconditional first and last frame.
    assert frame_filter["frames_selected"] == 4
    assert frame_filter["frames_sent"] == 4
    assert payload["processing"]["frames_analyzed"] == 4
    assert (output / "2026-08-16" / "daily_report.html").is_file()


