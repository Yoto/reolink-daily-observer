"""Batch submission: request bodies, result parsing, and failure accounting."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.genai.base import BatchRequest, FrameInput, GenAIResponseError
from app.genai.openai import BatchOptions, OpenAIProvider, write_shards


class Result(BaseModel):
    label: str


def _provider(tmp_path: Path, **batch: Any) -> OpenAIProvider:
    options = {
        "enabled": True,
        "poll_interval_sec": 0.0,
        "max_wait_sec": 30.0,
        "discount_ratio": 0.5,
    }
    options.update(batch)
    provider = OpenAIProvider(
        api_key="not-a-real-api-key",
        model="gpt-5.6-luna",
        max_attempts=1,
        max_output_tokens=4096,
        input_cost_per_million=0.20,
        output_cost_per_million=1.20,
        batch=BatchOptions(**options),
        temp_dir=tmp_path / "temp",
    )
    return provider


def _request(custom_id: str, frames: tuple[FrameInput, ...] = ()) -> BatchRequest[Result]:
    return BatchRequest(
        custom_id=custom_id, prompt="observe", response_model=Result, frames=frames
    )


def _output_line(custom_id: str, *, label: str = "ok") -> str:
    return json.dumps(
        {
            "custom_id": custom_id,
            "response": {
                "status_code": 200,
                "body": {
                    "id": f"resp_{custom_id}",
                    "model": "gpt-5.6-luna",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps({"label": label}),
                                }
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 1000,
                        "output_tokens": 100,
                        "total_tokens": 1100,
                        "input_tokens_details": {"cached_tokens": 50},
                    },
                },
            },
            "error": None,
        }
    )


class FakeFiles:
    def __init__(self, contents: dict[str, str]) -> None:
        self.contents = contents
        self.uploaded: list[bytes] = []
        self.deleted: list[str] = []

    def create(self, *, file: Any, purpose: str) -> SimpleNamespace:
        assert purpose == "batch"
        self.uploaded.append(file.read())
        return SimpleNamespace(id=f"file_in_{len(self.uploaded)}")

    def content(self, file_id: str) -> SimpleNamespace:
        return SimpleNamespace(text=self.contents[file_id])

    def delete(self, file_id: str) -> None:
        self.deleted.append(file_id)


class FakeBatches:
    def __init__(self, statuses: list[str], *, output_file_id: str | None) -> None:
        self.statuses = statuses
        self.output_file_id = output_file_id
        self.created: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.polls = 0

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.created.append(kwargs)
        return SimpleNamespace(id=f"batch_{len(self.created)}", status="validating")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        status = self.statuses[min(self.polls, len(self.statuses) - 1)]
        self.polls += 1
        return SimpleNamespace(
            id=batch_id,
            status=status,
            output_file_id=self.output_file_id if status == "completed" else None,
            error_file_id=None,
            errors=None,
            request_counts=SimpleNamespace(total=1, completed=1, failed=0),
        )

    def cancel(self, batch_id: str) -> None:
        self.cancelled.append(batch_id)


def _client(lines: list[str], statuses: list[str]) -> SimpleNamespace:
    files = FakeFiles({"file_out": "\n".join(lines) + "\n"})
    batches = FakeBatches(statuses, output_file_id="file_out")
    return SimpleNamespace(files=files, batches=batches)


def test_batch_body_carries_the_same_content_as_a_synchronous_request(
    tmp_path: Path,
) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")
    workspace = tmp_path / "shards"
    workspace.mkdir()

    shards = write_shards(
        [_request("event-c001", (FrameInput(path=frame, timestamp_sec=1.25),))],
        workspace,
        model="gpt-5.6-luna",
        max_output_tokens=4096,
        max_requests=50,
        max_bytes=10_000_000,
    )

    line = json.loads(shards[0].path.read_text(encoding="utf-8").strip())
    assert line["custom_id"] == "event-c001"
    assert line["url"] == "/v1/responses"
    body = line["body"]
    assert body["model"] == "gpt-5.6-luna"
    assert body["max_output_tokens"] == 4096
    assert body["store"] is False
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["name"] == "Result"
    content = body["input"][0]["content"]
    assert content[0]["text"] == "observe"
    assert content[1]["text"] == "動画開始から 1.250 秒"
    assert content[2]["image_url"].startswith("data:image/jpeg;base64,")


def test_shards_split_on_request_count_and_byte_budget(tmp_path: Path) -> None:
    workspace = tmp_path / "shards"
    workspace.mkdir()
    requests = [_request(f"event-c{index:03d}") for index in range(5)]

    by_count = write_shards(
        requests,
        workspace,
        model="m",
        max_output_tokens=16,
        max_requests=2,
        max_bytes=10_000_000,
    )
    assert [len(shard.custom_ids) for shard in by_count] == [2, 2, 1]

    workspace = tmp_path / "shards_bytes"
    workspace.mkdir()
    one_line = by_count[0].size_bytes // 2
    by_bytes = write_shards(
        requests,
        workspace,
        model="m",
        max_output_tokens=16,
        max_requests=50,
        max_bytes=one_line,
    )
    # Each line already fills the budget, so no shard may hold two of them.
    assert [len(shard.custom_ids) for shard in by_bytes] == [1, 1, 1, 1, 1]
    assert all(shard.path.is_file() for shard in by_bytes)


def test_batch_run_uploads_polls_and_halves_the_cost_estimate(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    client = _client([_output_line("a")], ["in_progress", "completed"])
    provider._client = client

    outcomes = provider.generate_structured_batch([_request("a")])

    assert client.batches.created[0]["endpoint"] == "/v1/responses"
    assert client.batches.created[0]["completion_window"] == "24h"
    assert outcomes["a"].value == Result(label="ok")
    usage = outcomes["a"].usage
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 100
    # Half of the 0.00032 a synchronous request would have cost.
    assert usage.estimated_cost == 0.00016
    assert usage.details["batch"] is True
    assert usage.details["batch_id"] == "batch_1"
    assert usage.details["cached_input_tokens"] == 50
    # A batch reports no per-request timing, and inventing one would be worse.
    assert usage.api_processing_sec is None
    # The uploaded payload is scratch; the result file is left in place.
    assert client.files.deleted == ["file_in_1"]
    assert not list((tmp_path / "temp").glob("genai-batch-*"))


def test_failed_requests_are_reported_per_request_not_raised(tmp_path: Path) -> None:
    lines = [
        _output_line("ok-one"),
        json.dumps(
            {
                "custom_id": "http-error",
                "response": {
                    "status_code": 429,
                    "body": {"error": {"message": "rate limited"}},
                },
                "error": None,
            }
        ),
        json.dumps(
            {
                "custom_id": "truncated",
                "response": {
                    "status_code": 200,
                    "body": {
                        "id": "resp_truncated",
                        "output": [],
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "usage": {"input_tokens": 10, "output_tokens": 4096},
                    },
                },
                "error": None,
            }
        ),
    ]
    provider = _provider(tmp_path)
    provider._client = _client(lines, ["completed"])

    outcomes = provider.generate_structured_batch(
        [_request("ok-one"), _request("http-error"), _request("truncated"), _request("missing")]
    )

    assert outcomes["ok-one"].succeeded
    assert not outcomes["http-error"].succeeded
    assert "rate limited" in outcomes["http-error"].error
    assert "max_output_tokens" in outcomes["truncated"].error
    # A request the provider never answered is still accounted for.
    assert "no result" in outcomes["missing"].error
    # The truncated request was billed even though it produced nothing usable.
    assert outcomes["truncated"].usage.input_tokens == 10


def test_a_batch_that_ends_unfinished_fails_only_its_own_requests(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path)
    provider._client = _client([], ["expired"])

    outcomes = provider.generate_structured_batch([_request("a"), _request("b")])

    assert set(outcomes) == {"a", "b"}
    assert all("expired" in outcome.error for outcome in outcomes.values())


def test_waiting_past_the_ceiling_cancels_the_batch(tmp_path: Path) -> None:
    provider = _provider(tmp_path, max_wait_sec=0.0)
    client = _client([], ["in_progress"])
    provider._client = client

    with pytest.raises(GenAIResponseError, match="did not finish"):
        provider.generate_structured_batch([_request("a")])

    assert client.batches.cancelled == ["batch_1"]


def test_disabled_batch_falls_back_to_immediate_requests(tmp_path: Path) -> None:
    provider = _provider(tmp_path, enabled=False)
    calls: list[str] = []

    class Responses:
        def parse(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs["input"][0]["content"][0]["text"])
            return SimpleNamespace(
                id="resp", model="m", output_parsed=Result(label="ok"), usage=None
            )

    provider._client = SimpleNamespace(responses=Responses())

    outcomes = provider.generate_structured_batch([_request("a"), _request("b")])

    assert calls == ["observe", "observe"]
    assert outcomes["a"].value == Result(label="ok")
    assert outcomes["b"].value == Result(label="ok")


def test_duplicate_custom_ids_are_rejected_before_anything_is_billed(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path)
    client = _client([], ["completed"])
    provider._client = client

    with pytest.raises(ValueError, match="duplicate batch custom_id"):
        provider.generate_structured_batch([_request("a"), _request("a")])

    assert client.batches.created == []
