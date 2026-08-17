"""Detector construction kept separate from the selection rules."""

from __future__ import annotations

from app.config import PersonFilterSettings
from app.person_filter.base import PersonDetector, PersonDetectorConfigurationError


def create_detector(settings: PersonFilterSettings) -> PersonDetector:
    """Build the configured detector, importing its backend only when used."""

    if settings.backend != "huggingface":
        raise PersonDetectorConfigurationError(
            f"unsupported person_filter.backend={settings.backend!r}; "
            "supported: huggingface"
        )
    # Imported here so torch and transformers stay optional for anyone running
    # with person_filter disabled, and for the test suite.
    from app.person_filter.huggingface import HuggingFacePersonDetector

    return HuggingFacePersonDetector(
        model=settings.model,
        model_dir=settings.model_dir,
        batch_size=settings.batch_size,
        threads=settings.threads,
    )


__all__ = ["create_detector"]
