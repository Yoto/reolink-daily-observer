"""SQLite-backed idempotency and processing metadata.

The cache identity includes both the source fingerprint and every component
that can change the semantic output. A changed model, prompt, or schema is
therefore reprocessed without deleting older state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Self

from pydantic import Field, field_validator

from app.models import APIUsage, NonEmptyText, SchemaModel


_STATE_SCHEMA_VERSION = 1


class ProcessingStatus(str, Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class FileFingerprint(SchemaModel):
    """Immutable input identity using a path relative to the mounted input root."""

    source_path: NonEmptyText
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    content_hash: str | None = None

    @field_validator("source_path")
    @classmethod
    def normalize_relative_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = PurePosixPath(normalized)
        if normalized in {"", "."} or path.is_absolute() or ".." in path.parts:
            raise ValueError("source_path must be relative to the input root")
        if len(normalized) >= 2 and normalized[1] == ":":
            raise ValueError("source_path must not contain a host drive path")
        return path.as_posix()

    @field_validator("content_hash")
    @classmethod
    def normalize_hash(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if not all(character in "0123456789abcdef" for character in normalized):
            raise ValueError("content_hash must be hexadecimal")
        return normalized

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        input_root: str | Path,
        compute_hash: bool = False,
    ) -> FileFingerprint:
        source = Path(path).resolve(strict=True)
        root = Path(input_root).resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"source path is not a regular file: {source}")
        try:
            relative = source.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"source file {source} is outside input root {root}") from exc
        stat = source.stat()
        digest: str | None = None
        if compute_hash:
            hasher = hashlib.sha256()
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
        return cls(
            source_path=PurePosixPath(*relative.parts).as_posix(),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            content_hash=digest,
        )


class CacheKey(SchemaModel):
    fingerprint: FileFingerprint
    provider: NonEmptyText
    model: NonEmptyText
    prompt_version: NonEmptyText
    schema_version: NonEmptyText


class ProcessingRecord(SchemaModel):
    id: int = Field(gt=0)
    fingerprint: FileFingerprint
    source_filename: NonEmptyText
    event_id: str | None = None
    recording_time: datetime | None = None
    status: ProcessingStatus
    provider: NonEmptyText
    model: NonEmptyText
    prompt_version: NonEmptyText
    schema_version: NonEmptyText
    event_json_path: str | None = None
    usage: APIUsage = Field(default_factory=APIUsage)
    error_type: str | None = None
    error_message: str | None = None
    attempt_count: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    processed_at: datetime | None = None

    @field_validator("recording_time", "created_at", "updated_at", "processed_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("state timestamps must be timezone-aware")
        return value

    @property
    def cache_key(self) -> CacheKey:
        return CacheKey(
            fingerprint=self.fingerprint,
            provider=self.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
        )


@dataclass(frozen=True, slots=True)
class ProcessingClaim:
    record: ProcessingRecord
    should_process: bool

    @property
    def cached(self) -> bool:
        return not self.should_process


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.utcoffset() is None:
        raise ValueError("timestamps persisted to state must be timezone-aware")
    return value.isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


class StateStore:
    """Small transactional state store suitable for one-shot container runs."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        output_root: str | Path | None = None,
        timeout_sec: float = 30.0,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_root = Path(output_root) if output_root is not None else None
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database_path,
            timeout=timeout_sec,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize(self) -> None:
        with self._lock:
            current_version = int(
                self._connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version not in (0, _STATE_SCHEMA_VERSION):
                raise RuntimeError(
                    f"unsupported state database version {current_version}; "
                    f"expected {_STATE_SCHEMA_VERSION}"
                )
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS processing_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                    mtime_ns INTEGER NOT NULL CHECK (mtime_ns >= 0),
                    content_hash TEXT NOT NULL DEFAULT '',
                    event_id TEXT,
                    recording_time TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('processing', 'completed', 'failed')
                    ),
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    event_json_path TEXT,
                    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
                    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
                    total_tokens INTEGER CHECK (total_tokens IS NULL OR total_tokens >= 0),
                    request_count INTEGER NOT NULL DEFAULT 0 CHECK (request_count >= 0),
                    api_processing_sec REAL CHECK (
                        api_processing_sec IS NULL OR api_processing_sec >= 0
                    ),
                    estimated_cost REAL CHECK (
                        estimated_cost IS NULL OR estimated_cost >= 0
                    ),
                    cost_currency TEXT,
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    error_type TEXT,
                    error_message TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    processed_at TEXT,
                    UNIQUE (
                        source_path, size_bytes, mtime_ns, content_hash,
                        provider, model, prompt_version, schema_version
                    )
                );

                CREATE INDEX IF NOT EXISTS idx_processing_source
                    ON processing_records(source_path, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_processing_status
                    ON processing_records(status, updated_at DESC);
                """
            )
            self._connection.execute(f"PRAGMA user_version = {_STATE_SCHEMA_VERSION}")

    @staticmethod
    def _key_parameters(key: CacheKey) -> tuple[Any, ...]:
        fingerprint = key.fingerprint
        return (
            fingerprint.source_path,
            fingerprint.size_bytes,
            fingerprint.mtime_ns,
            fingerprint.content_hash or "",
            key.provider,
            key.model,
            key.prompt_version,
            key.schema_version,
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ProcessingRecord:
        usage_payload: dict[str, Any]
        try:
            usage_payload = json.loads(row["usage_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            # Scalar columns remain a recoverable representation for databases
            # produced by an interrupted/older writer.
            usage_payload = {}
        scalar_usage = {
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "total_tokens": row["total_tokens"],
            "request_count": row["request_count"],
            "api_processing_sec": row["api_processing_sec"],
            "estimated_cost": row["estimated_cost"],
            "cost_currency": row["cost_currency"],
        }
        # None scalar values must not erase richer JSON values.
        usage_payload.update(
            {
                key: value
                for key, value in scalar_usage.items()
                if value is not None or key == "request_count"
            }
        )
        return ProcessingRecord(
            id=row["id"],
            fingerprint=FileFingerprint(
                source_path=row["source_path"],
                size_bytes=row["size_bytes"],
                mtime_ns=row["mtime_ns"],
                content_hash=row["content_hash"] or None,
            ),
            source_filename=row["source_filename"],
            event_id=row["event_id"],
            recording_time=_from_iso(row["recording_time"]),
            status=row["status"],
            provider=row["provider"],
            model=row["model"],
            prompt_version=row["prompt_version"],
            schema_version=row["schema_version"],
            event_json_path=row["event_json_path"],
            usage=APIUsage.model_validate(usage_payload),
            error_type=row["error_type"],
            error_message=row["error_message"],
            attempt_count=row["attempt_count"],
            created_at=_from_iso(row["created_at"]),
            updated_at=_from_iso(row["updated_at"]),
            processed_at=_from_iso(row["processed_at"]),
        )

    def _fetch_key(self, key: CacheKey) -> ProcessingRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM processing_records
            WHERE source_path = ? AND size_bytes = ? AND mtime_ns = ?
              AND content_hash = ? AND provider = ? AND model = ?
              AND prompt_version = ? AND schema_version = ?
            """,
            self._key_parameters(key),
        ).fetchone()
        return None if row is None else self._row_to_record(row)

    def _event_file_exists(self, event_json_path: str | None) -> bool:
        if not event_json_path:
            return False
        path = Path(event_json_path)
        if not path.is_absolute() and self.output_root is not None:
            path = self.output_root / path
        return path.is_file()

    def get_cached_record(
        self,
        key: CacheKey,
        *,
        require_event_file: bool = True,
    ) -> ProcessingRecord | None:
        """Return a completed exact-key record, optionally checking its artifact."""

        with self._lock:
            record = self._fetch_key(key)
            if record is None or record.status != ProcessingStatus.COMPLETED.value:
                return None
            if require_event_file and not self._event_file_exists(record.event_json_path):
                return None
            return record

    def should_process(
        self,
        key: CacheKey,
        *,
        force: bool = False,
        require_event_file: bool = True,
    ) -> bool:
        if force:
            return True
        return self.get_cached_record(
            key, require_event_file=require_event_file
        ) is None

    def claim_processing(
        self,
        key: CacheKey,
        *,
        event_id: str,
        force: bool = False,
        require_event_file: bool = True,
    ) -> ProcessingClaim:
        """Atomically reuse a valid result or mark this key as processing."""

        now = _to_iso(_utc_now())
        parameters = self._key_parameters(key)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._fetch_key(key)
                valid_cache = (
                    existing is not None
                    and existing.status == ProcessingStatus.COMPLETED.value
                    and (
                        not require_event_file
                        or self._event_file_exists(existing.event_json_path)
                    )
                )
                if valid_cache and not force:
                    self._connection.execute("COMMIT")
                    return ProcessingClaim(existing, should_process=False)

                if existing is None:
                    fingerprint = key.fingerprint
                    cursor = self._connection.execute(
                        """
                        INSERT INTO processing_records (
                            source_path, source_filename, size_bytes, mtime_ns,
                            content_hash, event_id, status, provider, model,
                            prompt_version, schema_version, usage_json,
                            attempt_count, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?, ?, ?, '{}', 1, ?, ?)
                        """,
                        (
                            fingerprint.source_path,
                            PurePosixPath(fingerprint.source_path).name,
                            fingerprint.size_bytes,
                            fingerprint.mtime_ns,
                            fingerprint.content_hash or "",
                            event_id,
                            key.provider,
                            key.model,
                            key.prompt_version,
                            key.schema_version,
                            now,
                            now,
                        ),
                    )
                    record_id = int(cursor.lastrowid)
                else:
                    record_id = existing.id
                    self._connection.execute(
                        """
                        UPDATE processing_records
                        SET event_id = ?, status = 'processing', event_json_path = NULL,
                            recording_time = NULL, input_tokens = NULL,
                            output_tokens = NULL, total_tokens = NULL,
                            request_count = 0, api_processing_sec = NULL,
                            estimated_cost = NULL, cost_currency = NULL,
                            usage_json = '{}', error_type = NULL,
                            error_message = NULL, processed_at = NULL,
                            attempt_count = attempt_count + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (event_id, now, record_id),
                    )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            record = self.get_record(record_id)
            return ProcessingClaim(record, should_process=True)

    @staticmethod
    def _coerce_usage(usage: APIUsage | dict[str, Any] | None) -> APIUsage:
        if usage is None:
            return APIUsage()
        if isinstance(usage, APIUsage):
            return usage
        return APIUsage.model_validate(usage)

    def mark_completed(
        self,
        record_id: int,
        *,
        event_json_path: str | Path,
        usage: APIUsage | dict[str, Any] | None = None,
        recording_time: datetime | None = None,
        event_id: str | None = None,
        processed_at: datetime | None = None,
    ) -> ProcessingRecord:
        artifact_path = str(event_json_path).strip()
        if not artifact_path:
            raise ValueError("event_json_path must not be empty")
        usage_model = self._coerce_usage(usage)
        finished = processed_at or _utc_now()
        usage_json = json.dumps(
            usage_model.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE processing_records
                SET status = 'completed',
                    event_id = COALESCE(?, event_id),
                    recording_time = ?, event_json_path = ?,
                    input_tokens = ?, output_tokens = ?, total_tokens = ?,
                    request_count = ?, api_processing_sec = ?,
                    estimated_cost = ?, cost_currency = ?, usage_json = ?,
                    error_type = NULL, error_message = NULL,
                    processed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    event_id,
                    _to_iso(recording_time),
                    artifact_path,
                    usage_model.input_tokens,
                    usage_model.output_tokens,
                    usage_model.total_tokens,
                    usage_model.request_count,
                    usage_model.api_processing_sec,
                    usage_model.estimated_cost,
                    usage_model.cost_currency,
                    usage_json,
                    _to_iso(finished),
                    _to_iso(finished),
                    record_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown processing record id {record_id}")
            return self.get_record(record_id)

    def mark_failed(
        self,
        record_id: int,
        *,
        error: str | BaseException,
        error_type: str | None = None,
        usage: APIUsage | dict[str, Any] | None = None,
        recording_time: datetime | None = None,
        processed_at: datetime | None = None,
    ) -> ProcessingRecord:
        usage_model = self._coerce_usage(usage)
        finished = processed_at or _utc_now()
        if isinstance(error, BaseException):
            error_message = str(error) or error.__class__.__name__
            resolved_type = error_type or error.__class__.__name__
        else:
            error_message = str(error)
            resolved_type = error_type or "ProcessingError"
        error_message = error_message.strip() or "unspecified processing failure"
        # Avoid unbounded database/log growth from provider response bodies.
        error_message = error_message[:8000]
        usage_json = json.dumps(
            usage_model.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE processing_records
                SET status = 'failed', recording_time = ?,
                    input_tokens = ?, output_tokens = ?, total_tokens = ?,
                    request_count = ?, api_processing_sec = ?,
                    estimated_cost = ?, cost_currency = ?, usage_json = ?,
                    event_json_path = NULL, error_type = ?, error_message = ?,
                    processed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _to_iso(recording_time),
                    usage_model.input_tokens,
                    usage_model.output_tokens,
                    usage_model.total_tokens,
                    usage_model.request_count,
                    usage_model.api_processing_sec,
                    usage_model.estimated_cost,
                    usage_model.cost_currency,
                    usage_json,
                    resolved_type,
                    error_message,
                    _to_iso(finished),
                    _to_iso(finished),
                    record_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown processing record id {record_id}")
            return self.get_record(record_id)

    def get_record(self, record_id: int) -> ProcessingRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM processing_records WHERE id = ?", (record_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown processing record id {record_id}")
            return self._row_to_record(row)

    def list_records(
        self,
        *,
        source_prefix: str | None = None,
        status: ProcessingStatus | str | None = None,
    ) -> list[ProcessingRecord]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if source_prefix is not None:
            normalized = source_prefix.replace("\\", "/").strip("/")
            clauses.append("source_path LIKE ? ESCAPE '\\'")
            escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"{escaped}%")
        if status is not None:
            status_value = status.value if isinstance(status, ProcessingStatus) else status
            status_value = ProcessingStatus(status_value).value
            clauses.append("status = ?")
            parameters.append(status_value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM processing_records{where} ORDER BY source_path, id",
                parameters,
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def status_counts(self, *, source_prefix: str | None = None) -> dict[str, int]:
        counts = {status.value: 0 for status in ProcessingStatus}
        for record in self.list_records(source_prefix=source_prefix):
            counts[str(record.status)] += 1
        return counts


__all__ = [
    "CacheKey",
    "FileFingerprint",
    "ProcessingClaim",
    "ProcessingRecord",
    "ProcessingStatus",
    "StateStore",
]
