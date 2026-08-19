"""One MP4 -> timestamped frames -> one validated event JSON model.

Observation is split into a preparation step and a completion step. Preparation
is local work — probe, extract, build prompts — and completion turns provider
results into an ``Event``. The synchronous path runs both back to back; the
daily batch path prepares every clip first, submits them as one batch, and
completes them once the batch returns.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from app.config import AppSettings
from app.genai.base import (
    BatchOutcome,
    BatchRequest,
    FrameInput,
    GenAIProvider,
    GenAIResponseError,
    failure_usage,
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


@dataclass(slots=True)
class PreparedEvent:
    """One clip's extracted frames, already turned into provider requests.

    The JPEGs stay on disk until ``release`` is called, because a deferred
    submission encodes them long after extraction. Every caller owns exactly
    one ``release`` per prepared event.
    """

    event_id: str
    source_file: str
    metadata: VideoMetadata
    recording_start: datetime | None
    analysis_signature: str | None
    source_fingerprint: str | None
    requests: tuple[BatchRequest[EventAnalysis], ...]
    frames_analyzed: int
    frame_bytes: int
    workspace: Path
    local_sec: float

    @property
    def chunk_count(self) -> int:
        return len(self.requests)

    def release(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


class EventAnalyzer:
    def __init__(self, settings: AppSettings, provider: GenAIProvider) -> None:
        self.settings = settings
        self.provider = provider
        self._event_prompt = _read_prompt(settings.prompts.event)
        self._synthesis_prompt = _read_prompt(settings.prompts.event_synthesis)

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
        prepared = self.prepare_file(
            video_path,
            event_id=event_id,
            source_file=source_file,
            metadata=metadata,
            recording_start=recording_start,
            analysis_signature=analysis_signature,
            source_fingerprint=source_fingerprint,
            expected_snapshot=expected_snapshot,
        )
        observation_started = time.perf_counter()
        try:
            outcomes = self._observe_now(prepared)
        finally:
            prepared.release()
        # Here the provider calls really are this run's wall clock, unlike the
        # shared queue wait a deferred batch spends.
        prepared.local_sec += time.perf_counter() - observation_started
        return self.complete(prepared, outcomes)

    def prepare_file(
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
    ) -> PreparedEvent:
        """Probe, extract frames, and build one request per frame chunk."""

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
        workspace = Path(
            tempfile.mkdtemp(prefix=f"event-{event_id}-", dir=str(temp_root))
        )
        try:
            frames = extract_frames(
                video_path,
                workspace,
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
            requests, frame_bytes = self._chunk_requests(
                event_id=event_id,
                frames=frames,
                metadata=metadata,
                source_file=source_file or video_path.name,
                recording_start=recording_start,
            )
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise

        return PreparedEvent(
            event_id=event_id,
            source_file=source_file or video_path.name,
            metadata=metadata,
            recording_start=recording_start,
            analysis_signature=analysis_signature,
            source_fingerprint=source_fingerprint,
            requests=requests,
            frames_analyzed=len(frames),
            frame_bytes=frame_bytes,
            workspace=workspace,
            local_sec=time.perf_counter() - started,
        )

    def complete(
        self,
        prepared: PreparedEvent,
        outcomes: Mapping[str, BatchOutcome[EventAnalysis]],
    ) -> Event:
        """Turn chunk results into one validated ``Event``.

        A multi-chunk clip is reconciled here with a synthesis request. That
        stage sends no images and there is at most one per clip, so it stays
        synchronous rather than costing the run a second deferred wait.
        """

        started = time.perf_counter()
        analysis, usage = self._merge(prepared, outcomes)
        analysis = _normalize_timestamps(analysis, prepared.metadata.duration_sec)

        processing = ProcessingMetadata(
            provider=self.provider.name,
            model=self.provider.model,
            prompt_version=self.settings.prompts.event_version,
            schema_version=SCHEMA_VERSION,
            analysis_signature=prepared.analysis_signature,
            source_fingerprint=prepared.source_fingerprint,
            frame_interval_sec=self.settings.frames.interval_sec,
            frames_analyzed=prepared.frames_analyzed,
            chunk_count=prepared.chunk_count,
            # Local work only. A deferred batch shares one queue wait across
            # every clip of the day, so attributing it per event would report
            # that wait dozens of times over.
            processing_time_sec=prepared.local_sec + (time.perf_counter() - started),
            usage=usage,
            completed_at=datetime.now(timezone.utc),
        )
        return Event(
            **analysis.model_dump(),
            schema_version=SCHEMA_VERSION,
            event_id=prepared.event_id,
            source_file=prepared.source_file,
            recording_start=prepared.recording_start,
            recording_end=recording_end(
                prepared.recording_start, prepared.metadata.duration_sec
            ),
            time_confidence=(
                TimeConfidence.FILENAME
                if prepared.recording_start is not None
                else TimeConfidence.UNKNOWN
            ),
            duration_sec=prepared.metadata.duration_sec,
            video_metadata=EventVideoMetadata(
                width=prepared.metadata.width,
                height=prepared.metadata.height,
                fps=prepared.metadata.fps,
                codec=prepared.metadata.codec,
            ),
            processing=processing,
        )

    def _chunk_requests(
        self,
        *,
        event_id: str,
        frames: Sequence[ExtractedFrame],
        metadata: VideoMetadata,
        source_file: str,
        recording_start: datetime | None,
    ) -> tuple[tuple[BatchRequest[EventAnalysis], ...], int]:
        chunks = chunk_frames(
            frames,
            max_images=self.settings.genai.max_images_per_request,
            max_raw_bytes=self.settings.genai.max_inline_image_bytes,
            overlap=self.settings.genai.chunk_overlap_frames,
        )
        requests: list[BatchRequest[EventAnalysis]] = []
        frame_bytes = 0
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
            for frame in chunk:
                try:
                    frame_bytes += frame.path.stat().st_size
                except OSError:  # pragma: no cover - stat of a fresh temp file
                    pass
            requests.append(
                BatchRequest(
                    custom_id=chunk_custom_id(event_id, index),
                    prompt=self._event_prompt.format(
                        context=json.dumps(context, ensure_ascii=False, indent=2)
                    ),
                    response_model=EventAnalysis,
                    frames=tuple(
                        FrameInput(path=frame.path, timestamp_sec=frame.timestamp_sec)
                        for frame in chunk
                    ),
                )
            )
        return tuple(requests), frame_bytes

    def _observe_now(
        self, prepared: PreparedEvent
    ) -> dict[str, BatchOutcome[EventAnalysis]]:
        """Run a prepared clip's chunks immediately, stopping at the first failure.

        Chunks after a failure are never sent: the event is lost either way, so
        there is no reason to pay for the rest of it.
        """

        outcomes: dict[str, BatchOutcome[EventAnalysis]] = {}
        for request in prepared.requests:
            try:
                result = self.provider.generate_structured(
                    prompt=request.prompt,
                    response_model=request.response_model,
                    frames=request.frames,
                )
            except Exception as exc:
                outcomes[request.custom_id] = BatchOutcome(
                    custom_id=request.custom_id,
                    usage=failure_usage(exc),
                    error=str(exc) or exc.__class__.__name__,
                )
                break
            outcomes[request.custom_id] = BatchOutcome(
                custom_id=request.custom_id, usage=result.usage, value=result.value
            )
        return outcomes

    def _merge(
        self,
        prepared: PreparedEvent,
        outcomes: Mapping[str, BatchOutcome[EventAnalysis]],
    ) -> tuple[EventAnalysis, APIUsage]:
        usage = APIUsage()
        values: list[EventAnalysis] = []
        failure: tuple[int, str] | None = None
        total = len(prepared.requests)
        for index, request in enumerate(prepared.requests, start=1):
            outcome = outcomes.get(request.custom_id)
            if outcome is None:
                failure = failure or (index, "no result was returned")
                continue
            usage = usage.plus(outcome.usage)
            if outcome.value is None:
                failure = failure or (index, outcome.error or "unknown error")
                continue
            values.append(outcome.value)
        if failure is not None:
            raise GenAIResponseError(
                f"event chunk {failure[0]}/{total} failed: {failure[1]}", usage=usage
            )

        if len(values) == 1:
            return values[0], usage

        synthesis_context = {
            "source_file": prepared.source_file,
            "duration_sec": prepared.metadata.duration_sec,
            "recording_start_local": (
                prepared.recording_start.isoformat()
                if prepared.recording_start
                else None
            ),
            "chunk_count": total,
        }
        synthesis_prompt = self._synthesis_prompt.format(
            context=json.dumps(synthesis_context, ensure_ascii=False, indent=2),
            chunk_json=json.dumps(
                [value.model_dump(mode="json") for value in values],
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
            raise GenAIResponseError(
                f"event synthesis failed: {exc}",
                usage=usage.plus(failure_usage(exc)),
            ) from exc
        return synthesis.value, usage.plus(synthesis.usage)


def chunk_custom_id(event_id: str, index: int) -> str:
    """Batch-safe request id: unique per chunk and short enough for the API."""

    return f"{event_id}-c{index:03d}"


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
