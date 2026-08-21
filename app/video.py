"""Portable video inspection and frame extraction helpers.

The functions in this module deliberately have no dependency on the application's
Pydantic models.  This keeps the ffmpeg boundary small and makes it possible to
use the helpers from both the daily pipeline and the single-video command.

Recording timestamps and filesystem mtimes have intentionally separate APIs:
``parse_recording_time`` only trusts a timestamp embedded in the filename,
whereas ``file_is_stable`` uses mtime solely to avoid reading an active upload.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import time
from typing import Any, Sequence
from zoneinfo import ZoneInfo


_REOLINK_TIMESTAMP = re.compile(r"(?<!\d)(\d{14})(?!\d)")
_ERROR_DETAIL_LIMIT = 2_000
# Bump when extraction/filter semantics change so orchestration can invalidate
# event artifacts even when the user-facing frame settings remain identical.
FRAME_EXTRACTION_VERSION = "ffmpeg_fps_pts_eofpass_daylight_v3"
LOGGER = logging.getLogger(__name__)
_VIDEO_DECODER_WARNING_RE = re.compile(
    r"\[(?:hevc|h26[45]|av1|vp9|mpeg4|mjpeg)\s+@",
    re.IGNORECASE,
)


class VideoProcessingError(RuntimeError):
    """Raised when ffprobe or ffmpeg cannot process a video.

    ``stderr`` is retained in a bounded, single-line form so callers can put the
    exception in a per-file failure record without accidentally producing a huge
    daily report or log entry.
    """

    def __init__(
        self,
        operation: str,
        detail: str,
        *,
        returncode: int | None = None,
        stderr: str | None = None,
    ) -> None:
        self.operation = operation
        self.returncode = returncode
        self.stderr = _summarize_error(stderr or "")

        message = f"{operation} failed: {detail}"
        if returncode is not None:
            message += f" (exit code {returncode})"
        if self.stderr:
            message += f": {self.stderr}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    """Metadata obtained from the first video stream with ffprobe."""

    duration_sec: float
    width: int
    height: int
    fps: float
    codec: str

    @property
    def duration(self) -> float:
        """Compatibility alias for code that refers to ffprobe's duration."""

        return self.duration_sec

    @property
    def resolution(self) -> tuple[int, int]:
        return (self.width, self.height)


@dataclass(frozen=True, slots=True)
class ExtractedFrame:
    """One frame in chronological order.

    ``timestamp_sec`` is the intended sample offset from the beginning of the
    video.  It is kept separately from the filename so the VLM adapter can label
    every image explicitly.
    """

    path: Path
    timestamp_sec: float
    index: int = 0


