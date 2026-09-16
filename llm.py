"""
The single place FintNet calls Claude.

Model: LLM_MODEL, default claude-haiku-4-5 (the project rule: prototypes use
the cheapest capable model). Key: ANTHROPIC_API_KEY.

Guardrails:
  * LLM_MAX_CALLS caps model calls per server instance (default 400), so a
    loop or a bot cannot run up the bill.
  * Every call is a Langfuse generation with token usage, and an HEC event.
  * Errors never escape as SDK exceptions: callers get LLMUnavailable and
    fall back to rules.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

import anthropic

import observability

MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5")

_lock = threading.Lock()
_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0}
_client: anthropic.Anthropic | None = None


class LLMUnavailable(Exception):
    """No key, budget used up, or the API failed. Callers fall back to rules."""


def available() -> bool:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False
    with _lock:
        used = _usage["calls"] + _usage["errors"]
    return used < int(os.getenv("LLM_MAX_CALLS", "400"))


def usage() -> dict:
    with _lock:
        snapshot = dict(_usage)
    # available() takes the lock itself; calling it inside the block above deadlocks.
    return dict(snapshot, model=MODEL, available=available())


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(timeout=30.0, max_retries=2)
    return _client


def create(name: str, *, system: str, messages: list[dict], max_tokens: int,
           tools: list[dict] | None = None, output_config: dict | None = None,
           metadata: dict[str, Any] | None = None, prompt: Any = None) -> anthropic.types.Message:
    """One Messages API call, traced and budgeted."""
    if not available():
        raise LLMUnavailable("model unavailable (no key or call budget used)")
    params: dict[str, Any] = {"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": messages}
    if tools:
        params["tools"] = tools
    if output_config:
        params["output_config"] = output_config
    with observability.generation(name, model=MODEL, system=system, messages=messages,
                                  model_parameters={"max_tokens": max_tokens}, prompt=prompt,
                                  metadata={"prompt_version": getattr(prompt, "version", None)
                                            or observability.version(system), **(metadata or {})}) as gen:
        try:
            response = client().messages.create(**params)
        except anthropic.RateLimitError as exc:
            _count(error=True)
            gen.update(level="ERROR", status_message="rate limited")
            raise LLMUnavailable("rate limited") from exc
        except anthropic.APIStatusError as exc:
            _count(error=True)
            gen.update(level="ERROR", status_message=f"API {exc.status_code}")
            raise LLMUnavailable(f"API error {exc.status_code}: {str(exc)[:200]}") from exc
        except anthropic.APIConnectionError as exc:
            _count(error=True)
            gen.update(level="ERROR", status_message="connection error")
            raise LLMUnavailable("connection error") from exc
        _count(input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens)
        gen.update(output=[b.model_dump() for b in response.content],
                   usage_details={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                   metadata={"stop_reason": response.stop_reason})
        return response


def json_call(name: str, *, system: str, user: str, schema: dict, max_tokens: int = 400,
              metadata: dict[str, Any] | None = None, prompt: Any = None) -> dict:
    """A call whose answer must match `schema`, using structured outputs."""
    response = create(name, system=system, messages=[{"role": "user", "content": user}], max_tokens=max_tokens,
                      output_config={"format": {"type": "json_schema", "schema": schema}},
                      metadata=metadata, prompt=prompt)
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise LLMUnavailable("model returned invalid JSON") from exc


def _count(*, input_tokens: int = 0, output_tokens: int = 0, error: bool = False) -> None:
    with _lock:
        if error:
            _usage["errors"] += 1
        else:
            _usage["calls"] += 1
            _usage["input_tokens"] += input_tokens
            _usage["output_tokens"] += output_tokens
