"""Small provider-neutral interface for structured multimodal generation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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


class GenAIProvider(ABC):
    """Generate one validated object, optionally from timestamped JPEGs."""

    name: str
    model: str

    @abstractmethod
    def generate_structured(
        self,
        *,
        prompt: str,
        response_model: type[SchemaT],
        frames: Sequence[FrameInput] = (),
    ) -> StructuredResult[SchemaT]:
        raise NotImplementedError

    def close(self) -> None:
        """Release provider resources, if any."""


def aggregate_usage(results: Sequence[StructuredResult[BaseModel]]) -> APIUsage:
    usage = APIUsage()
    for result in results:
        usage = usage.plus(result.usage)
    return usage
