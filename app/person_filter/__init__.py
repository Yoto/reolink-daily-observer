"""Local person detection used to drop frames before they reach the provider."""

from app.person_filter.base import (
    FrameDetection,
    PersonDetectionError,
    PersonDetector,
    PersonDetectorConfigurationError,
    PersonDetectorError,
)
from app.person_filter.factory import create_detector
from app.person_filter.filter import FrameFilterOutcome, PersonFrameFilter
from app.person_filter.selection import (
    ScoreDistribution,
    SelectionRules,
    merge_runs,
    person_runs,
    score_distribution,
    select_frame_indices,
)

__all__ = [
    "FrameDetection",
    "FrameFilterOutcome",
    "PersonDetectionError",
    "PersonDetector",
    "PersonDetectorConfigurationError",
    "PersonDetectorError",
    "PersonFrameFilter",
    "ScoreDistribution",
    "SelectionRules",
    "create_detector",
    "merge_runs",
    "person_runs",
    "score_distribution",
    "select_frame_indices",
]
