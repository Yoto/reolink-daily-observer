"""Which extracted frames survive, expressed as pure functions over scores.

Frames are never accepted or rejected one at a time.  A detector misses a
person for a frame or two in the middle of an approach, and cutting on the raw
per-frame verdict would punch holes in the sequence exactly where the movement
is -- the VLM would then see someone at the gate and someone at the door with
the walk between them deleted.  So above-threshold frames are grouped into
runs, short gaps between runs are filled, and every run is padded on both sides
to keep the "approaching" and "leaving" context that makes an event readable.

Nothing here imports a model or touches the filesystem: the input is a list of
scores and the output is a list of indices.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Literal, Sequence


Lighting = Literal["day", "night"]

# Fixed 0.1-wide bins. A stable bin layout matters more than a clever one here:
# the histograms are written to SQLite so thresholds can be re-tuned later from
# many days of runs, and re-binned history would not be comparable.
HISTOGRAM_BIN_COUNT = 10


@dataclass(frozen=True, slots=True)
class SelectionRules:
    """Selection parameters already converted to frame units."""

    threshold: float
    merge_gap_frames: int
    padding_frames: int
    keep_first_last: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if self.merge_gap_frames < 0:
            raise ValueError("merge_gap_frames cannot be negative")
        if self.padding_frames < 0:
            raise ValueError("padding_frames cannot be negative")

    @classmethod
    def from_seconds(
        cls,
        *,
        threshold: float,
        merge_gap_sec: float,
        padding_sec: float,
        keep_first_last: bool,
        interval_sec: float,
    ) -> SelectionRules:
        """Convert second-based configuration using the frame sample interval.

        Rounding is upward so a configured window is never silently shortened
        by a sample interval that does not divide it evenly.
        """

        if not math.isfinite(interval_sec) or interval_sec <= 0:
            raise ValueError("interval_sec must be a finite positive number")
        if merge_gap_sec < 0 or padding_sec < 0:
            raise ValueError("merge_gap_sec and padding_sec cannot be negative")
        return cls(
            threshold=threshold,
            merge_gap_frames=math.ceil(merge_gap_sec / interval_sec),
            padding_frames=math.ceil(padding_sec / interval_sec),
            keep_first_last=keep_first_last,
        )


@dataclass(frozen=True, slots=True)
class ScoreDistribution:
    """Compact summary of one video's detection scores.

    Recorded per video so the reduction rate can be attributed to a threshold
    afterwards.  Without it, a day of runs only says how many frames were cut,
    not how close the discarded frames were to being kept.
    """

    count: int
    max_score: float
    median_score: float
    histogram: tuple[int, ...]

    def payload(self) -> dict[str, object]:
        return {
            "count": self.count,
            "max": round(self.max_score, 4),
            "median": round(self.median_score, 4),
            "histogram": list(self.histogram),
        }


def score_distribution(scores: Sequence[float]) -> ScoreDistribution:
    """Summarize scores as max, median, and fixed 0.1-wide bins."""

    if not scores:
        return ScoreDistribution(
            count=0,
            max_score=0.0,
            median_score=0.0,
            histogram=(0,) * HISTOGRAM_BIN_COUNT,
        )
    bins = [0] * HISTOGRAM_BIN_COUNT
    for score in scores:
        index = min(int(score * HISTOGRAM_BIN_COUNT), HISTOGRAM_BIN_COUNT - 1)
        bins[max(index, 0)] += 1
    return ScoreDistribution(
        count=len(scores),
        max_score=max(scores),
        median_score=float(statistics.median(scores)),
        histogram=tuple(bins),
    )


def person_runs(scores: Sequence[float], threshold: float) -> list[tuple[int, int]]:
    """Return inclusive ``(start, end)`` runs of frames at or above *threshold*."""

    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, score in enumerate(scores):
        if score >= threshold:
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(scores) - 1))
    return runs


def merge_runs(runs: Sequence[tuple[int, int]], gap_frames: int) -> list[tuple[int, int]]:
    """Join runs separated by at most *gap_frames* undetected frames."""

    merged: list[tuple[int, int]] = []
    for start, end in runs:
        if merged and start - merged[-1][1] - 1 <= gap_frames:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def select_frame_indices(
    scores: Sequence[float],
    rules: SelectionRules,
) -> tuple[int, ...]:
    """Return the indices of the frames worth sending, in ascending order.

    The first and last frame are kept unconditionally when
    ``rules.keep_first_last`` is set.  A parcel left on the doorstep is only
    visible after the person who left it has walked out of frame, and those two
    images cost almost nothing next to the risk of losing the outcome of the
    event entirely.
    """

    if not scores:
        return ()

    last_index = len(scores) - 1
    selected: set[int] = set()
    for start, end in merge_runs(person_runs(scores, rules.threshold), rules.merge_gap_frames):
        padded_start = max(0, start - rules.padding_frames)
        padded_end = min(last_index, end + rules.padding_frames)
        selected.update(range(padded_start, padded_end + 1))

    if rules.keep_first_last:
        selected.add(0)
        selected.add(last_index)
    return tuple(sorted(selected))


__all__ = [
    "HISTOGRAM_BIN_COUNT",
    "Lighting",
    "ScoreDistribution",
    "SelectionRules",
    "merge_runs",
    "person_runs",
    "score_distribution",
    "select_frame_indices",
]
