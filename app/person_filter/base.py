"""Provider-neutral interface for local person detection.

The detector is deliberately narrow: it turns a list of JPEG paths into one
score per frame and nothing else.  Deciding which frames survive is a separate,
pure function (:mod:`app.person_filter.selection`), so the selection rules can
be unit tested without a model and the model can be swapped without touching
the rules.

``saturation`` travels with the score because the detector already decodes each
image.  Reolink switches to IR at night, and an RGB-trained detector loses
recall on that monochrome video, so the threshold has to differ between the two
cases.  Measuring the colour signal in the same pass avoids decoding every JPEG
twice just to answer "is this clip monochrome?".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class PersonDetectorError(RuntimeError):
    """Base error for local detector configuration and inference failures."""


class PersonDetectorConfigurationError(PersonDetectorError):
    """The configured detector cannot be constructed.

    This is raised for operator mistakes -- a missing optional dependency, a
    model directory that was never baked into the image, an unknown backend --
    and is intentionally fatal.  Continuing would either send every frame at
    full cost or drop frames using an unverified model, and neither failure is
    visible in the daily report.
    """


class PersonDetectionError(PersonDetectorError):
    """Inference failed after the detector had been constructed."""


@dataclass(frozen=True, slots=True)
class FrameDetection:
    """What one frame looked like to the detector.

    ``score`` is the highest person confidence in the frame, in ``0..1``; a
    frame with no person box at all scores ``0``.  ``saturation`` is the mean
    chroma of the image in ``0..1`` and is near zero for IR footage.
    """

    score: float
    saturation: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be between 0 and 1")
        if not 0.0 <= self.saturation <= 1.0:
            raise ValueError("saturation must be between 0 and 1")


class PersonDetector(ABC):
    """Score every supplied frame for the presence of a person."""

    name: str
    model: str

    @abstractmethod
    def detect(self, frames: Sequence[Path]) -> tuple[FrameDetection, ...]:
        """Return one detection per input frame, in the same order.

        Implementations must return exactly ``len(frames)`` results so the
        caller can map scores back onto the extracted frame sequence by index.
        """

        raise NotImplementedError

    def close(self) -> None:
        """Release detector resources, if any."""


__all__ = [
    "FrameDetection",
    "PersonDetectionError",
    "PersonDetector",
    "PersonDetectorConfigurationError",
    "PersonDetectorError",
]
