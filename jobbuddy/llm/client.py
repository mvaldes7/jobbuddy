"""Model-call protocol and strict JSON parsing helpers. There is no network client here: jobbuddy never
calls an external LLM API. Tests pass a fake implementing `LLMClient`; production passes None."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class TokenBudget:
    limit: int
    used: int = 0

    def estimate(self, messages: list[dict[str, str]], max_output: int) -> int:
        chars = sum(len(m.get("content", "")) for m in messages)
        return chars // 4 + max_output

    def check(self, needed: int) -> None:
        if self.used + needed > self.limit:
            raise BudgetExceeded(f"LLM token budget exhausted ({self.used}/{self.limit})")

    def consume(self, n: int) -> None:
        self.used += n

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


@dataclass
class LLMResponse:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMClient(Protocol):
    budget: TokenBudget

    def chat(self, messages: list[dict[str, str]], *, purpose: str, json_mode: bool = False,
             max_tokens: int | None = None, temperature: float | None = None) -> LLMResponse: ...


def parse_json_response(text: str) -> Any:
    """Parse JSON from a model reply, tolerating ```json fences and leading prose."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", t, re.S)
        if not m:
            raise
        return json.loads(m.group(1))


def chat_json(client: LLMClient, messages: list[dict[str, str]], model_cls: type[T], *, purpose: str,
              retries: int = 1, max_tokens: int | None = None) -> T:
    """Call the LLM and validate the JSON reply with pydantic; one corrective retry on bad output."""
    msgs = list(messages)
    last_err: Exception | None = None
    for _ in range(retries + 1):
        resp = client.chat(msgs, purpose=purpose, json_mode=True, max_tokens=max_tokens)
        try:
            return model_cls.model_validate(parse_json_response(resp.text))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            last_err = exc
            msgs = msgs + [
                {"role": "assistant", "content": resp.text[:2000]},
                {"role": "user", "content": "That was not valid JSON for the required schema. "
                                            f"Error: {str(exc)[:400]}. Reply with ONLY the corrected JSON."},
            ]
    raise ValueError(f"LLM returned invalid JSON for {purpose}: {last_err}")
