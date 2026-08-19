"""OpenAI adapter: synchronous Responses calls and deferred Batch submission.

Both paths send the same request body. The synchronous one serves the
single-request stages and ``analyze-video``; the batch one serves a whole day
of event observation, where every clip is independent and the run already
targets yesterday, so the bulk queue's latency buys a halved bill for free.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.genai.base import (
    BatchOutcome,
    BatchRequest,
    FrameInput,
    GenAIConfigurationError,
    GenAIProvider,
    GenAIResponseError,
    SchemaT,
    StructuredResult,
)
from app.models import APIUsage


LOGGER = logging.getLogger(__name__)

BATCH_ENDPOINT = "/v1/responses"
# Statuses after which a batch will never produce more output.
TERMINAL_BATCH_STATUSES = frozenset({"completed", "failed", "expired", "cancelled"})


@dataclass(frozen=True, slots=True)
class BatchOptions:
    """Provider-side knobs for the deferred path; mirrors ``BatchSettings``."""

    enabled: bool = False
    completion_window: str = "24h"
    poll_interval_sec: float = 30.0
    max_wait_sec: float = 90_000.0
    max_requests_per_batch: int = 50_000
    max_input_bytes: int = 180_000_000
    discount_ratio: float = 0.5
    delete_input_file: bool = True


@dataclass(slots=True)
class _Shard:
    """One JSONL file, sized to stay inside the provider's per-batch limits."""

    path: Path
    custom_ids: list[str] = field(default_factory=list)
    size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _Job:
    batch_id: str
    input_file_id: str
    custom_ids: tuple[str, ...]


