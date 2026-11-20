"""DecisionProvider backed by an LLM client (any provider behind the `LLMClient` port)."""

import json
from collections.abc import Mapping
from typing import Any

from experience_engine.capabilities.templates import render
from experience_engine.domain.benchmark import ModelSettings
from experience_engine.domain.capability import OutType
from experience_engine.ports.decisions import DecisionError, DecisionResult
from experience_engine.ports.llm import ChatMessage, LLMClient, LLMError, LLMRequest


class LlmDecisionProvider:
    def __init__(self, llm: LLMClient, model: ModelSettings):
        self._llm = llm
        self._model = model

    def decide(
        self,
        *,
        template_id: str,
        objective: str,
        inputs: Mapping[str, str],
        output_schema: Mapping[str, OutType],
        max_tokens: int,
    ) -> DecisionResult:
        try:
            prompt = render(template_id, objective, inputs, output_schema)
        except KeyError as exc:
            raise DecisionError(f"unknown template {template_id!r}") from exc
        request = LLMRequest(
            messages=[ChatMessage(role="user", content=prompt)],
            model=self._model.model_copy(update={"max_output_tokens": max_tokens}),
        )
        try:
            response = self._llm.complete(request)
        except LLMError as exc:
            raise DecisionError(f"model call failed: {str(exc)[:150]}") from exc
        tokens = response.prompt_tokens + response.completion_tokens
        start = response.text.find("{")
        if start < 0:
            raise DecisionError("no JSON object in the model reply")
        try:
            obj: Any
            obj, _ = json.JSONDecoder().raw_decode(response.text[start:])
        except json.JSONDecodeError as exc:
            raise DecisionError("model reply is not valid JSON") from exc
        if not isinstance(obj, dict) or set(obj) != set(output_schema):
            raise DecisionError("model reply keys do not match the output schema")
        return DecisionResult(values=dict(obj), tokens=tokens)
