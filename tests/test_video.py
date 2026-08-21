from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.video import (
    DaylightFeatures,
    VideoProcessingError,
    extract_frames,
    file_is_stable,
    measure_daylight_features,
    parse_recording_time,
    probe_video,
    recording_end,
    snapshot_file,
)


def test_parse_recording_time_uses_first_valid_14_digit_timestamp() -> None:
    value = parse_recording_time(
        "RecM01_20261340112233_20260816143210_camera.mp4"
    )

    assert value == datetime(2026, 8, 16, 14, 32, 10, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert value.utcoffset() == timedelta(hours=9)


def test_parse_recording_time_requires_a_standalone_valid_timestamp() -> None:
    assert parse_recording_time("video_1202608161432109.mp4") is None
    assert parse_recording_time("video_without_time.mp4") is None


def test_unknown_recording_time_does_not_fall_back_to_mtime(tmp_path: Path) -> None:
    video = tmp_path / "unknown.mp4"
    video.write_bytes(b"video")
    known_mtime = datetime(2020, 1, 2, tzinfo=ZoneInfo("Asia/Tokyo")).timestamp()
    os.utime(video, (known_mtime, known_mtime))

    assert parse_recording_time(video) is None


def test_recording_end_is_derived_only_when_start_is_known() -> None:
    start = datetime(2026, 8, 16, 14, 32, 10, tzinfo=ZoneInfo("Asia/Tokyo"))

    assert recording_end(start, 91.2) == start + timedelta(seconds=91.2)
    assert recording_end(None, 91.2) is None


def test_file_stability_uses_nonempty_size_and_mtime_age(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"complete")
    os.utime(video, (1_000, 1_000))

    assert file_is_stable(video, 120, now=1_120)
    assert not file_is_stable(video, 120, now=1_119.999)

    empty = tmp_path / "empty.mp4"
    empty.touch()
    os.utime(empty, (1_000, 1_000))
    assert not file_is_stable(empty, 0, now=1_000)


def test_file_stability_can_compare_a_prior_size_and_mtime_snapshot(
    tmp_path: Path,
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"first")
    os.utime(video, ns=(1_000_000_000_000, 1_000_000_000_000))
    before = snapshot_file(video)

    assert file_is_stable(video, 100, now=1_100, previous_snapshot=before)

    video.write_bytes(b"changed")
    os.utime(video, ns=(1_001_000_000_000, 1_001_000_000_000))
    assert not file_is_stable(video, 0, now=1_100, previous_snapshot=before)
    assert not file_is_stable(tmp_path / "gone.mp4", 0, now=1_100)


def test_file_stability_rejects_naive_datetime(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"complete")

    with pytest.raises(ValueError, match="timezone-aware"):
        file_is_stable(video, 0, now=datetime(2026, 8, 16))


def test_probe_video_parses_ffprobe_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "streams": [
            {
                "width": 3840,
                "height": 2160,
                "codec_name": "h264",
                "avg_frame_rate": "30000/1001",
                "r_frame_rate": "30/1",
                "duration": "91.100",
            }
        ],
        "format": {"duration": "91.200000"},
    }
    seen: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        seen.extend(command)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    metadata = probe_video("camera.mp4")

    assert metadata.duration_sec == pytest.approx(91.2)
    assert metadata.duration == pytest.approx(91.2)
    assert metadata.resolution == (3840, 2160)
    assert metadata.fps == pytest.approx(30000 / 1001)
    assert metadata.codec == "h264"
    assert seen[0] == "ffprobe"
    assert seen[-1] == "camera.mp4"
    assert "-select_streams" in seen


def test_probe_video_falls_back_to_stream_duration_and_r_frame_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "streams": [
            {
                "width": 1920,
                "height": 1080,
                "codec_name": "hevc",
                "avg_frame_rate": "0/0",
                "r_frame_rate": "25/1",
                "duration": "2.5",
            }
        ],
        "format": {"duration": "N/A"},
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr=""
        ),
    )

    metadata = probe_video("camera.mp4")

    assert metadata.duration_sec == 2.5
    assert metadata.fps == 25
    assert metadata.codec == "hevc"


def test_probe_video_wraps_command_failure_with_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="moov atom not found\ninvalid data",
        ),
    )

    with pytest.raises(VideoProcessingError) as error:
        probe_video("broken.mp4")

    assert error.value.operation == "ffprobe"
    assert error.value.returncode == 1
    assert "moov atom not found invalid data" in str(error.value)


