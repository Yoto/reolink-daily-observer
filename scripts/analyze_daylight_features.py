# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy>=2,<3", "pillow>=11,<13"]
# ///

"""Measure simple daylight features from the first seconds of MP4 recordings.

This is a diagnostic tool, intentionally separate from the application runtime.
It extracts a few tiny RGB frames through FFmpeg, computes per-frame saturation
and dark-pixel statistics, then aggregates those values per video.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFont


_TIMESTAMP_RE = re.compile(r"(?<!\d)(\d{14})(?!\d)")
_VIDEO_DECODER_WARNING_RE = re.compile(
    r"\[(?:hevc|h26[45]|av1|vp9|mpeg4|mjpeg)\s+@",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class VideoFeatures:
    path: str
    filename: str
    recording_time: str
    recording_date: str
    recording_hour: int
    sampled_frames: int
    median_saturation: float
    dark_ratio: float
    median_rgb_spread: float
    saturation_min: float
    saturation_max: float
    dark_ratio_min: float
    dark_ratio_max: float
    decoder_warning: bool
    decoder_message: str


@dataclass(frozen=True, slots=True)
class Failure:
    path: str
    error: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Root directory containing dated MP4 files")
    parser.add_argument("output", type=Path, help="Directory for CSV, JSON, and PNG outputs")
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--ffmpeg-bin", type=Path, default=Path("ffmpeg"))
    parser.add_argument("--frames", type=int, default=5)
    parser.add_argument("--interval-sec", type=float, default=1.0)
    parser.add_argument("--width", type=int, default=160)
    parser.add_argument("--height", type=int, default=90)
    parser.add_argument("--dark-threshold", type=int, default=40)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.start_date > args.end_date:
        parser.error("--start-date must be on or before --end-date")
    if args.frames <= 0 or args.width <= 0 or args.height <= 0 or args.workers <= 0:
        parser.error("frames, dimensions, and workers must be positive")
    if not 0 <= args.dark_threshold <= 255:
        parser.error("--dark-threshold must be between 0 and 255")
    return args


def recording_time(path: Path) -> datetime | None:
    for match in _TIMESTAMP_RE.finditer(path.name):
        try:
            return datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            continue
    return None


def iter_videos(root: Path, start: date, end: date) -> Iterable[tuple[Path, datetime]]:
    for path in sorted(root.rglob("*.mp4")):
        timestamp = recording_time(path)
        if timestamp is not None and start <= timestamp.date() <= end:
            yield path, timestamp


def extract_rgb_frames(
    path: Path,
    *,
    ffmpeg_bin: Path,
    frame_count: int,
    interval_sec: float,
    width: int,
    height: int,
) -> tuple[np.ndarray, str]:
    fps = 1.0 / interval_sec
    video_filter = (
        f"fps={fps:.12g}:start_time=0:round=near:eof_action=pass,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=area,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )
    command = [
        str(ffmpeg_bin),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        video_filter,
        "-frames:v",
        str(frame_count),
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    completed = subprocess.run(command, capture_output=True, timeout=120, check=False)
    decoder_message = completed.stderr.decode("utf-8", errors="replace").strip()
    if completed.returncode != 0:
        detail = decoder_message
        raise RuntimeError(detail[-1_000:] or f"FFmpeg exited with {completed.returncode}")

    bytes_per_frame = width * height * 3
    produced = len(completed.stdout) // bytes_per_frame
    if produced <= 0:
        raise RuntimeError("FFmpeg produced no complete RGB frames")
    payload = completed.stdout[: produced * bytes_per_frame]
    frames = np.frombuffer(payload, dtype=np.uint8).reshape(produced, height, width, 3)
    compact_message = " ".join(decoder_message.split())
    if len(compact_message) > 1_000:
        compact_message = compact_message[:999] + "…"
    return frames, compact_message


def frame_features(frame: np.ndarray, dark_threshold: int) -> tuple[float, float, float]:
    rgb = frame.astype(np.float32)
    maximum = rgb.max(axis=2)
    minimum = rgb.min(axis=2)
    spread = maximum - minimum
    saturation = np.divide(
        spread * 255.0,
        maximum,
        out=np.zeros_like(spread),
        where=maximum > 0,
    )
    luminance = (77.0 * rgb[:, :, 0] + 150.0 * rgb[:, :, 1] + 29.0 * rgb[:, :, 2]) / 256.0
    return (
        float(np.median(saturation)),
        float(np.mean(luminance < dark_threshold)),
        float(np.median(spread)),
    )


def analyze_video(path: Path, timestamp: datetime, args: argparse.Namespace) -> VideoFeatures:
    frames, decoder_message = extract_rgb_frames(
        path,
        ffmpeg_bin=args.ffmpeg_bin,
        frame_count=args.frames,
        interval_sec=args.interval_sec,
        width=args.width,
        height=args.height,
    )
    features = [frame_features(frame, args.dark_threshold) for frame in frames]
    saturations = np.asarray([item[0] for item in features])
    dark_ratios = np.asarray([item[1] for item in features])
    rgb_spreads = np.asarray([item[2] for item in features])
    return VideoFeatures(
        path=str(path),
        filename=path.name,
        recording_time=timestamp.isoformat(timespec="seconds"),
        recording_date=timestamp.date().isoformat(),
        recording_hour=timestamp.hour,
        sampled_frames=len(frames),
        median_saturation=float(np.median(saturations)),
        dark_ratio=float(np.median(dark_ratios)),
        median_rgb_spread=float(np.median(rgb_spreads)),
        saturation_min=float(np.min(saturations)),
        saturation_max=float(np.max(saturations)),
        dark_ratio_min=float(np.min(dark_ratios)),
        dark_ratio_max=float(np.max(dark_ratios)),
        decoder_warning=bool(_VIDEO_DECODER_WARNING_RE.search(decoder_message)),
        decoder_message=decoder_message,
    )


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values), q))


def summarize(rows: list[VideoFeatures], failures: list[Failure], args: argparse.Namespace) -> dict[str, object]:
    groups: dict[str, list[VideoFeatures]] = {}
    for row in rows:
        groups.setdefault(row.recording_date, []).append(row)

    by_date = {}
    for day, items in sorted(groups.items()):
        saturations = [item.median_saturation for item in items]
        dark_ratios = [item.dark_ratio for item in items]
        by_date[day] = {
            "videos": len(items),
            "median_saturation": {
                "p10": percentile(saturations, 10),
                "p50": percentile(saturations, 50),
                "p90": percentile(saturations, 90),
            },
            "dark_ratio": {
                "p10": percentile(dark_ratios, 10),
                "p50": percentile(dark_ratios, 50),
                "p90": percentile(dark_ratios, 90),
            },
        }

    return {
        "input": str(args.input),
        "start_date": args.start_date.isoformat(),
        "end_date": args.end_date.isoformat(),
        "videos_succeeded": len(rows),
        "videos_failed": len(failures),
        "videos_with_video_decoder_warnings": sum(row.decoder_warning for row in rows),
        "sampling": {
            "frames": args.frames,
            "interval_sec": args.interval_sec,
            "dimensions": [args.width, args.height],
            "dark_threshold": args.dark_threshold,
        },
        "by_date": by_date,
    }


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/segoeui.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def draw_scatter(
    rows: list[VideoFeatures],
    output: Path,
    start: date,
    end: date,
    *,
    include_decoder_warnings: bool,
) -> None:
    canvas_width, canvas_height = 1_200, 800
    left, top, right, bottom = 105, 80, 45, 95
    plot_width = canvas_width - left - right
    plot_height = canvas_height - top - bottom
    image = Image.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(image, "RGBA")
    title_font, axis_font, tick_font = _font(26), _font(20), _font(15)

    plotted_rows = rows if include_decoder_warnings else [row for row in rows if not row.decoder_warning]
    x_values = [row.median_saturation for row in plotted_rows]
    y_values = [row.dark_ratio * 100.0 for row in plotted_rows]
    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = min(y_values), max(y_values)
    x_pad = max(2.0, (x_max - x_min) * 0.05)
    y_pad = max(1.0, (y_max - y_min) * 0.05)
    x_min, x_max = max(0.0, x_min - x_pad), min(255.0, x_max + x_pad)
    y_min, y_max = max(0.0, y_min - y_pad), min(100.0, y_max + y_pad)
    if math.isclose(x_min, x_max):
        x_max = x_min + 1.0
    if math.isclose(y_min, y_max):
        y_max = y_min + 1.0

    def sx(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * plot_width

    def sy(value: float) -> float:
        return top + plot_height - (value - y_min) / (y_max - y_min) * plot_height

    status = "X = decoder warning" if include_decoder_warnings else "decoder warnings excluded"
    draw.text(
        (left, 28),
        f"Daylight features · {start.isoformat()}–{end.isoformat()} · {len(plotted_rows)} videos · {status}",
        fill="#1f2937",
        font=title_font,
    )
    draw.rectangle((left, top, left + plot_width, top + plot_height), outline="#6b7280", width=2)

    for index in range(6):
        x_value = x_min + (x_max - x_min) * index / 5
        y_value = y_min + (y_max - y_min) * index / 5
        x = sx(x_value)
        y = sy(y_value)
        draw.line((x, top, x, top + plot_height), fill="#e5e7eb", width=1)
        draw.line((left, y, left + plot_width, y), fill="#e5e7eb", width=1)
        x_label = f"{x_value:.0f}"
        y_label = f"{y_value:.0f}%"
        x_box = draw.textbbox((0, 0), x_label, font=tick_font)
        y_box = draw.textbbox((0, 0), y_label, font=tick_font)
        draw.text((x - (x_box[2] - x_box[0]) / 2, top + plot_height + 12), x_label, fill="#374151", font=tick_font)
        draw.text((left - (y_box[2] - y_box[0]) - 12, y - (y_box[3] - y_box[1]) / 2), y_label, fill="#374151", font=tick_font)

    days = sorted({row.recording_date for row in plotted_rows})
    palette = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9"]
    colors = {day: palette[index % len(palette)] for index, day in enumerate(days)}
    for row in plotted_rows:
        x, y = sx(row.median_saturation), sy(row.dark_ratio * 100.0)
        if row.decoder_warning:
            draw.line((x - 6, y - 6, x + 6, y + 6), fill="#4b5563", width=3)
            draw.line((x - 6, y + 6, x + 6, y - 6), fill="#4b5563", width=3)
            continue
        color = colors[row.recording_date]
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=color + "B8", outline=color + "E6")

    x_title = "Median HSV saturation (0-255)"
    x_box = draw.textbbox((0, 0), x_title, font=axis_font)
    draw.text((left + (plot_width - (x_box[2] - x_box[0])) / 2, canvas_height - 43), x_title, fill="#111827", font=axis_font)
    y_title = "Dark pixels, Y < 40 (%)"
    y_layer = Image.new("RGBA", (canvas_height, 44), (255, 255, 255, 0))
    y_draw = ImageDraw.Draw(y_layer)
    y_draw.text((0, 8), y_title, fill="#111827", font=axis_font)
    y_rotated = y_layer.rotate(90, expand=True)
    image.paste(y_rotated, (18, (canvas_height - y_rotated.height) // 2), y_rotated)

    legend_x = left + plot_width - 190
    legend_y = top + 16
    for index, day in enumerate(days):
        y = legend_y + index * 25
        draw.ellipse((legend_x, y + 4, legend_x + 12, y + 16), fill=colors[day])
        draw.text((legend_x + 20, y), day, fill="#111827", font=tick_font)

    image.save(output)


def write_csv(path: Path, rows: list[object]) -> None:
    if not rows:
        return
    dictionaries = [asdict(row) for row in rows]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(dictionaries[0]))
        writer.writeheader()
        writer.writerows(dictionaries)


def main() -> int:
    args = parse_args()
    videos = list(iter_videos(args.input, args.start_date, args.end_date))
    if not videos:
        print("No videos matched the requested date range", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    rows: list[VideoFeatures] = []
    failures: list[Failure] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(analyze_video, path, timestamp, args): path
            for path, timestamp in videos
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            path = futures[future]
            try:
                rows.append(future.result())
            except Exception as exc:  # Continue across damaged camera clips.
                failures.append(Failure(path=str(path), error=str(exc)))
            if completed % 25 == 0 or completed == len(videos):
                print(f"processed {completed}/{len(videos)}", flush=True)

    rows.sort(key=lambda row: (row.recording_time, row.path))
    failures.sort(key=lambda failure: failure.path)
    write_csv(args.output / "video_features.csv", rows)
    write_csv(args.output / "failures.csv", failures)
    summary = summarize(rows, failures, args)
    (args.output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if rows:
        draw_scatter(
            rows,
            args.output / "scatter.png",
            args.start_date,
            args.end_date,
            include_decoder_warnings=False,
        )
        draw_scatter(
            rows,
            args.output / "scatter_all.png",
            args.start_date,
            args.end_date,
            include_decoder_warnings=True,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