class OpenAIProvider(GenAIProvider):
    name = "openai"
    supports_batch = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_sec: float = 180,
        max_attempts: int = 4,
        max_output_tokens: int = 8192,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
        batch: BatchOptions | None = None,
        temp_dir: Path | None = None,
    ) -> None:
        if not api_key or api_key.startswith("replace-"):
            raise GenAIConfigurationError("OPENAI_API_KEY is required for OpenAI")
        if not model:
            raise GenAIConfigurationError("GENAI_MODEL is required for OpenAI")

        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - installation failure
            raise GenAIConfigurationError(
                "openai is not installed; install project dependencies"
            ) from exc

        self.model = model
        self._client = OpenAI(
            api_key=api_key,
            timeout=timeout_sec,
            max_retries=max(0, max_attempts - 1),
        )
        self._max_output_tokens = max_output_tokens
        self._input_cost = input_cost_per_million
        self._output_cost = output_cost_per_million
        self._batch = batch or BatchOptions()
        self._temp_dir = temp_dir

    def generate_structured(
        self,
        *,
        prompt: str,
        response_model: type[SchemaT],
        frames: Sequence[FrameInput] = (),
    ) -> StructuredResult[SchemaT]:
        content = build_content(prompt, frames)

        started = time.perf_counter()
        try:
            response = self._client.responses.parse(
                model=self.model,
                input=[{"role": "user", "content": content}],
                text_format=response_model,
                max_output_tokens=self._max_output_tokens,
                store=False,
            )
        except Exception as exc:
            raise GenAIResponseError(f"OpenAI request failed: {exc}") from exc

        elapsed = time.perf_counter() - started
        value = getattr(response, "output_parsed", None)
        if not isinstance(value, response_model):
            raise GenAIResponseError(
                "OpenAI returned no schema-valid structured output",
                usage=self._usage(response, elapsed),
            )
        return StructuredResult(
            value=value,
            usage=self._usage(response, elapsed),
            elapsed_sec=elapsed,
        )

    # -- deferred batch path ------------------------------------------------

    def generate_structured_batch(
        self, requests: Sequence[BatchRequest[SchemaT]]
    ) -> dict[str, BatchOutcome[SchemaT]]:
        """Submit every request as one or more batches and wait for all of them.

        A batch reports per-request failures in its output rather than raising,
        so one unusable clip does not cost the day its other results. Only a
        failure to submit, or a wait that runs past ``max_wait_sec``, raises.
        """

        if not requests:
            return {}
        if not self._batch.enabled:
            return super().generate_structured_batch(requests)

        models: dict[str, type[SchemaT]] = {}
        for request in requests:
            if request.custom_id in models:
                raise ValueError(f"duplicate batch custom_id {request.custom_id!r}")
            models[request.custom_id] = request.response_model

        started = time.perf_counter()
        workspace = Path(
            tempfile.mkdtemp(prefix="genai-batch-", dir=_writable_dir(self._temp_dir))
        )
        jobs: list[_Job] = []
        try:
            try:
                for shard in write_shards(
                    requests,
                    workspace,
                    model=self.model,
                    max_output_tokens=self._max_output_tokens,
                    max_requests=self._batch.max_requests_per_batch,
                    max_bytes=self._batch.max_input_bytes,
                ):
                    jobs.append(self._submit(shard))
            except Exception as exc:
                # Anything already queued is billable work whose result this run
                # can no longer assemble.
                self._cancel(jobs)
                self._forget_inputs(jobs)
                if isinstance(exc, GenAIResponseError):
                    raise
                raise GenAIResponseError(f"batch submission failed: {exc}") from exc
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

        outcomes: dict[str, BatchOutcome[SchemaT]] = {}
        try:
            for job, batch in self._wait(jobs):
                self._collect(job, batch, models, outcomes)
        finally:
            self._forget_inputs(jobs)

        LOGGER.info(
            "batch stage complete batches=%d requests=%d succeeded=%d failed=%d "
            "waited_sec=%.1f",
            len(jobs),
            len(requests),
            sum(1 for outcome in outcomes.values() if outcome.succeeded),
            sum(1 for outcome in outcomes.values() if not outcome.succeeded),
            time.perf_counter() - started,
        )
        return outcomes

    def _submit(self, shard: _Shard) -> _Job:
        with shard.path.open("rb") as stream:
            uploaded = self._client.files.create(file=stream, purpose="batch")
        batch = self._client.batches.create(
            input_file_id=uploaded.id,
            endpoint=BATCH_ENDPOINT,
            completion_window=self._batch.completion_window,
            metadata={"source": "reolink-daily-observer"},
        )
        # The batch id is the only handle on work that is already being billed,
        # so it is logged before anything else can fail.
        LOGGER.info(
            "batch submitted batch_id=%s input_file_id=%s requests=%d input_bytes=%d",
            batch.id,
            uploaded.id,
            len(shard.custom_ids),
            shard.size_bytes,
        )
        return _Job(
            batch_id=batch.id,
            input_file_id=uploaded.id,
            custom_ids=tuple(shard.custom_ids),
        )

    def _wait(self, jobs: Sequence[_Job]) -> list[tuple[_Job, Any]]:
        deadline = time.monotonic() + self._batch.max_wait_sec
        pending = {job.batch_id: job for job in jobs}
        finished: list[tuple[_Job, Any]] = []
        while pending:
            for batch_id, job in list(pending.items()):
                try:
                    batch = self._client.batches.retrieve(batch_id)
                except Exception as exc:
                    raise GenAIResponseError(
                        f"cannot read batch {batch_id}: {exc}"
                    ) from exc
                status = str(getattr(batch, "status", "") or "")
                if status in TERMINAL_BATCH_STATUSES:
                    LOGGER.info(
                        "batch finished batch_id=%s status=%s counts=%s",
                        batch_id,
                        status,
                        _counts(batch),
                    )
                    finished.append((job, batch))
                    del pending[batch_id]
                else:
                    LOGGER.info(
                        "batch pending batch_id=%s status=%s counts=%s",
                        batch_id,
                        status,
                        _counts(batch),
                    )
            if not pending:
                break
            if time.monotonic() >= deadline:
                self._cancel(list(pending.values()))
                raise GenAIResponseError(
                    f"batch did not finish within {self._batch.max_wait_sec:.0f}s; "
                    "cancelled " + ", ".join(sorted(pending))
                )
            time.sleep(self._batch.poll_interval_sec)
        return finished

    def _collect(
        self,
        job: _Job,
        batch: Any,
        models: Mapping[str, type[SchemaT]],
        outcomes: dict[str, BatchOutcome[SchemaT]],
    ) -> None:
        status = str(getattr(batch, "status", "") or "unknown")
        if status != "completed":
            reason = f"batch {job.batch_id} ended with status {status}"
            detail = _batch_error_text(batch)
            if detail:
                reason = f"{reason}: {detail}"
            for custom_id in job.custom_ids:
                outcomes[custom_id] = BatchOutcome(custom_id=custom_id, error=reason)
            return

        for line in self._read_lines(getattr(batch, "output_file_id", None)):
            custom_id = str(line.get("custom_id") or "")
            model = models.get(custom_id)
            if model is None:
                continue
            outcomes[custom_id] = self._outcome(line, model, job.batch_id)
        for line in self._read_lines(getattr(batch, "error_file_id", None)):
            custom_id = str(line.get("custom_id") or "")
            if custom_id in outcomes or custom_id not in models:
                continue
            outcomes[custom_id] = BatchOutcome(
                custom_id=custom_id,
                error=f"batch request rejected: {_error_text(line.get('error'))}",
            )
        for custom_id in job.custom_ids:
            if custom_id not in outcomes:
                outcomes[custom_id] = BatchOutcome(
                    custom_id=custom_id,
                    error=f"batch {job.batch_id} returned no result for this request",
                )

    def _outcome(
        self, line: Mapping[str, Any], model: type[SchemaT], batch_id: str
    ) -> BatchOutcome[SchemaT]:
        custom_id = str(line.get("custom_id") or "")
        if line.get("error"):
            return BatchOutcome(
                custom_id=custom_id, error=_error_text(line.get("error"))
            )
        response = line.get("response")
        if not isinstance(response, Mapping):
            return BatchOutcome(custom_id=custom_id, error="batch line has no response")
        body = response.get("body")
        if not isinstance(body, Mapping):
            return BatchOutcome(custom_id=custom_id, error="batch response has no body")

        usage = self._batch_usage(body, batch_id)
        status_code = response.get("status_code")
        if status_code is not None and int(status_code) != 200:
            return BatchOutcome(
                custom_id=custom_id,
                usage=usage,
                error=f"HTTP {status_code}: {_error_text(body.get('error'))}",
            )
        if body.get("error"):
            return BatchOutcome(
                custom_id=custom_id, usage=usage, error=_error_text(body.get("error"))
            )

        text, refusal = _output_text(body)
        if refusal:
            return BatchOutcome(
                custom_id=custom_id, usage=usage, error=f"model refused: {refusal}"
            )
        if not text:
            incomplete = body.get("incomplete_details")
            reason = ""
            if isinstance(incomplete, Mapping) and incomplete.get("reason"):
                reason = f" ({incomplete['reason']})"
            return BatchOutcome(
                custom_id=custom_id,
                usage=usage,
                error=f"batch response carried no structured output{reason}",
            )
        try:
            value = model.model_validate_json(text)
        except Exception as exc:
            return BatchOutcome(
                custom_id=custom_id,
                usage=usage,
                error=f"batch response failed schema validation: {exc}",
            )
        return BatchOutcome(custom_id=custom_id, usage=usage, value=value)

    def _read_lines(self, file_id: str | None) -> list[dict[str, Any]]:
        if not file_id:
            return []
        try:
            content = self._client.files.content(file_id)
        except Exception as exc:
            raise GenAIResponseError(
                f"cannot download batch file {file_id}: {exc}"
            ) from exc
        raw = getattr(content, "text", None)
        if raw is None:
            raw = bytes(content.read()).decode("utf-8")
        lines: list[dict[str, Any]] = []
        for entry in raw.splitlines():
            entry = entry.strip()
            if not entry:
                continue
            try:
                parsed = json.loads(entry)
            except json.JSONDecodeError:
                LOGGER.warning("skipping unparsable batch line in file %s", file_id)
                continue
            if isinstance(parsed, dict):
                lines.append(parsed)
        return lines

    def _cancel(self, jobs: Iterable[_Job]) -> None:
        for job in jobs:
            try:
                self._client.batches.cancel(job.batch_id)
            except Exception as exc:
                LOGGER.warning("cannot cancel batch %s: %s", job.batch_id, exc)

    def _forget_inputs(self, jobs: Iterable[_Job]) -> None:
        """Drop the uploaded JSONL; it is this run's scratch payload.

        Result and error files are left in place, so a completed batch stays
        auditable in the provider's project after the run.
        """

        if not self._batch.delete_input_file:
            return
        for job in jobs:
            try:
                self._client.files.delete(job.input_file_id)
            except Exception as exc:
                LOGGER.warning(
                    "batch input file %s left in place: %s", job.input_file_id, exc
                )

    # -- usage --------------------------------------------------------------

    def _usage(self, response: Any, elapsed: float) -> APIUsage:
        metadata = getattr(response, "usage", None)
        if metadata is None:
            return APIUsage(request_count=1, api_processing_sec=elapsed)

        return self._build_usage(
            input_tokens=getattr(metadata, "input_tokens", None),
            output_tokens=getattr(metadata, "output_tokens", None),
            total_tokens=getattr(metadata, "total_tokens", None),
            cached_tokens=getattr(
                getattr(metadata, "input_tokens_details", None), "cached_tokens", None
            ),
            reasoning_tokens=getattr(
                getattr(metadata, "output_tokens_details", None),
                "reasoning_tokens",
                None,
            ),
            response_id=getattr(response, "id", None),
            model_version=getattr(response, "model", None),
            api_processing_sec=elapsed,
        )

    def _batch_usage(self, body: Mapping[str, Any], batch_id: str) -> APIUsage:
        details: dict[str, Any] = {"batch": True, "batch_id": batch_id}
        metadata = body.get("usage")
        if not isinstance(metadata, Mapping):
            return APIUsage(request_count=1, details=details)

        input_details = metadata.get("input_tokens_details")
        output_details = metadata.get("output_tokens_details")
        return self._build_usage(
            input_tokens=metadata.get("input_tokens"),
            output_tokens=metadata.get("output_tokens"),
            total_tokens=metadata.get("total_tokens"),
            cached_tokens=(
                input_details.get("cached_tokens")
                if isinstance(input_details, Mapping)
                else None
            ),
            reasoning_tokens=(
                output_details.get("reasoning_tokens")
                if isinstance(output_details, Mapping)
                else None
            ),
            response_id=body.get("id"),
            model_version=body.get("model"),
            # A batch reports no per-request timing, and charging every request
            # with the whole queue's wall clock would be worse than admitting
            # the metric is unavailable.
            api_processing_sec=None,
            cost_multiplier=self._batch.discount_ratio,
            extra_details=details,
        )

    def _build_usage(
        self,
        *,
        input_tokens: Any,
        output_tokens: Any,
        total_tokens: Any,
        cached_tokens: Any,
        reasoning_tokens: Any,
        response_id: Any,
        model_version: Any,
        api_processing_sec: float | None,
        cost_multiplier: float = 1.0,
        extra_details: Mapping[str, Any] | None = None,
    ) -> APIUsage:
        estimated_cost: float | None = None
        if (
            input_tokens is not None
            and output_tokens is not None
            and self._input_cost is not None
            and self._output_cost is not None
        ):
            estimated_cost = (
                (input_tokens * self._input_cost + output_tokens * self._output_cost)
                / 1_000_000
                * cost_multiplier
            )

        details: dict[str, Any] = dict(extra_details or {})
        if cached_tokens is not None:
            details["cached_input_tokens"] = cached_tokens
        if reasoning_tokens is not None:
            details["reasoning_tokens"] = reasoning_tokens
        if response_id:
            details["response_id"] = str(response_id)
        if model_version:
            details["model_version"] = str(model_version)

        return APIUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            request_count=1,
            api_processing_sec=api_processing_sec,
            estimated_cost=estimated_cost,
            cost_currency="USD" if estimated_cost is not None else None,
            details=details,
        )

    def close(self) -> None:
        self._client.close()


