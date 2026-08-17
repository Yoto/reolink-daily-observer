from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models import APIUsage
from app.state import (
    CacheKey,
    FileFingerprint,
    ProcessingStatus,
    StateStore,
)


def make_key(fingerprint: FileFingerprint, *, model: str = "model-v1") -> CacheKey:
    return CacheKey(
        fingerprint=fingerprint,
        provider="mock",
        model=model,
        prompt_version="event_observation_v1",
        schema_version="1.0",
    )


def test_fingerprint_is_relative_and_can_include_hash(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    day = input_root / "2026-08-16"
    day.mkdir(parents=True)
    video = day / "clip.mp4"
    video.write_bytes(b"video bytes")

    fingerprint = FileFingerprint.from_path(
        video, input_root=input_root, compute_hash=True
    )
    assert fingerprint.source_path == "2026-08-16/clip.mp4"
    assert fingerprint.size_bytes == len(b"video bytes")
    assert fingerprint.content_hash is not None
    assert len(fingerprint.content_hash) == 64

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="outside input root"):
        FileFingerprint.from_path(outside, input_root=input_root)


def test_completed_exact_key_is_reused_and_changed_model_is_not(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    event_file = output / "event.json"
    event_file.write_text("{}", encoding="utf-8")
    fingerprint = FileFingerprint(
        source_path="2026-08-16/clip.mp4",
        size_bytes=100,
        mtime_ns=123,
    )
    key = make_key(fingerprint)

    with StateStore(tmp_path / "state.sqlite", output_root=output) as store:
        claim = store.claim_processing(key, event_id="event-1")
        assert claim.should_process
        completed = store.mark_completed(
            claim.record.id,
            event_json_path="event.json",
            recording_time=datetime(2026, 8, 16, 1, tzinfo=timezone.utc),
            usage=APIUsage(
                input_tokens=12,
                output_tokens=4,
                request_count=1,
                api_processing_sec=0.25,
            ),
        )
        assert completed.status == ProcessingStatus.COMPLETED.value
        assert completed.usage.total_tokens == 16

        cached_claim = store.claim_processing(key, event_id="event-1")
        assert cached_claim.cached
        assert cached_claim.record.id == completed.id
        assert cached_claim.record.attempt_count == 1

        changed_model_claim = store.claim_processing(
            make_key(fingerprint, model="model-v2"),
            event_id="event-1",
        )
        assert changed_model_claim.should_process
        assert changed_model_claim.record.id != completed.id


def test_missing_cached_artifact_and_force_trigger_reprocessing(tmp_path: Path) -> None:
    fingerprint = FileFingerprint(source_path="day/a.mp4", size_bytes=5, mtime_ns=6)
    key = make_key(fingerprint)
    event_path = tmp_path / "event.json"
    event_path.write_text("{}", encoding="utf-8")

    with StateStore(tmp_path / "state.sqlite") as store:
        claim = store.claim_processing(key, event_id="event-a")
        store.mark_completed(claim.record.id, event_json_path=event_path)

        forced = store.claim_processing(key, event_id="event-a", force=True)
        assert forced.should_process
        assert forced.record.attempt_count == 2
        store.mark_completed(forced.record.id, event_json_path=event_path)

        event_path.unlink()
        stale = store.claim_processing(key, event_id="event-a")
        assert stale.should_process
        assert stale.record.attempt_count == 3


def test_failure_is_persisted_without_becoming_cache_hit(tmp_path: Path) -> None:
    fingerprint = FileFingerprint(source_path="day/b.mp4", size_bytes=10, mtime_ns=20)
    key = make_key(fingerprint)
    database = tmp_path / "nested" / "state.sqlite"

    with StateStore(database) as store:
        claim = store.claim_processing(key, event_id="event-b")
        failed = store.mark_failed(
            claim.record.id,
            error=TimeoutError("provider timed out"),
            usage=APIUsage(request_count=3, api_processing_sec=12.5),
        )
        assert failed.status == ProcessingStatus.FAILED.value
        assert failed.error_type == "TimeoutError"
        assert failed.error_message == "provider timed out"
        assert failed.usage.request_count == 3
        assert store.get_cached_record(key, require_event_file=False) is None

    # State survives a new one-shot process opening the database.
    with StateStore(database) as reopened:
        records = reopened.list_records(source_prefix="day/")
        assert len(records) == 1
        assert records[0].status == ProcessingStatus.FAILED.value
        assert reopened.status_counts(source_prefix="day/")["failed"] == 1


def test_file_change_gets_a_distinct_cache_key(tmp_path: Path) -> None:
    original = FileFingerprint(source_path="day/c.mp4", size_bytes=100, mtime_ns=1)
    changed = FileFingerprint(source_path="day/c.mp4", size_bytes=101, mtime_ns=2)
    event_path = tmp_path / "event.json"
    event_path.write_text("{}", encoding="utf-8")

    with StateStore(tmp_path / "state.sqlite") as store:
        first = store.claim_processing(make_key(original), event_id="event-c")
        store.mark_completed(first.record.id, event_json_path=event_path)
        second = store.claim_processing(make_key(changed), event_id="event-c")
        assert second.should_process
        assert second.record.id != first.record.id


def test_source_path_must_be_relative() -> None:
    with pytest.raises(ValidationError, match="relative"):
        FileFingerprint(source_path="/data/input/day/a.mp4", size_bytes=1, mtime_ns=1)
    with pytest.raises(ValidationError, match="relative"):
        FileFingerprint(source_path="day/../a.mp4", size_bytes=1, mtime_ns=1)
