from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppSettings
from app.genai.base import GenAIProvider, StructuredResult
from app.models import APIUsage
from app.scene_author import (
    SceneAuthorSuggestion,
    SceneExampleObservation,
    SceneFeatureCandidate,
    _settings_without_scene,
    run_scene_author,
)
from app.video import ExtractedFrame, VideoMetadata


class SceneAuthorProvider(GenAIProvider):
    name = "scene-author-test"
    model = "scene-author-test-v1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def generate_structured(self, *, prompt, response_model, frames=()):
        self.calls.append((prompt, tuple(frames)))
        usage = APIUsage(
            input_tokens=10,
            output_tokens=5,
            request_count=1,
            estimated_cost=0.01,
            cost_currency="USD",
        )
        if response_model is SceneExampleObservation:
            number = sum(
                1 for _, prior_frames in self.calls[:-1] if prior_frames
            ) + 1
            return StructuredResult(
                value=SceneExampleObservation(
                    example_number=number,
                    summary=f"example {number}",
                    observed_features=[
                        "道路側から現れる",
                        "玄関付近に短時間立ち寄る",
                    ],
                    uncertainties=["携行物の種類は判別できない"],
                ),
                usage=usage,
                elapsed_sec=0.01,
            )
        if response_model is SceneAuthorSuggestion:
            return StructuredResult(
                value=SceneAuthorSuggestion(
                    label="ignored-by-caller",
                    common_features=[
                        SceneFeatureCandidate(
                            description="道路側から現れて玄関付近に短時間立ち寄る",
                            example_numbers=[1, 2],
                            rationale="両方の正例に共通する",
                        )
                    ],
                    supporting_features=[],
                    avoid_features=[
                        SceneFeatureCandidate(
                            description="携行物の具体的な種類",
                            example_numbers=[1, 2],
                            rationale="映像から安定して判別できない",
                        )
                    ],
                    suggested_scene_text=(
                        "新聞配達は道路側から現れ、玄関付近に短時間立ち寄って退出する。"
                    ),
                    cautions=["携行物の種類は必須条件にしない"],
                ),
                usage=usage,
                elapsed_sec=0.01,
            )
        raise AssertionError(response_model)


def test_settings_without_scene_never_reads_scene_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene_path = tmp_path / "scene.yaml"
    # Deliberately invalid YAML: the ordinary loader would fail if it opened this.
    scene_path.write_text("[", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "timezone: Asia/Tokyo",
                "scene_file: scene.yaml",
                "scene:",
                "  household: ${SCENE_ONLY_SECRET}",
                "genai:",
                "  provider: mock",
                "  model: mock-scene-author",
                "paths:",
                f"  temp: {tmp_path.as_posix()}/temp",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYZER_SCENE__NOTES", "environment-secret")

    settings = _settings_without_scene(config_path)

    assert settings.scene_file is None
    assert not settings.scene.is_configured
    assert settings.genai.provider == "mock"


def test_scene_author_observes_each_video_then_synthesizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    videos = (tmp_path / "clip-one.mp4", tmp_path / "clip-two.mp4")
    for video in videos:
        video.write_bytes(b"mp4")

    settings = AppSettings.model_validate(
        {
            "paths": {"temp": tmp_path / "temp"},
            "frames": {
                "max_long_edge_px": 1280,
                "jpeg_quality": 85,
            },
            "genai": {
                "provider": "mock",
                "model": "mock-scene-author",
                "max_images_per_request": 4,
                "max_inline_image_bytes": 100_000,
            },
        }
    )

    monkeypatch.setattr(
        "app.scene_author.probe_video",
        lambda *args, **kwargs: VideoMetadata(
            duration_sec=20.0,
            width=1920,
            height=1080,
            fps=25.0,
            codec="h264",
        ),
    )

    def fake_extract(video_path: Path, output_dir: Path, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for index in range(6):
            path = output_dir / f"frame_{index:06d}.jpg"
            path.write_bytes(b"jpeg" * 10)
            result.append(ExtractedFrame(path, float(index * 4), index))
        return result

    monkeypatch.setattr("app.scene_author.extract_frames", fake_extract)
    monkeypatch.setattr("app.scene_author.parse_recording_time", lambda *args: None)

    provider = SceneAuthorProvider()
    result = run_scene_author(
        label="新聞配達",
        videos=videos,
        settings=settings,
        provider=provider,
        frames_per_video=4,
    )

    assert len(result.observations) == 2
    assert result.suggestion.label == "新聞配達"
    assert "新聞配達" in result.suggestion.suggested_scene_text
    assert result.usage.request_count == 3
    assert result.usage.estimated_cost == pytest.approx(0.03)
    assert len(provider.calls) == 3
    assert len(provider.calls[0][1]) == 4
    assert len(provider.calls[1][1]) == 4
    assert provider.calls[2][1] == ()
    assert "clip-one.mp4" not in provider.calls[0][0]
    assert "clip-two.mp4" not in provider.calls[1][0]
    assert list((tmp_path / "temp").iterdir()) == []