def build_content(prompt: str, frames: Sequence[FrameInput]) -> list[dict[str, Any]]:
    """Build the user content shared by the synchronous and batch paths."""

    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    for frame in frames:
        encoded = base64.b64encode(frame.path.read_bytes()).decode("ascii")
        content.extend(
            [
                {
                    "type": "input_text",
                    "text": f"動画開始から {frame.timestamp_sec:.3f} 秒",
                },
                {
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{encoded}",
                    "detail": "auto",
                },
            ]
        )
    return content


def text_format_param(response_model: type[Any]) -> dict[str, Any]:
    """Return the ``text.format`` block ``responses.parse`` would have sent.

    The batch endpoint takes a raw body, so the strict schema has to be built
    here. Reusing the SDK's own converter keeps a batched request identical to
    the synchronous one it replaces.
    """

    try:
        from openai.lib._parsing._responses import type_to_text_format_param

        return dict(type_to_text_format_param(response_model))
    except ImportError:  # pragma: no cover - SDK layout change
        pass
    try:
        from openai.lib._pydantic import to_strict_json_schema
    except ImportError as exc:  # pragma: no cover - SDK layout change
        raise GenAIConfigurationError(
            "the installed openai package exposes no strict-schema converter; "
            "batch mode needs openai>=2,<3"
        ) from exc
    return {
        "type": "json_schema",
        "strict": True,
        "name": response_model.__name__,
        "schema": to_strict_json_schema(response_model),
    }