@dataclass(frozen=True, slots=True)
class DaylightFeatures:
    """Small-frame statistics used only to select a safe extraction resolution."""

    sampled_frames: int
    median_saturation: float
    dark_ratio: float
    decoder_warning: bool = False

    def is_daylike(
        self,
        *,
        saturation_threshold: float,
        dark_ratio_threshold: float,
    ) -> bool:
        """Return true only when both conservative daylight conditions hold."""

        return (
            not self.decoder_warning
            and self.median_saturation > saturation_threshold
            and self.dark_ratio < dark_ratio_threshold
        )


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Filesystem values useful for upload-stability diagnostics/state."""

    size: int
    mtime: float
    mtime_ns: int


def parse_recording_time(
    filename: str | os.PathLike[str],
    timezone: str = "Asia/Tokyo",
) -> datetime | None:
    """Parse the first valid standalone ``YYYYMMDDHHMMSS`` in *filename*.

    Reolink names can contain more than one timestamp (for example, start and
    end).  The first valid value is treated as the recording start.  Invalid
    candidates are skipped in case a later candidate is valid.  No filesystem
    metadata fallback is made: an unknown filename timestamp returns ``None``.
    """

    name = Path(filename).name
    tz = ZoneInfo(timezone)
    for match in _REOLINK_TIMESTAMP.finditer(name):
        try:
            naive = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        return naive.replace(tzinfo=tz)
    return None


def recording_end(
    recording_start: datetime | None,
    duration_sec: float,
) -> datetime | None:
    """Return a recording end time when its start is known."""

    if recording_start is None:
        return None
    if not math.isfinite(duration_sec) or duration_sec < 0:
        raise ValueError("duration_sec must be a finite non-negative number")
    return recording_start + timedelta(seconds=duration_sec)


def snapshot_file(path: str | os.PathLike[str]) -> FileSnapshot:
    """Read the size and mtime used to determine whether an upload is stable."""

    stat = Path(path).stat()
    return FileSnapshot(
        size=stat.st_size,
        mtime=stat.st_mtime,
        mtime_ns=stat.st_mtime_ns,
    )


def file_is_stable(
    path: str | os.PathLike[str],
    stable_seconds: float,
    *,
    now: float | datetime | None = None,
    previous_snapshot: FileSnapshot | None = None,
) -> bool:
    """Return whether a non-empty file appears safe to process.

    A file is stable when its current mtime is at least ``stable_seconds`` old.
    Supplying a prior snapshot adds an explicit size/mtime equality check, which
    is useful for callers that poll.  A missing file is treated as not stable so
    a concurrently renamed upload does not abort processing of other videos.

    This function must never be used to infer a recording timestamp.
    """

    if not math.isfinite(stable_seconds) or stable_seconds < 0:
        raise ValueError("stable_seconds must be a finite non-negative number")

    try:
        current = snapshot_file(path)
    except (FileNotFoundError, OSError):
        return False

    if current.size <= 0:
        return False
    if previous_snapshot is not None and current != previous_snapshot:
        return False

    if now is None:
        now_timestamp = time.time()
    elif isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware when supplied as datetime")
        now_timestamp = now.timestamp()
    else:
        now_timestamp = float(now)

    if not math.isfinite(now_timestamp):
        raise ValueError("now must be finite")
    return now_timestamp - current.mtime >= stable_seconds


def probe_video(
    path: str | os.PathLike[str],
    *,
    ffprobe_bin: str = "ffprobe",
    timeout_sec: float = 60.0,
) -> VideoMetadata:
    """Read duration, dimensions, frame rate, and codec using ffprobe."""

    video_path = Path(path)
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        (
            "stream=width,height,codec_name,avg_frame_rate,r_frame_rate,duration:"
            "format=duration"
        ),
        "-of",
        "json",
        os.fspath(video_path),
    ]
    result = _run_command(command, operation="ffprobe", timeout_sec=timeout_sec)

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise VideoProcessingError("ffprobe", f"invalid JSON output ({exc})") from exc

    try:
        streams = payload["streams"]
        stream = streams[0]
        if not isinstance(stream, dict):
            raise TypeError("video stream must be an object")
    except (KeyError, IndexError, TypeError) as exc:
        raise VideoProcessingError("ffprobe", "no video stream in output") from exc

    try:
        width = _positive_int(stream.get("width"), "width")
        height = _positive_int(stream.get("height"), "height")
        codec = str(stream.get("codec_name") or "").strip()
        if not codec:
            raise ValueError("codec is missing")

        fps = _parse_frame_rate(stream)
        duration = _parse_duration(payload, stream)
    except (TypeError, ValueError) as exc:
        raise VideoProcessingError("ffprobe", f"invalid metadata ({exc})") from exc

    return VideoMetadata(
        duration_sec=duration,
        width=width,
        height=height,
        fps=fps,
        codec=codec,
    )


def extract_frames(
    video_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    interval_sec: float = 1.0,
    max_long_edge_px: int = 1280,
    jpeg_quality: int = 85,
    ffmpeg_bin: str = "ffmpeg",
    timeout_sec: float | None = None,
) -> list[ExtractedFrame]:
    """Extract ordered JPEG samples spanning the video from time zero.

    The caller owns ``output_dir`` and should normally place it below a
    ``TemporaryDirectory``.  Existing ``frame_*.jpg`` files are rejected to
    prevent stale images from being mixed with this extraction.
    """

    interval = _positive_finite_float(interval_sec, "interval_sec")
    if isinstance(max_long_edge_px, bool) or not isinstance(max_long_edge_px, int):
        raise TypeError("max_long_edge_px must be an integer")
    if max_long_edge_px <= 0:
        raise ValueError("max_long_edge_px must be positive")
    if isinstance(jpeg_quality, bool) or not isinstance(jpeg_quality, int):
        raise TypeError("jpeg_quality must be an integer")
    if not 1 <= jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be between 1 and 100")

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.glob("frame_*.jpg")):
        raise ValueError("output_dir already contains frame_*.jpg files")

    # ``min`` avoids enlarging small inputs; -2 preserves aspect ratio while
    # producing an even derived dimension that encoders handle consistently.
    edge = max_long_edge_px
    scale_filter = (
        f"scale=w='if(gte(iw,ih),min(iw,{edge}),-2)':"
        f"h='if(gte(iw,ih),-2,min(ih,{edge}))':flags=lanczos"
    )
    interval_text = format(interval, ".12g")
    # Preserve source PTS so timestamps remain offsets from the container
    # timeline even when an HEVC clip begins with undecodable inter-frames.
    # ``eof_action=pass`` retains the final valid interval instead of dropping
    # it during EOF rounding.
    video_filter = (
        f"fps=fps=1/{interval_text}:start_time=0:round=near:eof_action=pass,"
        f"{scale_filter}"
    )
    output_pattern = destination / "frame_%06d.jpg"

    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        os.fspath(Path(video_path)),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        video_filter,
        "-q:v",
        str(_jpeg_quality_to_qscale(jpeg_quality)),
        "-start_number",
        "0",
        os.fspath(output_pattern),
    ]
    result = _run_command(command, operation="ffmpeg", timeout_sec=timeout_sec)
    if result.stderr.strip():
        # ffmpeg can return zero while reporting recoverable HEVC corruption
        # (for example a recording that starts between reference frames).
        LOGGER.warning(
            "ffmpeg completed with decoder diagnostics file=%s: %s",
            Path(video_path).name,
            _summarize_error(result.stderr),
        )

    paths = sorted(destination.glob("frame_*.jpg"), key=_frame_number)
    if not paths:
        raise VideoProcessingError("ffmpeg", "no frames were produced")

    return [
        ExtractedFrame(
            path=frame_path,
            timestamp_sec=_frame_number(frame_path) * interval,
            index=_frame_number(frame_path),
        )
        for frame_path in paths
    ]


def measure_daylight_features(
    video_path: str | os.PathLike[str],
    *,
    sample_frames: int = 5,
    interval_sec: float = 1.0,
    sample_width: int = 160,
    sample_height: int = 90,
    dark_luminance_threshold: int = 40,
    ffmpeg_bin: str = "ffmpeg",
    timeout_sec: float | None = None,
) -> DaylightFeatures:
    """Measure saturation and dark-pixel ratio from a few tiny leading frames."""

    if isinstance(sample_frames, bool) or not isinstance(sample_frames, int):
        raise TypeError("sample_frames must be an integer")
    if sample_frames <= 0:
        raise ValueError("sample_frames must be positive")
    interval = _positive_finite_float(interval_sec, "interval_sec")
    for name, value in (("sample_width", sample_width), ("sample_height", sample_height)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value <= 0:
            raise ValueError(f"{name} must be positive")
    if (
        isinstance(dark_luminance_threshold, bool)
        or not isinstance(dark_luminance_threshold, int)
        or not 0 <= dark_luminance_threshold <= 255
    ):
        raise ValueError("dark_luminance_threshold must be an integer from 0 to 255")

    fps = 1.0 / interval
    video_filter = (
        f"fps={fps:.12g}:start_time=0:round=near:eof_action=pass,"
        f"scale={sample_width}:{sample_height}:"
        "force_original_aspect_ratio=decrease:flags=area,"
        f"pad={sample_width}:{sample_height}:(ow-iw)/2:(oh-ih)/2:black"
    )
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        os.fspath(Path(video_path)),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        video_filter,
        "-frames:v",
        str(sample_frames),
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    result = _run_binary_command(
        command,
        operation="ffmpeg daylight sampling",
        timeout_sec=timeout_sec,
    )
    frame_size = sample_width * sample_height * 3
    produced = min(sample_frames, len(result.stdout) // frame_size)
    if produced <= 0:
        raise VideoProcessingError("ffmpeg daylight sampling", "no frames were produced")
    if len(result.stdout) % frame_size:
        LOGGER.warning(
            "discarding incomplete daylight sample bytes file=%s bytes=%d",
            Path(video_path).name,
            len(result.stdout) % frame_size,
        )

    saturations: list[float] = []
    dark_ratios: list[float] = []
    payload = memoryview(result.stdout)
    for index in range(produced):
        start = index * frame_size
        saturation, dark_ratio = _frame_daylight_features(
            payload[start : start + frame_size],
            dark_luminance_threshold=dark_luminance_threshold,
        )
        saturations.append(saturation)
        dark_ratios.append(dark_ratio)

    stderr = result.stderr.decode("utf-8", errors="replace")
    return DaylightFeatures(
        sampled_frames=produced,
        median_saturation=float(statistics.median(saturations)),
        dark_ratio=float(statistics.median(dark_ratios)),
        decoder_warning=bool(_VIDEO_DECODER_WARNING_RE.search(stderr)),
    )


def _frame_daylight_features(
    frame: memoryview,
    *,
    dark_luminance_threshold: int,
) -> tuple[float, float]:
    saturations: list[float] = []
    dark_pixels = 0
    pixel_count = len(frame) // 3
    luminance_limit = dark_luminance_threshold * 256
    for offset in range(0, pixel_count * 3, 3):
        red = frame[offset]
        green = frame[offset + 1]
        blue = frame[offset + 2]
        maximum = max(red, green, blue)
        spread = maximum - min(red, green, blue)
        saturations.append((spread * 255.0 / maximum) if maximum else 0.0)
        if 77 * red + 150 * green + 29 * blue < luminance_limit:
            dark_pixels += 1
    return float(statistics.median(saturations)), dark_pixels / pixel_count


def _run_command(
    command: Sequence[str],
    *,
    operation: str,
    timeout_sec: float | None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
    except FileNotFoundError as exc:
        raise VideoProcessingError(
            operation,
            f"executable not found ({command[0]})",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        timeout_text = "configured timeout" if timeout_sec is None else f"{timeout_sec}s"
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        raise VideoProcessingError(
            operation,
            f"timed out after {timeout_text}",
            stderr=stderr,
        ) from exc
    except OSError as exc:
        raise VideoProcessingError(operation, str(exc)) from exc

    if result.returncode != 0:
        raise VideoProcessingError(
            operation,
            "command returned a non-zero status",
            returncode=result.returncode,
            stderr=result.stderr,
        )
    return result


def _run_binary_command(
    command: Sequence[str],
    *,
    operation: str,
    timeout_sec: float | None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=False,
            check=False,
            timeout=timeout_sec,
        )
    except FileNotFoundError as exc:
        raise VideoProcessingError(
            operation,
            f"executable not found ({command[0]})",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        timeout_text = "configured timeout" if timeout_sec is None else f"{timeout_sec}s"
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        raise VideoProcessingError(
            operation,
            f"timed out after {timeout_text}",
            stderr=stderr,
        ) from exc
    except OSError as exc:
        raise VideoProcessingError(operation, str(exc)) from exc

    if result.returncode != 0:
        raise VideoProcessingError(
            operation,
            "command returned a non-zero status",
            returncode=result.returncode,
            stderr=result.stderr.decode("utf-8", errors="replace"),
        )
    return result


def _parse_duration(payload: dict[str, Any], stream: dict[str, Any]) -> float:
    format_data = payload.get("format")
    candidates = []
    if isinstance(format_data, dict):
        candidates.append(format_data.get("duration"))
    candidates.append(stream.get("duration"))

    for candidate in candidates:
        try:
            duration = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise ValueError("duration is missing or non-positive")


def _parse_frame_rate(stream: dict[str, Any]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key)
        if raw is None:
            continue
        try:
            if isinstance(raw, str) and "/" in raw:
                numerator_text, denominator_text = raw.split("/", 1)
                numerator = float(numerator_text)
                denominator = float(denominator_text)
                if denominator == 0:
                    continue
                rate = numerator / denominator
            else:
                rate = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and rate > 0:
            return rate
    raise ValueError("fps is missing or non-positive")


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} is not an integer")
    number = int(value)
    if number <= 0:
        raise ValueError(f"{name} is non-positive")
    return number


def _positive_finite_float(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _jpeg_quality_to_qscale(quality: int) -> int:
    # ffmpeg's MJPEG encoder uses 2 (best) through 31 (worst), unlike the
    # conventional 1..100 JPEG quality scale exposed by application config.
    return max(2, min(31, round(31 - ((quality - 1) * 29 / 99))))


def _frame_number(path: Path) -> int:
    match = re.search(r"(\d+)$", path.stem)
    return int(match.group(1)) if match else 0


def _summarize_error(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) <= _ERROR_DETAIL_LIMIT:
        return compact
    return compact[: _ERROR_DETAIL_LIMIT - 1] + "…"
