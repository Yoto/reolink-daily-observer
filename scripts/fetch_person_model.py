"""Download the person-detection weights during the image build.

Run only at build time. The daily batch starts unattended at 04:00 and must not
depend on Hugging Face being reachable, so the container image carries the
weights and the runtime loads them with ``local_files_only=True``.

    python scripts/fetch_person_model.py hustvl/yolos-tiny /opt/analyzer/models/person-detector
"""

from __future__ import annotations

import sys
from pathlib import Path


# Alternative weight formats for runtimes this image does not have. Fetching
# everything a repository happens to contain would put a second and third copy
# of the same weights in the image for no benefit.
IGNORE_PATTERNS = (
    "*.msgpack",
    "*.h5",
    "*.onnx",
    "*.onnx_data",
    "*.tflite",
    "*.ot",
)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    repository, destination = argv[0], Path(argv[1])

    from huggingface_hub import snapshot_download
    from transformers import AutoImageProcessor, AutoModelForObjectDetection

    from app.person_filter.base import PersonDetectorConfigurationError
    from app.person_filter.huggingface import person_label_id

    destination.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repository,
        local_dir=destination,
        ignore_patterns=list(IGNORE_PATTERNS),
    )

    # Load once here so a repository that is missing a file, or that cannot be
    # read by the installed transformers version, fails the build instead of
    # the first daily run.
    AutoImageProcessor.from_pretrained(destination, local_files_only=True)
    model = AutoModelForObjectDetection.from_pretrained(
        destination, local_files_only=True
    )
    try:
        label_id = person_label_id(model.config.id2label)
    except PersonDetectorConfigurationError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"{repository} -> {destination} "
        f"({len(model.config.id2label)} classes, person={label_id})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
