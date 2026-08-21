"""Small provider-neutral interface for structured multimodal generation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, Sequence, TypeVar

from pydantic import BaseModel

from app.models import APIUsage


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class GenAIError(RuntimeError):
    """Base error for provider and response failures."""


class GenAIConfigurationError(GenAIError):
    """The selected provider cannot run with the supplied configuration."""


class GenAIResponseError(GenAIError):
    """The provider failed after making one or more API requests.

    ``usage`` preserves any metadata that was available before the terminal
    failure.  HTTP failures often have no token metadata, but their request
    count and elapsed time can still be recorded by the caller.
    """

    def __init__(self, message: str, *, usage: APIUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage or APIUsage()


@dataclass(frozen=True, slots=True)
class FrameInput:
    path: Path
    timestamp_sec: float


@dataclass(frozen=True, slots=True)
class StructuredResult(Generic[SchemaT]):
    value: SchemaT
    usage: APIUsage
    elapsed_sec: float


@dataclass(frozen=True, slots=True)
class BatchRequest(Generic[SchemaT]):
    """One deferred generation, identified by a caller-owned ``custom_id``.

    The id travels to the provider and back, so the caller can reassemble
    results without relying on ordering.  Providers may reorder or retry.
    """

    custom_id: str
    prompt: str
    response_model: type[SchemaT]
    frames: tuple[FrameInput, ...] = ()


@dataclass(frozen=True, slots=True)
class BatchOutcome(Generic[SchemaT]):
    """One deferred result: either a validated value or a per-request error.

    A batch completes as a whole even when individual requests fail, so a
    failure is data here rather than an exception.  ``usage`` is still reported
    for a failed request when the provider charged for it.
    """

    custom_id: str
    usage: APIUsage = field(default_factory=APIUsage)
    value: SchemaT | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.value is not None


class GenAIProvider(ABC):
    """Generate one validated object, optionally from timestamped JPEGs."""

    name: str
    model: str
    # True only when the adapter has a genuinely deferred, cheaper bulk path.
    # The base implementation below is a correctness fallback, not a discount,
    # so orchestration can use it to decide whether batching is worth the wait.
    supports_batch: bool = False

    @abstractmethod
    def generate_structured(
        self,
        *,
        prompt: str,
        response_model: type[SchemaT],
        frames: Sequence[FrameInput] = (),
    ) -> StructuredResult[SchemaT]:
        raise NotImplementedError

    def generate_structured_batch(
        self, requests: Sequence[BatchRequest[SchemaT]]
    ) -> dict[str, BatchOutcome[SchemaT]]:
        """Resolve many requests at once, keyed by ``custom_id``.

        The default runs them one at a time through the synchronous path so a
        provider without a batch endpoint stays usable.  Adapters that have one
        override this and set ``supports_batch``.
        """

        outcomes: dict[str, BatchOutcome[SchemaT]] = {}
        for request in requests:
            if request.custom_id in outcomes:
                raise ValueError(f"duplicate batch custom_id {request.custom_id!r}")
            try:
                result = self.generate_structured(
                    prompt=request.prompt,
                    response_model=request.response_model,
                    frames=request.frames,
                )
            except Exception as exc:
                outcomes[request.custom_id] = BatchOutcome(
                    custom_id=request.custom_id,
                    usage=failure_usage(exc),
                    error=str(exc) or exc.__class__.__name__,
                )
            else:
                outcomes[request.custom_id] = BatchOutcome(
                    custom_id=request.custom_id,
                    usage=result.usage,
                    value=result.value,
                )
        return outcomes

    def close(self) -> None:
        """Release provider resources, if any."""


def aggregate_usage(results: Sequence[StructuredResult[BaseModel]]) -> APIUsage:
    usage = APIUsage()
    for result in results:
        usage = usage.plus(result.usage)
    return usage


def failure_usage(exc: BaseException) -> APIUsage:
    """Recover whatever usage a failing provider call managed to report."""

    usage = getattr(exc, "usage", None)
    return usage if isinstance(usage, APIUsage) else APIUsage()
