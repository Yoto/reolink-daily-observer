"""Deterministic offline provider used only for tests and pipeline smoke runs."""

from __future__ import annotations

import time
from typing import Sequence

from pydantic import BaseModel

from app.genai.base import FrameInput, GenAIProvider, SchemaT, StructuredResult
from app.models import APIUsage, EventAnalysis, Observation, TriageResult


class MockProvider(GenAIProvider):
    """Exercise orchestration without claiming to perform visual inference."""

    name = "mock"

    def __init__(self, model: str = "mock-observer-v1") -> None:
        self.model = model

    def generate_structured(
        self,
        *,
        prompt: str,
        response_model: type[SchemaT],
        frames: Sequence[FrameInput] = (),
    ) -> StructuredResult[SchemaT]:
        started = time.perf_counter()
        if issubclass(response_model, EventAnalysis):
            end = frames[-1].timestamp_sec if frames else 0.0
            payload: BaseModel = EventAnalysis(
                entities=[],
                observations=[
                    Observation(
                        start_sec=0,
                        end_sec=end,
                        description=(
                            "オフライン動作確認モードでフレーム列を受領した。"
                            "画像内容の意味解析は行っていない。"
                        ),
                        certainty="unknown",
                    )
                ],
                interactions=[],
                scene_changes=[],
                summary=(
                    "オフライン動作確認用の出力。実映像の評価にはOpenAI providerを使用する。"
                ),
                uncertainties=["mock providerのため映像内容は観察していない"],
            )
        elif issubclass(response_model, TriageResult):
            # Triage is a judgement stage; a mock must not invent judgements.
            # An empty result makes the report render its "nothing evaluated"
            # path instead of implying every event was checked and cleared.
            payload = TriageResult(
                items=[],
                day_notes=[
                    "オフライン動作確認モードのため、イベントの判定は行っていない。"
                ],
            )
        else:
            fields = response_model.model_fields
            payload_data: dict[str, object] = {}
            if "overview" in fields:
                payload_data["overview"] = (
                    "オフライン動作確認モードでevent JSONを集約した。"
                    "実映像に関する要約は生成していない。"
                )
            if "time_periods" in fields:
                payload_data["time_periods"] = []
            if "recurring_patterns" in fields:
                payload_data["recurring_patterns"] = []
            if "representative_events" in fields:
                payload_data["representative_events"] = []
            payload = response_model.model_validate(payload_data)

        elapsed = time.perf_counter() - started
        return StructuredResult(
            value=payload,  # type: ignore[arg-type]
            usage=APIUsage(request_count=1, api_processing_sec=elapsed),
            elapsed_sec=elapsed,
        )
