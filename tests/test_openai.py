from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel

from app.genai.base import FrameInput
from app.genai.openai import OpenAIProvider


def test_usage_maps_responses_api_metadata_without_a_live_request() -> None:
    provider = object.__new__(OpenAIProvider)
    provider._input_cost = 0.20
    provider._output_cost = 1.20
    response = SimpleNamespace(
        id="resp_test",
        model="gpt-5.6-luna",
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=100,
            total_tokens=1100,
            input_tokens_details=SimpleNamespace(cached_tokens=50),
            output_tokens_details=SimpleNamespace(reasoning_tokens=10),
        ),
    )

    usage = provider._usage(response, 1.5)

    assert usage.input_tokens == 1000
    assert usage.output_tokens == 100
    assert usage.total_tokens == 1100
    assert usage.estimated_cost == 0.00032
    assert usage.details["response_id"] == "resp_test"


def test_generate_structured_sends_timestamped_data_url(tmp_path) -> None:
    class Result(BaseModel):
        label: str

    captured = {}

    class Responses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                id="resp_test",
                model="gpt-5.6-luna",
                output_parsed=Result(label="ok"),
                usage=None,
            )

    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")
    provider = OpenAIProvider(
        api_key="not-a-real-api-key",
        model="gpt-5.6-luna",
        max_attempts=1,
    )
    provider._client = SimpleNamespace(responses=Responses())

    result = provider.generate_structured(
        prompt="observe",
        response_model=Result,
        frames=[FrameInput(path=frame, timestamp_sec=1.25)],
    )

    assert result.value == Result(label="ok")
    assert captured["text_format"] is Result
    assert captured["store"] is False
    content = captured["input"][0]["content"]
    assert content[1]["text"] == "動画開始から 1.250 秒"
    assert content[2]["image_url"].startswith("data:image/jpeg;base64,")
