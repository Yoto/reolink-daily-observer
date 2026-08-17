"""One MP4 -> timestamped frames -> one validated event JSON model."""

from __future__ import annotations

import json
import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from app.config import AppSettings
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
    ProcessingMetadata,
    SCHEMA_VERSION,
    TimeConfidence,
    VideoMetadata as EventVideoMetadata,
)
from app.person_filter import PersonFrameFilter
from app.video import (
    ExtractedFrame,
    FileSnapshot,
    VideoMetadata,
    VideoProcessingError,
    extract_frames,
    parse_recording_time,
    probe_video,
    recording_end,
    snapshot_file,
)


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Bump for semantic orchestration/normalization changes not represented by the
# prompt, schema, provider model, or frame extraction version.
EVENT_ANALYSIS_PIPELINE_VERSION = "event_pipeline_v1"


class EventAnalyzer:
    def __init__(
        self,
        settings: AppSettings,
        provider: GenAIProvider,
        frame_filter: PersonFrameFilter | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        # Built once per run: loading detector weights takes seconds, and a
        # misconfigured filter should stop the run before the first API call
        # rather than after the first video.
        self.frame_filter = frame_filter or PersonFrameFilter(settings)
        self._event_prompt = _read_prompt(settings.prompts.event)
        self._synthesis_prompt = _read_prompt(settings.prompts.event_synthesis)

    def close(self) -> None:
        self.frame_filter.close()

    def analyze_file(
        self,
        video_path: Path,
        *,
        event_id: str,
        source_file: str | None = None,
        metadata: VideoMetadata | None = None,
        recording_start: datetime | None = None,
        analysis_signature: str | None = None,
        source_fingerprint: str | None = None,
        expected_snapshot: FileSnapshot | None = None,
    ) -> Event:
        started = time.perf_counter()
        metadata = metadata or probe_video(
            video_path, timeout_sec=self.settings.input.ffprobe_timeout_sec
        )
        if recording_start is None:
            recording_start = parse_recording_time(
                video_path.name, self.settings.timezone
            )

        temp_root = self.settings.paths.temp
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"event-{event_id}-", dir=temp_root
        ) as temporary:
            frames = extract_frames(
                video_path,
                Path(temporary),
                interval_sec=self.settings.frames.interval_sec,
                max_long_edge_px=self.settings.frames.max_long_edge_px,
                jpeg_quality=self.settings.frames.jpeg_quality,
                timeout_sec=self.settings.frames.ffmpeg_timeout_sec,
            )
            if (
                expected_snapshot is not None
                and snapshot_file(video_path) != expected_snapshot
            ):
                raise VideoProcessingError(
                    "input stability",
                    "source size or mtime changed during frame extraction",
                )
            LOGGER.info(
                "extracted frames event_id=%s duration_sec=%.3f frames=%d",
                event_id,
                metadata.duration_sec,
                len(frames),
            )
            # Local person detection runs between extraction and the provider
            # so the pre-record and post-record margins Reolink adds to every
            # clip are never paid for. The JPEGs still live in the same
            # temporary directory and are removed with it either way.
            filtered = self.frame_filter.select(frames)
            analysis, usage, chunk_count = self._observe(
                frames=filtered.frames,
                metadata=metadata,
                source_file=source_file or video_path.name,
                recording_start=recording_start,
            )

        analysis = _normalize_timestamps(analysis, metadata.duration_sec)
        completed_at = datetime.now(timezone.utc)
        processing = ProcessingMetadata(
            provider=self.provider.name,
            model=self.provider.model,
            prompt_version=self.settings.prompts.event_version,
            schema_version=SCHEMA_VERSION,
            analysis_signature=analysis_signature,
            source_fingerprint=source_fingerprint,
            frame_interval_sec=self.settings.frames.interval_sec,
            frames_analyzed=len(filtered.frames),
            chunk_count=chunk_count,
            processing_time_sec=time.perf_counter() - started,
            usage=usage,
            frame_filter=filtered.metadata,
            completed_at=completed_at,
        )
        return Event(
            **analysis.model_dump(),
            schema_version=SCHEMA_VERSION,
            event_id=event_id,
            source_file=source_file or video_path.name,
            recording_start=recording_start,
            recording_end=recording_end(recording_start, metadata.duration_sec),
            time_confidence=(
                TimeConfidence.FILENAME
                if recording_start is not None
                else TimeConfidence.UNKNOWN
            ),
            duration_sec=metadata.duration_sec,
            video_metadata=EventVideoMetadata(
                width=metadata.width,
                height=metadata.height,
                fps=metadata.fps,
                codec=metadata.codec,
            ),
            processing=processing,
        )

    def _observe(
        self,
        *,
        frames: Sequence[ExtractedFrame],
        metadata: VideoMetadata,
        source_file: str,
        recording_start: datetime | None = None,
    ) -> tuple[EventAnalysis, APIUsage, int]:
        chunks = chunk_frames(
            frames,
            max_images=self.settings.genai.max_images_per_request,
            max_raw_bytes=self.settings.genai.max_inline_image_bytes,
            overlap=self.settings.genai.chunk_overlap_frames,
        )
        results: list[StructuredResult[EventAnalysis]] = []
        usage = APIUsage()
        for index, chunk in enumerate(chunks, start=1):
            context = {
                "source_file": source_file,
                "duration_sec": metadata.duration_sec,
                "recording_start_local": (
                    recording_start.isoformat() if recording_start else None
                ),
                "video_metadata": {
                    "width": metadata.width,
                    "height": metadata.height,
                    "fps": metadata.fps,
                    "codec": metadata.codec,
                },
                "chunk": {
                    "index": index,
                    "count": len(chunks),
                    "start_sec": chunk[0].timestamp_sec,
                    "end_sec": chunk[-1].timestamp_sec,
                },
            }
            scene = self.settings.scene.prompt_payload()
            if scene:
                context["scene"] = scene
            prompt = self._event_prompt.format(
                context=json.dumps(context, ensure_ascii=False, indent=2)
            )
            try:
                result = self.provider.generate_structured(
                    prompt=prompt,
                    response_model=EventAnalysis,
                    frames=[
                        FrameInput(path=frame.path, timestamp_sec=frame.timestamp_sec)
                        for frame in chunk
                    ],
                )
            except Exception as exc:
                failed_usage = getattr(exc, "usage", APIUsage())
                if not isinstance(failed_usage, APIUsage):
                    failed_usage = APIUsage()
                raise GenAIResponseError(
                    f"event chunk {index}/{len(chunks)} failed: {exc}",
                    usage=usage.plus(failed_usage),
                ) from exc
            results.append(result)
            usage = usage.plus(result.usage)

        if len(results) == 1:
            return results[0].value, usage, 1

        synthesis_context = {
            "source_file": source_file,
            "duration_sec": metadata.duration_sec,
            "recording_start_local": (
                recording_start.isoformat() if recording_start else None
            ),
            "chunk_count": len(chunks),
        }
        synthesis_prompt = self._synthesis_prompt.format(
            context=json.dumps(synthesis_context, ensure_ascii=False, indent=2),
            chunk_json=json.dumps(
                [result.value.model_dump(mode="json") for result in results],
                ensure_ascii=False,
                indent=2,
            ),
        )
        try:
            synthesis = self.provider.generate_structured(
                prompt=synthesis_prompt,
                response_model=EventAnalysis,
            )
        except Exception as exc:
            failed_usage = getattr(exc, "usage", APIUsage())
            if not isinstance(failed_usage, APIUsage):
                failed_usage = APIUsage()
            raise GenAIResponseError(
                f"event synthesis failed: {exc}",
                usage=usage.plus(failed_usage),
            ) from exc
        return synthesis.value, usage.plus(synthesis.usage), len(chunks)


