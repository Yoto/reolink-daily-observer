"""Offline helper that turns labeled camera clips into scene-writing suggestions.

This command intentionally does not load config/scene.yaml and never edits it.
It sends selected frames from labeled examples to the configured GenAI provider,
then synthesizes a human-reviewable suggestion for manual scene maintenance.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml
from pydantic import Field

from app.config import AppSettings, SettingsError, _apply_env_overrides, _substitute_env
from app.genai import create_provider
from app.genai.base import FrameInput, GenAIProvider
from app.models import APIUsage, NonEmptyText, SchemaModel
from app.video import ExtractedFrame, extract_frames, parse_recording_time, probe_video


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OBSERVE_PROMPT = Path("prompts/scene_author_observe_v1.txt")
SYNTHESIZE_PROMPT = Path("prompts/scene_author_synthesize_v1.txt")
SCENE_AUTHOR_VERSION = "scene_author_v1"


class SceneExampleObservation(SchemaModel):
    example_number: int = Field(ge=1)
    summary: NonEmptyText
    observed_features: list[NonEmptyText] = Field(default_factory=list)
    uncertainties: list[NonEmptyText] = Field(default_factory=list)


class SceneFeatureCandidate(SchemaModel):
    description: NonEmptyText
    example_numbers: list[int] = Field(default_factory=list)
    rationale: NonEmptyText


class SceneAuthorSuggestion(SchemaModel):
    label: NonEmptyText
    common_features: list[SceneFeatureCandidate] = Field(default_factory=list)
    supporting_features: list[SceneFeatureCandidate] = Field(default_factory=list)
    avoid_features: list[SceneFeatureCandidate] = Field(default_factory=list)
    suggested_scene_text: NonEmptyText
    cautions: list[NonEmptyText] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SceneAuthorResult:
    suggestion: SceneAuthorSuggestion
    observations: tuple[SceneExampleObservation, ...]
    usage: APIUsage
    provider: str
    model: str
    version: str = SCENE_AUTHOR_VERSION


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    _configure_logging()
    parser = argparse.ArgumentParser(
        prog="scene-author",
        description=(
            "Derive a human-reviewable scene.yaml suggestion from labeled camera clips. "
            "The current scene is never loaded or modified."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    suggest = commands.add_parser(
        "suggest",
        help="analyze two or more positive examples and suggest a routine description",
    )
    suggest.add_argument("label", help="known ground-truth label, e.g. 新聞配達")
    suggest.add_argument("videos", nargs="+", type=Path)
    suggest.add_argument("--config", type=Path, help="YAML configuration path")
    suggest.add_argument(
        "--frames-per-video",
        type=int,
        default=12,
        help="maximum sampled frames sent for each clip (default: 12)",
    )
    suggest.add_argument(
        "--max-long-edge-px",
        type=int,
        help="analysis frame long edge; default is frames.max_long_edge_px",
    )
    suggest.add_argument(
        "--json-output",
        type=Path,
        help="also write structured observations, suggestion, and usage as JSON",
    )
    args = parser.parse_args(arguments)

    if args.frames_per_video < 2:
        parser.error("--frames-per-video must be at least 2")
    if args.max_long_edge_px is not None and args.max_long_edge_px < 1:
        parser.error("--max-long-edge-px must be positive")
    if len(args.videos) < 2:
        parser.error("scene-author suggest requires at least two labeled example videos")

    try:
        settings = _settings_without_scene(args.config)
        videos = tuple(path.resolve(strict=True) for path in args.videos)
        provider = create_provider(settings)
        try:
            result = run_scene_author(
                label=args.label,
                videos=videos,
                settings=settings,
                provider=provider,
                frames_per_video=args.frames_per_video,
                max_long_edge_px=args.max_long_edge_px,
            )
        finally:
            provider.close()
    except KeyboardInterrupt:
        LOGGER.error("interrupted")
        return 130
    except Exception:
        LOGGER.exception("scene-author failed")
        return 1

    print(render_result(result))
    if args.json_output is not None:
        write_json_result(args.json_output, result)
        print(f"\nJSON: {args.json_output}")
    return 0


def run_scene_author(
    *,
    label: str,
    videos: Sequence[Path],
    settings: AppSettings,
    provider: GenAIProvider,
    frames_per_video: int = 12,
    max_long_edge_px: int | None = None,
) -> SceneAuthorResult:
    clean_label = " ".join(label.split())
    if not clean_label:
        raise ValueError("label must not be empty")
    if len(videos) < 2:
        raise ValueError("at least two labeled examples are required")
    if frames_per_video < 2:
        raise ValueError("frames_per_video must be at least 2")

    max_edge = max_long_edge_px or settings.frames.max_long_edge_px
    if max_edge < 1:
        raise ValueError("max_long_edge_px must be positive")

    temp_root = settings.paths.temp
    temp_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="scene-author-", dir=str(temp_root)))
    observations: list[SceneExampleObservation] = []
    usage = APIUsage()
    started = time.perf_counter()
    try:
        for example_number, video in enumerate(videos, start=1):
            metadata = probe_video(
                video, timeout_sec=settings.input.ffprobe_timeout_sec
            )
            extraction_interval = _sampling_interval(
                metadata.duration_sec, frames_per_video
            )
            example_dir = workspace / f"example-{example_number:02d}"
            frames = extract_frames(
                video,
                example_dir,
                interval_sec=extraction_interval,
                max_long_edge_px=max_edge,
                jpeg_quality=settings.frames.jpeg_quality,
                timeout_sec=settings.frames.ffmpeg_timeout_sec,
            )
            selected = _select_frames(
                frames,
                max_images=_effective_image_limit(settings, frames_per_video),
                max_raw_bytes=settings.genai.max_inline_image_bytes,
            )
            if not selected:
                raise ValueError(f"no frames extracted from example {example_number}")

            recording_start = parse_recording_time(video.name, settings.timezone)
            prompt = _read_prompt(OBSERVE_PROMPT).format(
                label=clean_label,
                example_number=example_number,
                recording_start_local=(
                    recording_start.isoformat() if recording_start is not None else "unknown"
                ),
                duration_sec=f"{metadata.duration_sec:.3f}",
            )
            generated = provider.generate_structured(
                prompt=prompt,
                response_model=SceneExampleObservation,
                frames=tuple(
                    FrameInput(path=frame.path, timestamp_sec=frame.timestamp_sec)
                    for frame in selected
                ),
            )
            observation = generated.value.model_copy(
                update={"example_number": example_number}
            )
            observations.append(observation)
            usage = usage.plus(generated.usage)
            LOGGER.info(
                "scene-author example complete index=%d/%d frames=%d duration_sec=%.3f",
                example_number,
                len(videos),
                len(selected),
                metadata.duration_sec,
            )

        synthesis_prompt = _read_prompt(SYNTHESIZE_PROMPT).format(
            label=clean_label,
            example_count=len(observations),
            observations_json=json.dumps(
                [item.model_dump(mode="json") for item in observations],
                ensure_ascii=False,
                indent=2,
            ),
        )
        synthesized = provider.generate_structured(
            prompt=synthesis_prompt,
            response_model=SceneAuthorSuggestion,
        )
        suggestion = synthesized.value.model_copy(update={"label": clean_label})
        usage = usage.plus(synthesized.usage)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    LOGGER.info(
        "scene-author complete examples=%d requests=%d estimated_cost=%s elapsed_sec=%.3f",
        len(observations),
        usage.request_count,
        usage.estimated_cost,
        time.perf_counter() - started,
    )
    return SceneAuthorResult(
        suggestion=suggestion,
        observations=tuple(observations),
        usage=usage,
        provider=provider.name,
        model=provider.model,
    )


def render_result(result: SceneAuthorResult) -> str:
    suggestion = result.suggestion
    lines = [
        f"# scene-author: {suggestion.label}",
        "",
        "## 共通して強く使える特徴",
    ]
    _append_features(lines, suggestion.common_features)
    lines.extend(["", "## 補助的に使える特徴"])
    _append_features(lines, suggestion.supporting_features)
    lines.extend(["", "## sceneの条件に使わない方がよい特徴"])
    _append_features(lines, suggestion.avoid_features)
    lines.extend(
        [
            "",
            "## scene.yaml 追加候補",
            suggestion.suggested_scene_text.strip(),
        ]
    )
    if suggestion.cautions:
        lines.extend(["", "## 注意点"])
        lines.extend(f"- {item}" for item in suggestion.cautions)
    lines.extend(
        [
            "",
            (
                f"provider={result.provider} model={result.model} "
                f"examples={len(result.observations)} "
                f"requests={result.usage.request_count} "
                f"estimated_cost={result.usage.estimated_cost}"
            ),
            "この出力は候補です。scene.yaml は自動では読み込み・変更されません。",
        ]
    )
    return "\n".join(lines)


def write_json_result(path: Path, result: SceneAuthorResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": result.version,
        "provider": result.provider,
        "model": result.model,
        "observations": [item.model_dump(mode="json") for item in result.observations],
        "suggestion": result.suggestion.model_dump(mode="json"),
        "usage": result.usage.model_dump(mode="json"),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _append_features(lines: list[str], features: Sequence[SceneFeatureCandidate]) -> None:
    if not features:
        lines.append("- なし")
        return
    for feature in features:
        examples = (
            ",".join(str(value) for value in feature.example_numbers)
            if feature.example_numbers
            else "-"
        )
        lines.append(
            f"- {feature.description} (examples: {examples}) — {feature.rationale}"
        )


def _sampling_interval(duration_sec: float, frame_limit: int) -> float:
    if duration_sec <= 0:
        return 1.0
    return max(0.5, duration_sec / max(frame_limit - 1, 1))


def _effective_image_limit(settings: AppSettings, requested: int) -> int:
    configured = settings.genai.max_images_per_request
    if isinstance(configured, int):
        return min(requested, configured)
    return requested


def _select_frames(
    frames: Sequence[ExtractedFrame], *, max_images: int, max_raw_bytes: int
) -> tuple[ExtractedFrame, ...]:
    if max_images < 1:
        return ()
    selected = _evenly_spaced(frames, min(len(frames), max_images))
    while len(selected) > 1 and sum(
        frame.path.stat().st_size for frame in selected
    ) > max_raw_bytes:
        selected = _evenly_spaced(selected, len(selected) - 1)
    if selected and sum(frame.path.stat().st_size for frame in selected) > max_raw_bytes:
        raise ValueError(
            "a single extracted frame exceeds genai.max_inline_image_bytes"
        )
    return tuple(selected)


def _evenly_spaced(
    frames: Sequence[ExtractedFrame], count: int
) -> tuple[ExtractedFrame, ...]:
    if count <= 0 or not frames:
        return ()
    if count >= len(frames):
        return tuple(frames)
    if count == 1:
        return (frames[0],)
    last = len(frames) - 1
    indices = [round(index * last / (count - 1)) for index in range(count)]
    deduplicated = list(dict.fromkeys(indices))
    return tuple(frames[index] for index in deduplicated)


def _read_prompt(path: Path) -> str:
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    return resolved.read_text(encoding="utf-8")


def _settings_without_scene(explicit_path: Path | None) -> AppSettings:
    """Load ordinary runtime settings while deliberately excluding scene data.

    The main loader follows ``scene_file``. This helper must not: scene-author is
    allowed to see labeled clips and generic runtime settings, but never the
    household scene description it is helping a human maintain.
    """

    environment: Mapping[str, str] = os.environ
    path = explicit_path
    if path is None:
        raw_path = environment.get("CONFIG_PATH") or environment.get("ANALYZER_CONFIG")
        path = Path(raw_path) if raw_path else None

    data: dict[str, Any] = {}
    if path is not None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SettingsError(f"cannot read configuration file {path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise SettingsError(f"invalid YAML in configuration file {path}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise SettingsError("the YAML document root must be a mapping")
        # Remove scene data before environment substitution. The helper must not
        # resolve secrets or other private values embedded in inline scene data.
        raw = dict(raw)
        raw.pop("scene_file", None)
        raw.pop("scene", None)
        data = _substitute_env(raw, environment)

    _apply_env_overrides(data, environment)
    # Generic ANALYZER_SCENE_* overrides are applied above with all other runtime
    # settings, then removed here before validation/provider construction.
    data.pop("scene_file", None)
    data.pop("scene", None)
    return AppSettings.model_validate(data)


def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
