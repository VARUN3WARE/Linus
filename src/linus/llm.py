"""OpenAI, used only where text has to be generated: reproduction tests.

Everything that is a decision goes to JEV (see jev.py). The client is wrapped
with neatlogs so every generation shows up in the trace with its tokens.
"""

from dataclasses import dataclass

import neatlogs
from openai import OpenAI

from linus.config import Settings


@dataclass
class Generation:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int


class Generator:
    def __init__(self, settings: Settings) -> None:
        self._client = neatlogs.wrap(OpenAI(api_key=settings.openai_api_key, max_retries=3, timeout=120))
        self.model = settings.openai_model
        self.reasoning = settings.openai_reasoning
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def complete(self, system: str, user: str) -> Generation:
        kwargs = {}
        if self.reasoning and self.model.startswith(("gpt-5", "gpt-6", "o")):
            kwargs["reasoning_effort"] = self.reasoning
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **kwargs,
        )
        usage = response.usage
        self.calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        return Generation(
            text=response.choices[0].message.content or "",
            model=response.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )
