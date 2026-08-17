"""OpenAI Responses API adapter for timestamped JPEGs and Pydantic output."""

from __future__ import annotations

import base64
import time
from typing import Any, Sequence

from app.genai.base import (
    FrameInput,
    GenAIConfigurationError,
    GenAIProvider,
    GenAIResponseError,
    SchemaT,
    StructuredResult,
)
from app.models import APIUsage


class OpenAIProvider(GenAIProvider):
    name = "openai"

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

    def generate_structured(
        self,
        *,
        prompt: str,
        response_model: type[SchemaT],
        frames: Sequence[FrameInput] = (),
    ) -> StructuredResult[SchemaT]:
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

    def _usage(self, response: Any, elapsed: float) -> APIUsage:
        metadata = getattr(response, "usage", None)
        if metadata is None:
            return APIUsage(request_count=1, api_processing_sec=elapsed)

        input_tokens = getattr(metadata, "input_tokens", None)
        output_tokens = getattr(metadata, "output_tokens", None)
        total_tokens = getattr(metadata, "total_tokens", None)
        estimated_cost: float | None = None
        if (
            input_tokens is not None
            and output_tokens is not None
            and self._input_cost is not None
            and self._output_cost is not None
        ):
            estimated_cost = (
                input_tokens * self._input_cost + output_tokens * self._output_cost
            ) / 1_000_000

        details: dict[str, int | str] = {}
        input_details = getattr(metadata, "input_tokens_details", None)
        cached_tokens = getattr(input_details, "cached_tokens", None)
        if cached_tokens is not None:
            details["cached_input_tokens"] = cached_tokens
        output_details = getattr(metadata, "output_tokens_details", None)
        reasoning_tokens = getattr(output_details, "reasoning_tokens", None)
        if reasoning_tokens is not None:
            details["reasoning_tokens"] = reasoning_tokens
        if getattr(response, "id", None):
            details["response_id"] = str(response.id)
        if getattr(response, "model", None):
            details["model_version"] = str(response.model)

        return APIUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            request_count=1,
            api_processing_sec=elapsed,
            estimated_cost=estimated_cost,
            cost_currency="USD" if estimated_cost is not None else None,
            details=details,
        )

    def close(self) -> None:
        self._client.close()
