"""Provider construction kept separate from orchestration."""

from __future__ import annotations

import os
from typing import Any

from app.genai.base import GenAIConfigurationError, GenAIProvider
from app.genai.mock import MockProvider
from app.genai.openai import BatchOptions, OpenAIProvider


def _value(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def _secret(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    return getter() if callable(getter) else str(value or "")


def create_provider(settings: Any) -> GenAIProvider:
    genai = _value(settings, "genai", settings)
    provider_name = os.getenv(
        "GENAI_PROVIDER", str(_value(genai, "provider", "openai"))
    ).strip().lower()
    model = os.getenv("GENAI_MODEL", str(_value(genai, "model", ""))).strip()

    if provider_name == "mock":
        return MockProvider(model=model or "mock-observer-v1")
    if provider_name != "openai":
        raise GenAIConfigurationError(
            f"unsupported GENAI_PROVIDER={provider_name!r}; supported: openai, mock"
        )

    retry = _value(genai, "retry", None)
    pricing = _value(genai, "pricing", None)
    paths = _value(settings, "paths", None)
    return OpenAIProvider(
        api_key=os.getenv("OPENAI_API_KEY", _secret(_value(genai, "api_key", ""))),
        model=model,
        timeout_sec=float(_value(genai, "request_timeout_sec", 180)),
        max_attempts=int(_value(retry, "max_attempts", 4)),
        max_output_tokens=int(_value(genai, "max_output_tokens", 8192)),
        input_cost_per_million=_value(pricing, "input_per_million_tokens", 0.20),
        output_cost_per_million=_value(pricing, "output_per_million_tokens", 1.20),
        batch=_batch_options(_value(genai, "batch", None)),
        temp_dir=_value(paths, "temp", None),
    )


def _batch_options(batch: Any) -> BatchOptions:
    # A settings object without a batch section predates this feature, so it
    # keeps the synchronous behaviour it was written against.
    if batch is None:
        return BatchOptions()
    defaults = BatchOptions()
    return BatchOptions(
        enabled=bool(_value(batch, "enabled", defaults.enabled)),
        completion_window=str(
            _value(batch, "completion_window", defaults.completion_window)
        ),
        poll_interval_sec=float(
            _value(batch, "poll_interval_sec", defaults.poll_interval_sec)
        ),
        max_wait_sec=float(_value(batch, "max_wait_sec", defaults.max_wait_sec)),
        max_requests_per_batch=int(
            _value(batch, "max_requests_per_batch", defaults.max_requests_per_batch)
        ),
        max_input_bytes=int(_value(batch, "max_input_bytes", defaults.max_input_bytes)),
        discount_ratio=float(_value(batch, "discount_ratio", defaults.discount_ratio)),
        delete_input_file=bool(
            _value(batch, "delete_input_file", defaults.delete_input_file)
        ),
    )
