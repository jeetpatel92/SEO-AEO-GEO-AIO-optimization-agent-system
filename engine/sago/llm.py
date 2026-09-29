"""Claude calls for every agent role.

Each call sends the house pack (site profile, policy, learned rules; cached for
1h) plus the role prompt (cached) as system, the job's data as the user turn,
and a JSON schema as structured output, so every handoff between agents is
schema valid. Server side refusal fallbacks are on by default. A refusal, a
truncated answer or an unavailable API surfaces as a typed error the Conductor
can route.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

import anthropic


class LLMError(Exception):
    """Non retryable for this item: skip it, keep the run going."""


class LLMRefusal(LLMError):
    pass


class LLMUnavailable(LLMError):
    """Rate limit, outage, credits or auth: stop generating for this run."""


class BudgetExceeded(LLMError):
    pass


@dataclass
class Usage:
    max_input: int
    max_output: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0

    def add(self, u) -> None:
        self.calls += 1
        self.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

    @property
    def effective_input(self) -> int:
        return self.input_tokens + self.cache_write + self.cache_read // 10

    def check(self) -> None:
        if self.effective_input > self.max_input or self.output_tokens > self.max_output:
            raise BudgetExceeded(f"run budget reached: {self.effective_input} input, {self.output_tokens} output tokens")

    def as_dict(self) -> dict:
        return {"calls": self.calls, "input": self.input_tokens, "output": self.output_tokens,
                "cache_read": self.cache_read, "cache_write": self.cache_write}


def obj(properties: dict, required: list[str] | None = None) -> dict:
    """Schema object: every property required, no extras (what structured outputs expect)."""
    return {"type": "object", "properties": properties, "required": required or list(properties),
            "additionalProperties": False}


def arr(items: dict) -> dict:
    return {"type": "array", "items": items}


def enum(*values) -> dict:
    t = "integer" if all(isinstance(v, int) for v in values) else "string"
    return {"type": t, "enum": list(values)}


S = {"type": "string"}
N = {"type": "number"}
I = {"type": "integer"}
B = {"type": "boolean"}


def _check_required(schema: dict, data) -> None:
    if schema.get("type") == "object":
        if not isinstance(data, dict):
            raise LLMError("structured output is not an object")
        for k in schema.get("required", []):
            if k not in data:
                raise LLMError(f"structured output missing '{k}'")


class ClaudeLLM:
    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self.client = client or anthropic.Anthropic(max_retries=3)
        self._fallbacks = bool(cfg.settings.get("use_server_fallbacks", True))
        self.new_usage()

    def new_usage(self) -> Usage:
        b = self.cfg.settings["run_budget"]
        self.usage = Usage(b["max_input_tokens"], b["max_output_tokens"])
        return self.usage

    def structured(self, role: str, payload, schema: dict, max_tokens: int = 16000) -> dict:
        self.usage.check()
        model, effort = self.cfg.model_for(role)
        system = [
            {"type": "text", "text": self.cfg.house_pack(), "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            {"type": "text", "text": self.cfg.prompt(role), "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        ]
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
        kwargs = dict(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        resp = self._call(role, kwargs)
        self.usage.add(resp.usage)
        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise LLMRefusal(f"{role}: declined (category {cat})")
        if resp.stop_reason == "max_tokens":
            raise LLMError(f"{role}: answer cut off at max_tokens")
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
        if not text:
            raise LLMError(f"{role}: no text block in response")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"{role}: invalid JSON: {e}") from e
        _check_required(schema, data)
        return data

    def _call(self, role: str, kwargs: dict):
        try:
            if self._fallbacks:
                return self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                        fallbacks="default", **kwargs)
            return self.client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            if "credit balance" in msg:
                raise LLMUnavailable("Anthropic credit balance is too low: top up at console.anthropic.com") from e
            if self._fallbacks and "fallback" in msg:
                self._fallbacks = False  # the beta is not available to this account: carry on without it
                return self._call(role, kwargs)
            raise LLMError(f"{role}: bad request: {e}") from e
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise LLMUnavailable(f"Anthropic API key rejected: {e}") from e
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as e:
            raise LLMUnavailable(f"{role}: {type(e).__name__} after retries") from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 or e.status_code == 429:
                raise LLMUnavailable(f"{role}: Anthropic API {e.status_code} after retries") from e
            raise LLMError(f"{role}: API error {e.status_code}: {e}") from e


class FakeLLM:
    """For tests and fixture demos: `responder(role, payload) -> dict | Exception`."""

    def __init__(self, responder: Callable[[str, dict], dict], budget: tuple[int, int] = (10**9, 10**9)):
        self.responder = responder
        self.usage = Usage(*budget)
        self.calls: list[tuple[str, dict]] = []

    def new_usage(self) -> Usage:
        self.usage = Usage(self.usage.max_input, self.usage.max_output)
        return self.usage

    def structured(self, role: str, payload, schema: dict, max_tokens: int = 16000) -> dict:
        self.usage.check()
        self.calls.append((role, payload))
        self.usage.calls += 1
        self.usage.input_tokens += 1000
        self.usage.output_tokens += 100
        out = self.responder(role, payload)
        if isinstance(out, Exception):
            raise out
        _check_required(schema, out)
        return out
