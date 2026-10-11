"""JEV (TypeSafe System One): fast, typed decisions with calibrated confidence.

Every cheap judgement in Linus goes through here, as one request per decision
with all its questions fanned out against the same state. Following the
jev-1.13 guidance, arithmetic, counting and dates stay in code, and nothing
JEV says can move money on its own: it routes and flags, tests decide.
"""

import json
from collections.abc import Mapping
from typing import Any

from neatlogs import trace
from typesafe_sdk import Question, SystemOneResponse, TypeSafeClient

from linus.config import Settings

# jev-1.13 pricing: $0.042 per million input tokens; output tokens are free.
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000


def _dump(value: Any, limit: int = 20_000) -> str:
    return json.dumps(value, default=str)[:limit]


class Jev:
    def __init__(self, settings: Settings) -> None:
        kwargs: dict[str, Any] = {
            "api_key": settings.jev_api_key,
            "model": settings.jev_model,
            "timeout": 30.0,
        }
        if settings.jev_base_url:
            kwargs["base_url"] = settings.jev_base_url
        self._client = TypeSafeClient(**kwargs)
        self.calls = 0
        self.input_tokens = 0

    @property
    def cost_usd(self) -> float:
        return self.input_tokens * USD_PER_INPUT_TOKEN

    def ask(self, step: str, state: Any, questions: Mapping[str, Question]) -> SystemOneResponse:
        """One System One request, traced in neatlogs as an LLM span."""
        with trace(name=f"jev.{step}", kind="LLM") as span:
            span.set_attribute("neatlogs.internal", False)
            span.set_attribute("neatlogs.llm.provider", "typesafe")
            span.set_attribute("neatlogs.llm.system", "jev")
            span.set_attribute(
                "input.value",
                _dump({"state": state, "questions": {k: q.model_dump() for k, q in questions.items()}}),
            )
            response = self._client.system_one(state=state, questions=questions)
            tokens = response.usage.input_tokens
            self.calls += 1
            self.input_tokens += tokens
            span.set_attribute("neatlogs.llm.model_name", response.model)
            span.set_attribute("neatlogs.llm.token_count.prompt", tokens)
            span.set_attribute("neatlogs.llm.token_count.completion", response.usage.output_tokens)
            span.set_attribute("neatlogs.llm.token_count.total", tokens + response.usage.output_tokens)
            span.set_attribute("linus.cost_usd", tokens * USD_PER_INPUT_TOKEN)
            span.set_attribute("output.value", _dump(response.model_dump()["answers"]))
        return response