def chunk_frames(
    frames: Sequence[ExtractedFrame],
    *,
    max_images: str | int,
    max_raw_bytes: int,
    overlap: int,
) -> list[list[ExtractedFrame]]:
    """Split only when image-count or inline-body safety limits require it."""

    if not frames:
        raise ValueError("at least one extracted frame is required")
    image_limit = 3600 if max_images == "auto" else int(max_images)
    if image_limit < 1 or max_raw_bytes < 1:
        raise ValueError("chunk limits must be positive")
    if overlap < 0:
        raise ValueError("overlap cannot be negative")

    chunks: list[list[ExtractedFrame]] = []
    start = 0
    while start < len(frames):
        current: list[ExtractedFrame] = []
        current_bytes = 0
        cursor = start
        while cursor < len(frames) and len(current) < image_limit:
            frame = frames[cursor]
            frame_bytes = frame.path.stat().st_size
            if current and current_bytes + frame_bytes > max_raw_bytes:
                break
            current.append(frame)
            current_bytes += frame_bytes
            cursor += 1
        if not current:
            current = [frames[start]]
            cursor = start + 1
        chunks.append(current)
        if cursor >= len(frames):
            break
        effective_overlap = min(overlap, max(0, len(current) - 1))
        start = cursor - effective_overlap
    return chunks


def _normalize_timestamps(analysis: EventAnalysis, duration: float) -> EventAnalysis:
    """Bound model-provided offsets to the trusted ffprobe duration."""

    payload = analysis.model_dump(mode="python")
    for collection in ("observations", "interactions", "scene_changes"):
        for statement in payload[collection]:
            statement["start_sec"] = min(max(statement["start_sec"], 0), duration)
            statement["end_sec"] = min(max(statement["end_sec"], 0), duration)
            if statement["end_sec"] < statement["start_sec"]:
                statement["end_sec"] = statement["start_sec"]
        payload[collection].sort(key=lambda value: (value["start_sec"], value["end_sec"]))
    for entity in payload["entities"]:
        for key in ("first_seen_sec", "last_seen_sec"):
            if entity[key] is not None:
                entity[key] = min(max(entity[key], 0), duration)
        if (
            entity["first_seen_sec"] is not None
            and entity["last_seen_sec"] is not None
            and entity["last_seen_sec"] < entity["first_seen_sec"]
        ):
            entity["last_seen_sec"] = entity["first_seen_sec"]
    return EventAnalysis.model_validate(payload)


def _read_prompt(path: Path) -> str:
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read prompt {resolved}: {exc}") from exc