def test_extract_frames_builds_portable_filter_and_returns_time_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "frames"
    seen: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        seen.extend(command)
        output_pattern = Path(command[-1])
        # Deliberately create out of order; the function must sort numerically.
        for number in (2, 0, 1):
            Path(str(output_pattern).replace("%06d", f"{number:06d}")).write_bytes(
                b"jpeg"
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    frames = extract_frames(
        "camera.mp4",
        destination,
        interval_sec=2,
        max_long_edge_px=1280,
        jpeg_quality=85,
    )

    assert [frame.path.name for frame in frames] == [
        "frame_000000.jpg",
        "frame_000001.jpg",
        "frame_000002.jpg",
    ]
    assert [frame.timestamp_sec for frame in frames] == [0.0, 2.0, 4.0]
    assert [frame.index for frame in frames] == [0, 1, 2]
    assert seen[0] == "ffmpeg"
    assert seen[seen.index("-start_number") + 1] == "0"
    video_filter = seen[seen.index("-vf") + 1]
    assert video_filter.startswith(
        "fps=fps=1/2:start_time=0:round=near:eof_action=pass,"
    )
    assert "min(iw,1280)" in video_filter
    assert "min(ih,1280)" in video_filter
    assert "flags=lanczos" in video_filter
    assert seen[seen.index("-q:v") + 1] == "6"


def test_measure_daylight_features_uses_leading_tiny_rgb_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    # Three identical, clearly lit and colorful 2x1 frames.
    raw_frame = bytes((80, 160, 80)) * 2

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        seen.extend(command)
        assert kwargs["text"] is False
        return SimpleNamespace(
            returncode=0,
            stdout=raw_frame * 3,
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    features = measure_daylight_features(
        "camera.mp4",
        sample_frames=3,
        sample_width=2,
        sample_height=1,
    )

    assert features.sampled_frames == 3
    assert features.median_saturation == pytest.approx(127.5)
    assert features.dark_ratio == 0
    assert not features.decoder_warning
    assert features.is_daylike(saturation_threshold=20, dark_ratio_threshold=0.2)
    assert seen[seen.index("-frames:v") + 1] == "3"
    assert seen[seen.index("-pix_fmt") + 1] == "rgb24"
    video_filter = seen[seen.index("-vf") + 1]
    assert "fps=1:start_time=0" in video_filter
    assert "scale=2:1" in video_filter


def test_measure_daylight_features_keeps_decoder_warning_on_safe_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_frame = bytes((80, 160, 80)) * 2
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=raw_frame,
            stderr=b"[hevc @ 0001] PPS changed between slices",
        ),
    )

    features = measure_daylight_features(
        "damaged.mp4",
        sample_frames=1,
        sample_width=2,
        sample_height=1,
    )

    assert features.decoder_warning
    assert not features.is_daylike(saturation_threshold=20, dark_ratio_threshold=0.2)


def test_measure_daylight_features_reports_ffmpeg_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"invalid video data",
        ),
    )

    with pytest.raises(VideoProcessingError, match="invalid video data"):
        measure_daylight_features("broken.mp4")


@pytest.mark.parametrize(
    ("features", "expected"),
    [
        (DaylightFeatures(5, 20.0, 0.1), False),
        (DaylightFeatures(5, 21.0, 0.2), False),
        (DaylightFeatures(5, 21.0, 0.199), True),
        (DaylightFeatures(5, 21.0, 0.1, decoder_warning=True), False),
    ],
)
def test_daylike_thresholds_are_strict(
    features: DaylightFeatures,
    expected: bool,
) -> None:
    assert (
        features.is_daylike(saturation_threshold=20, dark_ratio_threshold=0.2)
        is expected
    )


def test_extract_frames_rejects_stale_output(tmp_path: Path) -> None:
    destination = tmp_path / "frames"
    destination.mkdir()
    (destination / "frame_000000.jpg").write_bytes(b"stale")

    with pytest.raises(ValueError, match="already contains"):
        extract_frames("camera.mp4", destination)


def test_extract_frames_reports_ffmpeg_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=183, stdout="", stderr="Error while decoding stream #0:0"
        ),
    )

    with pytest.raises(VideoProcessingError) as error:
        extract_frames("broken.mp4", tmp_path / "frames")

    assert error.value.operation == "ffmpeg"
    assert error.value.returncode == 183
    assert "Error while decoding stream #0:0" in str(error.value)


def test_extract_frames_raises_when_ffmpeg_produces_no_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    with pytest.raises(VideoProcessingError, match="no frames were produced"):
        extract_frames("empty.mp4", tmp_path / "frames")


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"interval_sec": 0}, "interval_sec"),
        ({"max_long_edge_px": 0}, "max_long_edge_px"),
        ({"jpeg_quality": 101}, "jpeg_quality"),
    ],
)
def test_extract_frames_validates_settings(
    tmp_path: Path,
    kwargs: dict[str, object],
    expected: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=expected):
        extract_frames("camera.mp4", tmp_path / "frames", **kwargs)
