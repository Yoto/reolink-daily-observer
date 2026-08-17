"""The frame-selection stage that sits between ffmpeg and the provider."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Sequence

from app.config import AppSettings
from app.models import FrameFilterMetadata, FrameFilterStatus
from app.person_filter.base import (
    FrameDetection,
    PersonDetectionError,
    PersonDetector,
)
from app.person_filter.factory import create_detector
from app.person_filter.selection import (
    Lighting,
    SelectionRules,
    score_distribution,
    select_frame_indices,
)
from app.video import ExtractedFrame


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FrameFilterOutcome:
    """The frames to send onward, plus the accounting for how they were chosen."""

    frames: tuple[ExtractedFrame, ...]
    metadata: FrameFilterMetadata


class PersonFrameFilter:
    """Decide which extracted frames are worth an API call.

    Construction is eager and fatal on a bad configuration: a missing model
    directory or a missing optional dependency is an operator mistake that
    should surface before the run spends anything, not on the first video.
    Inference failures during a run are treated differently -- see
    :meth:`select`.
    """

    def __init__(
        self,
        settings: AppSettings,
        detector: PersonDetector | None = None,
    ) -> None:
        self.settings = settings.person_filter
        self._interval_sec = settings.frames.interval_sec
        self._detector: PersonDetector | None = None
        if self.settings.enabled:
            self._detector = detector or create_detector(self.settings)

    def select(self, frames: Sequence[ExtractedFrame]) -> FrameFilterOutcome:
        """Return the frames to analyse and a record of how that was decided."""

        extracted = tuple(frames)
        if self._detector is None or not extracted:
            return FrameFilterOutcome(
                frames=extracted,
                metadata=FrameFilterMetadata(
                    status=FrameFilterStatus.DISABLED,
                    frames_extracted=len(extracted),
                    # "Selected everything" rather than "selected nothing", so
                    # aggregating a day of records does not read a disabled
                    # filter as a total saving.
                    frames_selected=len(extracted),
                    frames_sent=len(extracted),
                ),
            )

        started = time.perf_counter()
        try:
            detections = self._detector.detect([frame.path for frame in extracted])
            if len(detections) != len(extracted):
                raise PersonDetectionError(
                    f"detector returned {len(detections)} results "
                    f"for {len(extracted)} frames"
                )
        except PersonDetectionError as exc:
            # Fail open. Sending every frame costs money; dropping frames on the
            # strength of a detector that just failed costs evidence, and the
            # loss would be invisible in the report. The status and the message
            # are recorded so the extra spend is attributable afterwards.
            LOGGER.error(
                "person filter failed, sending every frame frames=%d: %s",
                len(extracted),
                exc,
            )
            return FrameFilterOutcome(
                frames=extracted,
                metadata=FrameFilterMetadata(
                    status=FrameFilterStatus.FAILED,
                    detector=self._detector.name,
                    model=self._detector.model,
                    frames_extracted=len(extracted),
                    frames_selected=len(extracted),
                    frames_sent=len(extracted),
                    detection_sec=time.perf_counter() - started,
                    error=str(exc)[:1000],
                ),
            )
        detection_sec = time.perf_counter() - started

        scores = [detection.score for detection in detections]
        lighting, saturation = _classify_lighting(
            detections, self.settings.night_saturation_threshold
        )
        threshold = (
            self.settings.score_threshold_night
            if lighting == "night"
            else self.settings.score_threshold_day
        )
        rules = SelectionRules.from_seconds(
            threshold=threshold,
            merge_gap_sec=self.settings.merge_gap_sec,
            padding_sec=self.settings.padding_sec,
            keep_first_last=self.settings.keep_first_last_frame,
            interval_sec=self._interval_sec,
        )
        indices = select_frame_indices(scores, rules)
        if not indices:
            # Only reachable with keep_first_last_frame disabled and nothing
            # detected. An event still has to be produced from something, and
            # one frame is the smallest honest answer to "what did the camera
            # record?".
            LOGGER.warning(
                "person filter selected no frames; keeping the first frame "
                "frames=%d threshold=%.2f",
                len(extracted),
                threshold,
            )
            indices = (0,)

        distribution = score_distribution(scores)
        selected = tuple(extracted[index] for index in indices)
        sent = extracted if self.settings.dry_run else selected
        metadata = FrameFilterMetadata(
            status=(
                FrameFilterStatus.DRY_RUN
                if self.settings.dry_run
                else FrameFilterStatus.APPLIED
            ),
            detector=self._detector.name,
            model=self._detector.model,
            lighting=lighting,
            saturation=saturation,
            score_threshold=threshold,
            frames_extracted=len(extracted),
            frames_selected=len(selected),
            frames_sent=len(sent),
            reduction_ratio=1.0 - (len(selected) / len(extracted)),
            score_max=distribution.max_score,
            score_median=distribution.median_score,
            score_histogram=list(distribution.histogram),
            detection_sec=detection_sec,
        )
        LOGGER.info(
            "person filter status=%s extracted=%d selected=%d sent=%d "
            "reduction=%.3f lighting=%s saturation=%.3f threshold=%.2f "
            "score_max=%.3f score_median=%.3f histogram=%s "
            "merge_gap_frames=%d padding_frames=%d detection_sec=%.3f",
            metadata.status,
            metadata.frames_extracted,
            metadata.frames_selected,
            metadata.frames_sent,
            metadata.reduction_ratio,
            lighting,
            saturation,
            threshold,
            distribution.max_score,
            distribution.median_score,
            ",".join(str(count) for count in distribution.histogram),
            rules.merge_gap_frames,
            rules.padding_frames,
            detection_sec,
        )
        return FrameFilterOutcome(frames=sent, metadata=metadata)

    def close(self) -> None:
        if self._detector is not None:
            self._detector.close()
            self._detector = None


def _classify_lighting(
    detections: Sequence[FrameDetection],
    night_saturation_threshold: float,
) -> tuple[Lighting, float]:
    """Call the whole clip day or night from its mean chroma.

    One verdict per video rather than per frame: a clip that straddles the
    camera's IR switch would otherwise change threshold halfway through and
    make the resulting selection impossible to reason about.
    """

    saturation = sum(detection.saturation for detection in detections) / len(detections)
    lighting: Lighting = "night" if saturation <= night_saturation_threshold else "day"
    return lighting, saturation


__all__ = ["FrameFilterOutcome", "PersonFrameFilter"]
