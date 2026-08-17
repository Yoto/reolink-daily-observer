"""CPU person detection with a Hugging Face object-detection model.

The heavy imports live inside the constructor.  ``app.person_filter`` is
imported by the pipeline unconditionally, and pulling in torch just to discover
that ``person_filter.enabled`` is ``false`` would add seconds to every run and
make the test suite depend on a two-gigabyte optional dependency.

Weights are read from a local directory only.  The daily batch runs unattended
at 04:00 and must not fail because Hugging Face is unreachable, so the image
build bakes the model in (see ``scripts/fetch_person_model.py``) and nothing is
downloaded at run time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Sequence

from app.person_filter.base import (
    FrameDetection,
    PersonDetectionError,
    PersonDetector,
    PersonDetectorConfigurationError,
)


LOGGER = logging.getLogger(__name__)
# Chroma is measured on a thumbnail. Whether a clip is IR footage is a
# whole-image property, and a 64px copy answers it for a fraction of the cost
# of scanning a 1280px JPEG.
_SATURATION_THUMBNAIL_PX = 64
_PERSON_LABELS = ("person", "people", "pedestrian")


class HuggingFacePersonDetector(PersonDetector):
    """Score frames with a local ``AutoModelForObjectDetection`` checkpoint."""

    name = "huggingface"

    def __init__(
        self,
        *,
        model: str,
        model_dir: Path,
        batch_size: int = 8,
        threads: int = 0,
    ) -> None:
        self.model = model
        self._model_dir = Path(model_dir)
        self._batch_size = max(1, int(batch_size))
        if not self._model_dir.is_dir():
            raise PersonDetectorConfigurationError(
                f"person detector weights are not present at {self._model_dir}; "
                "they are baked in at image build time, so rebuild the image or "
                "set person_filter.enabled=false"
            )

        try:
            import torch
            from PIL import Image
            from transformers import AutoImageProcessor, AutoModelForObjectDetection
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise PersonDetectorConfigurationError(
                "person_filter is enabled but its optional dependencies are "
                f"missing ({exc}); install the 'detect' extra or set "
                "person_filter.enabled=false"
            ) from exc

        self._torch = torch
        self._image = Image
        if threads > 0:
            torch.set_num_threads(threads)

        try:
            # ``use_fast`` is pinned rather than left to the library default:
            # the fast path needs torchvision, which this image does not carry,
            # and a default that flips on upgrade would quietly change every
            # score this stage produces.
            self._processor = AutoImageProcessor.from_pretrained(
                self._model_dir, local_files_only=True, use_fast=False
            )
            self._model = AutoModelForObjectDetection.from_pretrained(
                self._model_dir, local_files_only=True
            )
        except Exception as exc:
            raise PersonDetectorConfigurationError(
                f"cannot load person detector from {self._model_dir}: {exc}"
            ) from exc
        self._model.eval()

        self._person_id = person_label_id(self._model.config.id2label)
        LOGGER.info(
            "person detector ready model=%s dir=%s person_label_id=%d threads=%d",
            self.model,
            self._model_dir,
            self._person_id,
            torch.get_num_threads(),
        )

    def detect(self, frames: Sequence[Path]) -> tuple[FrameDetection, ...]:
        if not frames:
            return ()
        results: list[FrameDetection] = []
        for start in range(0, len(frames), self._batch_size):
            batch = list(frames[start : start + self._batch_size])
            results.extend(self._detect_batch(batch))
        return tuple(results)

    def _detect_batch(self, paths: Sequence[Path]) -> list[FrameDetection]:
        try:
            images = [self._image.open(path).convert("RGB") for path in paths]
        except OSError as exc:
            raise PersonDetectionError(f"cannot read extracted frame: {exc}") from exc

        try:
            saturations = [self._saturation(image) for image in images]
            inputs = self._processor(images=images, return_tensors="pt")
            with self._torch.inference_mode():
                outputs = self._model(**inputs)
            scores = self._person_scores(outputs.logits)
        except PersonDetectionError:
            raise
        except Exception as exc:
            raise PersonDetectionError(f"person detection failed: {exc}") from exc
        finally:
            for image in images:
                image.close()

        return [
            FrameDetection(score=score, saturation=saturation)
            for score, saturation in zip(scores, saturations, strict=True)
        ]

    def _person_scores(self, logits: Any) -> list[float]:
        """Highest person confidence per image in the batch.

        Architectures with a trailing "no object" class are decoded with a
        softmax over classes; the sigmoid family has one independent score per
        class instead. Reading the width of the logits distinguishes them more
        reliably than matching on the model type, which changes with every new
        model family.
        """

        if logits.shape[-1] == len(self._model.config.id2label) + 1:
            probabilities = logits.softmax(-1)[..., :-1]
        else:
            probabilities = logits.sigmoid()
        # Deliberately the maximum over every query rather than the top-scoring
        # class per query: a frame where "person" is the runner-up behind
        # "backpack" still contains a person, and this stage only has to decide
        # whether the frame is worth showing the VLM.
        best = probabilities[..., self._person_id].amax(dim=-1)
        return [min(max(float(value), 0.0), 1.0) for value in best]

    def _saturation(self, image: Any) -> float:
        thumbnail = image.copy()
        thumbnail.thumbnail(
            (_SATURATION_THUMBNAIL_PX, _SATURATION_THUMBNAIL_PX),
            self._image.Resampling.BILINEAR,
        )
        channel = thumbnail.convert("HSV").getchannel("S")
        try:
            histogram = channel.histogram()
            total = sum(histogram)
            if total <= 0:
                return 0.0
            weighted = sum(value * count for value, count in enumerate(histogram))
            return min(max(weighted / (total * 255.0), 0.0), 1.0)
        finally:
            channel.close()
            thumbnail.close()

    def close(self) -> None:
        self._model = None
        self._processor = None


def person_label_id(id2label: dict[int, str]) -> int:
    """Return the class index this checkpoint uses for people.

    Also called by the build-time download script, so a checkpoint that cannot
    detect a person fails the image build rather than quietly rejecting every
    frame at 04:00.
    """

    for identifier, label in id2label.items():
        if str(label).strip().lower() in _PERSON_LABELS:
            return int(identifier)
    raise PersonDetectorConfigurationError(
        "the configured detection model has no person class; "
        f"labels: {sorted(str(label) for label in id2label.values())[:20]}"
    )


__all__ = ["HuggingFacePersonDetector", "person_label_id"]