def build_request_body(
    request: BatchRequest[Any], *, model: str, max_output_tokens: int
) -> dict[str, Any]:
    return {
        "model": model,
        "input": [
            {"role": "user", "content": build_content(request.prompt, request.frames)}
        ],
        "text": {"format": text_format_param(request.response_model)},
        "max_output_tokens": max_output_tokens,
        "store": False,
    }


def write_shards(
    requests: Sequence[BatchRequest[Any]],
    workspace: Path,
    *,
    model: str,
    max_output_tokens: int,
    max_requests: int,
    max_bytes: int,
) -> list[_Shard]:
    """Serialize requests to JSONL, splitting on the provider's batch limits.

    Every line is written as it is built, so only one base64-encoded request is
    held in memory at a time even though a busy day is gigabytes of frames.
    """

    shards: list[_Shard] = []
    current: _Shard | None = None
    handle = None
    try:
        for request in requests:
            line = (
                json.dumps(
                    {
                        "custom_id": request.custom_id,
                        "method": "POST",
                        "url": BATCH_ENDPOINT,
                        "body": build_request_body(
                            request, model=model, max_output_tokens=max_output_tokens
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            if current is not None and (
                len(current.custom_ids) >= max_requests
                or current.size_bytes + len(line) > max_bytes
            ):
                handle.close()  # type: ignore[union-attr]
                handle = None
                current = None
            if current is None:
                current = _Shard(path=workspace / f"batch_{len(shards):03d}.jsonl")
                shards.append(current)
                handle = current.path.open("wb")
                if len(line) > max_bytes:
                    # A single request larger than a whole batch cannot be split
                    # here. It is sent anyway so the provider's rejection lands
                    # on that one clip instead of on the whole day.
                    LOGGER.warning(
                        "batch request %s is %d bytes, above the %d byte batch limit",
                        request.custom_id,
                        len(line),
                        max_bytes,
                    )
            handle.write(line)  # type: ignore[union-attr]
            current.custom_ids.append(request.custom_id)
            current.size_bytes += len(line)
    finally:
        if handle is not None:
            handle.close()
    return shards


def _writable_dir(path: Path | None) -> str | None:
    if path is None:
        return None
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _output_text(body: Mapping[str, Any]) -> tuple[str, str]:
    """Join the assistant text parts, or return the refusal that replaced them."""

    chunks: list[str] = []
    refusals: list[str] = []
    output = body.get("output")
    if not isinstance(output, list):
        return "", ""
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "output_text":
                chunks.append(str(part.get("text") or ""))
            elif part.get("type") == "refusal":
                refusals.append(str(part.get("refusal") or ""))
    return "".join(chunks), " ".join(value for value in refusals if value)


def _error_text(error: Any) -> str:
    if isinstance(error, Mapping):
        message = error.get("message") or error.get("code")
        if message:
            return str(message)
    return str(error) if error else "unknown error"


def _batch_error_text(batch: Any) -> str:
    data = getattr(getattr(batch, "errors", None), "data", None)
    if not data:
        return ""
    messages = [
        str(getattr(item, "message", ""))
        for item in data
        if getattr(item, "message", None)
    ]
    return "; ".join(messages[:3])


def _counts(batch: Any) -> str:
    counts = getattr(batch, "request_counts", None)
    if counts is None:
        return "unknown"
    return (
        f"{getattr(counts, 'completed', '?')}/{getattr(counts, 'total', '?')}"
        f" failed={getattr(counts, 'failed', '?')}"
    )
