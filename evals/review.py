"""
Human review in Langfuse: the score schema and the annotation queue.

    python evals/review.py setup          # create the score configs and the queue
    python evals/review.py queue [n]      # put the newest assistant answers in the queue
    python evals/review.py queue-bad      # queue only answers a reader marked not useful
    python evals/review.py status         # what is waiting for review

Scores a reviewer gives here land on the same traces as the automatic ones, so
one trace carries the code grade, the judge and the human verdict.
"""
from __future__ import annotations

import os
import sys

import requests

BASE = (os.getenv("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com").rstrip("/")
AUTH = (os.getenv("LANGFUSE_PUBLIC_KEY", ""), os.getenv("LANGFUSE_SECRET_KEY", ""))
QUEUE_NAME = "assistant-answers"

# What a reviewer can record on an answer.
SCORE_CONFIGS = [
    {"name": "human_faithful", "dataType": "BOOLEAN",
     "description": "Every figure in the answer comes from the tool results"},
    {"name": "human_in_scope", "dataType": "BOOLEAN",
     "description": "Stays within spending, income, balances and recurring payments"},
    {"name": "human_quality", "dataType": "CATEGORICAL",
     "categories": [{"label": "good", "value": 2}, {"label": "acceptable", "value": 1}, {"label": "poor", "value": 0}],
     "description": "Overall usefulness of the answer to the customer"},
    {"name": "human_comment", "dataType": "NUMERIC", "minValue": 0, "maxValue": 1,
     "description": "Reviewed, with a written comment"},
]


def _call(method: str, path: str, **kw):
    r = requests.request(method, BASE + path, auth=AUTH, timeout=30, **kw)
    if r.status_code >= 300:
        sys.exit(f"{method} {path} -> {r.status_code} {r.text[:300]}")
    return r.json() if r.text else {}


def _configs() -> dict[str, str]:
    return {c["name"]: c["id"] for c in _call("GET", "/api/public/score-configs?limit=100")["data"]}


def _queue() -> dict | None:
    for q in _call("GET", "/api/public/annotation-queues?limit=100")["data"]:
        if q["name"] == QUEUE_NAME:
            return q
    return None


def setup() -> None:
    existing = _configs()
    ids = []
    for config in SCORE_CONFIGS:
        if config["name"] in existing:
            ids.append(existing[config["name"]])
            print(f"score config {config['name']}: already there")
            continue
        created = _call("POST", "/api/public/score-configs", json=config)
        ids.append(created["id"])
        print(f"score config {config['name']}: created ({config['dataType']})")
    queue = _queue()
    if queue:
        print(f"queue {QUEUE_NAME}: already there ({queue['id']})")
        return
    queue = _call("POST", "/api/public/annotation-queues",
                  json={"name": QUEUE_NAME, "description": "Assistant answers for human review",
                        "scoreConfigIds": ids})
    print(f"queue {QUEUE_NAME}: created ({queue['id']})")


def _assistant_traces(limit: int) -> list[dict]:
    data = _call("GET", f"/api/public/traces?limit={limit}&name=assistant-question")["data"]
    return data


def queue(count: int = 10, only_bad: bool = False) -> None:
    q = _queue()
    if q is None:
        sys.exit("run `python evals/review.py setup` first")
    added = 0
    for trace in _assistant_traces(50):
        scores = trace.get("scores") or []
        if only_bad and not any(s.get("name") == "user_feedback" and float(s.get("value", 1)) == 0 for s in scores):
            continue
        _call("POST", f"/api/public/annotation-queues/{q['id']}/items",
              json={"objectId": trace["id"], "objectType": "TRACE", "status": "PENDING"})
        added += 1
        if added >= count:
            break
    print(f"queued {added} answer{'' if added == 1 else 's'} in {QUEUE_NAME}")


def status() -> None:
    q = _queue()
    if q is None:
        print("no queue yet: run setup")
        return
    items = _call("GET", f"/api/public/annotation-queues/{q['id']}/items?limit=100")["data"]
    pending = [i for i in items if i.get("status") == "PENDING"]
    print(f"{QUEUE_NAME}: {len(pending)} pending of {len(items)} items")
    print(f"review at {BASE}/project/<project>/annotation-queues/{q['id']}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "setup":
        setup()
    elif command == "queue":
        queue(int(sys.argv[2]) if len(sys.argv) > 2 else 10)
    elif command == "queue-bad":
        queue(20, only_bad=True)
    else:
        status()
