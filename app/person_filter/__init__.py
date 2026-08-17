"""Local person detection used to drop frames before they reach the provider."""

from app.person_filter.base import (
    FrameDetection,
    PersonDetectionError,
    PersonDetector,
    PersonDetectorConfigurationError,
    PersonDetectorError,
)
from app.person_filter.factory import create_detector

__all__ = [
    "FrameDetection",
    "PersonDetectionError",
    "PersonDetector",
    "PersonDetectorConfigurationError",
    "PersonDetectorError",
    "create_detector",
]
