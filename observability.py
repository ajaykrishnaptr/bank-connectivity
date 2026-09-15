"""
Tracing for every assistant question, tool call, model call and cron run.

Two sinks carry the same trace_id and span_id:
  1. Langfuse, when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set
     (LANGFUSE_TRACING_ENABLED=false turns it off). IBANs are masked before
     export unless LANGFUSE_MASK_IBANS=false.
  2. The HEC event log (eventlog.py).

Environment: LANGFUSE_TRACING_ENVIRONMENT if set, else "vercel" on Vercel and
"local" elsewhere; evaluation scripts set "eval" so runs never mix with traffic.

Pattern copied from the payee-assist build, trimmed to what FintNet uses.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

import eventlog

_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
_client = None
_current: ContextVar[dict | None] = ContextVar("fintnet_trace", default=None)


def enabled() -> bool:
    keys = os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    return bool(keys) and os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() != "false"


def mask(*, data: Any, **_: Any) -> Any:
    """Mask IBANs anywhere in a Langfuse payload: DE89 **** 3000."""
    if os.getenv("LANGFUSE_MASK_IBANS", "true").lower() == "false":
        return data
    if isinstance(data, str):
        return _IBAN.sub(lambda m: (c := m.group(0).replace(" ", ""))[:4] + " **** " + c[-4:], data)
    if isinstance(data, dict):
        return {k: mask(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(data=v) for v in data]
    return data


def client():
    global _client
    if _client is None and enabled():
        from langfuse import Langfuse
        env = os.getenv("LANGFUSE_TRACING_ENVIRONMENT") or ("vercel" if os.getenv("VERCEL") else "local")
        _client = Langfuse(mask=mask, environment=env,
                           release=(os.getenv("VERCEL_GIT_COMMIT_SHA") or "")[:7] or None)
    return _client


def version(text: str) -> str:
    """Short hash of a system prompt, so runs can be grouped by prompt version."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


class _Observation:
    """Forwards updates to the Langfuse observation, if any, and keeps them for the event log."""

    def __init__(self, obs: Any = None):
        self._obs, self.fields = obs, {}

    def update(self, **kw: Any) -> "_Observation":
        self.fields.update({k: v for k, v in kw.items() if v is not None})
        if self._obs is not None:
            self._obs.update(**kw)
        return self


def _sourcetype(as_type: str, name: str) -> str:
    if as_type == "generation":
        return "llm:generation"
    if name.startswith("tool:"):
        return "fintnet:tool"
    if name.startswith("cron:"):
        return "fintnet:cron"
    return "fintnet:request" if as_type == "request" else "fintnet:step"


@contextmanager
def _observe(as_type: str, name: str, *, input: Any = None, metadata: dict[str, Any] | None = None,
             tags: list[str] | None = None, langfuse_kwargs: dict[str, Any] | None = None) -> Iterator[_Observation]:
    c = client()
    parent = _current.get()
    started = time.time()
    status, error = "ok", None

    def body(obs: Any) -> Iterator[_Observation]:
        nonlocal status, error
        trace_id = (c.get_current_trace_id() if obs is not None else None) or (parent or {}).get("trace_id") \
            or uuid.uuid4().hex
        span_id = (c.get_current_observation_id() if obs is not None else None) or uuid.uuid4().hex[:16]
        token = _current.set({"trace_id": trace_id, "span_id": span_id})
        wrapper = _Observation(obs)
        try:
            yield wrapper
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {str(exc)[:300]}"
            raise
        finally:
            _current.reset(token)
            f = wrapper.fields
            if f.get("level") == "ERROR":
                status = "error"
            usage = f.get("usage_details") or {}
            eventlog.emit(_sourcetype(as_type, name), {
                "trace_id": trace_id, "span_id": span_id, "parent_span_id": (parent or {}).get("span_id"),
                "name": name, "kind": as_type, "status": status, "error": error or f.get("status_message"),
                "duration_ms": round((time.time() - started) * 1000), "tags": tags, "metadata": metadata,
                "model": (langfuse_kwargs or {}).get("model"),
                "input_tokens": usage.get("input"), "output_tokens": usage.get("output"),
                "input": input, "output": f.get("output")}, when=started)

    if c is None:
        yield from body(None)
        return
    if as_type == "request":
        from langfuse import propagate_attributes
        with propagate_attributes(trace_name=name, tags=tags or [],
                                  metadata={k: str(v) for k, v in (metadata or {}).items()}):
            with c.start_as_current_observation(as_type="span", name=name, input=input) as obs:
                yield from body(obs)
    else:
        with c.start_as_current_observation(as_type=as_type, name=name, input=input, metadata=metadata,
                                            **(langfuse_kwargs or {})) as obs:
            yield from body(obs)


@contextmanager
def request(name: str, *, input: Any = None, tags: list[str] | None = None,
            metadata: dict[str, Any] | None = None) -> Iterator[_Observation]:
    """One trace per assistant question or cron run."""
    with _observe("request", name, input=input, tags=tags, metadata=metadata) as obs:
        yield obs


@contextmanager
def generation(name: str, *, model: str, system: str, messages: Any,
               model_parameters: dict[str, Any], metadata: dict[str, Any] | None = None) -> Iterator[_Observation]:
    with _observe("generation", name, input={"system": system, "messages": messages},
                  metadata={"provider": "anthropic", **(metadata or {})},
                  langfuse_kwargs={"model": model, "model_parameters": model_parameters}) as obs:
        yield obs


@contextmanager
def step(name: str, *, input: Any = None, metadata: dict[str, Any] | None = None) -> Iterator[_Observation]:
    """A child observation of whatever is current, for example one tool call."""
    with _observe("span", name, input=input, metadata=metadata) as obs:
        yield obs


def trace_id() -> str | None:
    return (_current.get() or {}).get("trace_id")


def score(name: str, value: float | str, *, data_type: str = "NUMERIC", comment: str | None = None) -> None:
    """Score the current trace, for example evaluation accuracy."""
    c = client()
    if c is not None:
        c.score_current_trace(name=name, value=value, data_type=data_type, comment=comment)
    ctx = _current.get() or {}
    eventlog.emit("fintnet:score", {"trace_id": ctx.get("trace_id"), "span_id": ctx.get("span_id"),
                                    "name": name, "value": value, "comment": comment})


def flush() -> None:
    c = client()
    if c is not None:
        c.flush()
