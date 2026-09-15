"""
Structured event log in the Splunk HTTP Event Collector (HEC) format.

Every assistant request, tool call, model call, cron run and score is written
as one JSON line:
  {"time": 1757923200.123, "host": "...", "source": "fintnet",
   "sourcetype": "fintnet:tool", "index": "fintnet", "event": {...}}
That is the body a Splunk HEC endpoint accepts, so the same lines can be sent
to a real Splunk instance unchanged (set SPLUNK_HEC_URL and SPLUNK_HEC_TOKEN).

Stores, off when EVENT_LOG=off:
  * Local: a file (EVENT_LOG_PATH, default logs/events.jsonl), written in full.
  * Vercel: Upstash Redis (KV_REST_API_URL, KV_REST_API_TOKEN). The newest
    EVENT_LOG_MAX events (default 2000) are kept, and bearer and access tokens
    are masked to their last 6 characters.

The log must never break a request, so every write swallows its own errors.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

import requests

INDEX = "fintnet"
_lock = threading.Lock()
_ROOT = Path(__file__).resolve().parent
_REDIS_KEY = os.getenv("EVENT_LOG_REDIS_KEY", "fintnet:events")
_TOKEN_KEYS = {"access_token", "refresh_token", "id_token", "consent_id"}


def backend() -> str | None:
    if os.getenv("EVENT_LOG", "on").lower() == "off":
        return None
    hosted = os.getenv("VERCEL") or os.getenv("EVENT_LOG_BACKEND") == "redis"
    if hosted and os.getenv("KV_REST_API_URL") and os.getenv("KV_REST_API_TOKEN"):
        return "redis"
    return None if os.getenv("VERCEL") else "file"


def enabled() -> bool:
    return backend() is not None


def _max_events() -> int:
    return int(os.getenv("EVENT_LOG_MAX", "2000"))


def _tail(value: str) -> str:
    return "****" + value[-6:] if len(value) > 6 else "****"


def mask_tokens(value: Any, key: str | None = None) -> Any:
    """Bearer headers and OAuth token fields keep only their last 6 characters."""
    if isinstance(value, dict):
        return {k: mask_tokens(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_tokens(v) for v in value]
    if isinstance(value, str):
        if key in _TOKEN_KEYS:
            return _tail(value)
        if value.startswith("Bearer ") and len(value) > 20:
            return "Bearer " + _tail(value[7:])
    return value


def _redis(commands: list[list]) -> list:
    resp = requests.post(os.environ["KV_REST_API_URL"].rstrip("/") + "/pipeline", json=commands, timeout=3.0,
                         headers={"Authorization": f"Bearer {os.environ['KV_REST_API_TOKEN']}"})
    resp.raise_for_status()
    return resp.json()


def path() -> Path:
    p = Path(os.getenv("EVENT_LOG_PATH", "logs/events.jsonl"))
    return p if p.is_absolute() else _ROOT / p


def emit(sourcetype: str, event: dict[str, Any], *, when: float | None = None) -> None:
    store = backend()
    if store is None:
        return
    record = {"time": round(when if when is not None else time.time(), 3), "host": socket.gethostname(),
              "source": "fintnet", "sourcetype": sourcetype, "index": INDEX,
              "event": mask_tokens(event) if store == "redis" else event}
    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        if store == "redis":
            _redis([["LPUSH", _REDIS_KEY, line], ["LTRIM", _REDIS_KEY, 0, _max_events() - 1]])
        else:
            p = path()
            with _lock:
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
    except Exception:  # noqa: BLE001 — the log must never break a request
        pass
    _forward(line)


def _forward(line: str) -> None:
    url, token = os.getenv("SPLUNK_HEC_URL"), os.getenv("SPLUNK_HEC_TOKEN")
    if not (url and token):
        return

    def post() -> None:
        try:
            requests.post(url, data=line, headers={"Authorization": f"Splunk {token}"}, timeout=5.0)
        except requests.RequestException:
            pass

    threading.Thread(target=post, daemon=True).start()


def read(limit: int = 500) -> list[dict]:
    """The newest events, newest first. Unreadable lines are skipped."""
    try:
        if backend() == "redis":
            lines = _redis([["LRANGE", _REDIS_KEY, 0, min(limit, _max_events()) - 1]])[0].get("result") or []
        else:
            p = path()
            if not p.exists():
                return []
            with _lock, p.open(encoding="utf-8") as f:
                lines = list(reversed(f.readlines()[-limit:]))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
