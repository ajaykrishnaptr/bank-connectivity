"""
Step 10 — A tiny tool-using agent over your FintNet transactions.

Run:
    python3 agent.py "how much did I spend on groceries last month?"

What this teaches:
  - The "agent loop": LLM picks a tool, your code runs it, result goes
    back to the LLM, repeat until the LLM produces a final answer.
  - JSON mode as the contract between LLM and code (no regex parsing).
  - Tools should return *small* results. CPU inference is bottlenecked
    on prompt evaluation, so dumping 50 raw transactions back into the
    context will hang the model. Each tool here returns a scalar
    summary; the LLM composes by re-issuing filter args, not by
    passing data between tools.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "instance" / "ais.db"
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:3b"
MAX_TURNS = 5


# ---------------------------------------------------------------------------
# Tools — plain Python functions. Each returns a small scalar result.
# ---------------------------------------------------------------------------

def _filter_clause(days: int, category: str | None):
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    sql = "WHERE booking_date >= ? "
    params: list = [cutoff]
    if category:
        sql += "AND category = ? "
        params.append(category)
    return sql, params


def total_spent(days: int, category: str | None = None) -> dict:
    """Sum amounts in the window, split into inflow vs outflow."""
    where, params = _filter_clause(days, category)
    sql = (
        "SELECT "
        "  COUNT(*) AS count, "
        "  COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0) AS inflow, "
        "  COALESCE(SUM(CASE WHEN amount < 0 THEN amount END), 0) AS outflow "
        "FROM transactions " + where
    )
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(sql, params).fetchone()
    count, inflow, outflow = row
    return {
        "count": count,
        "inflow": round(float(inflow), 2),
        "outflow": round(float(outflow), 2),
        "net": round(float(inflow) + float(outflow), 2),
    }


def top_merchants(days: int, category: str | None = None, n: int = 5) -> dict:
    """Return the top `n` merchants by absolute outflow in the window."""
    where, params = _filter_clause(days, category)
    sql = (
        "SELECT creditor_name, ROUND(SUM(amount), 2) AS total "
        "FROM transactions " + where +
        "AND amount < 0 AND creditor_name IS NOT NULL "
        "GROUP BY creditor_name ORDER BY SUM(amount) ASC LIMIT ?"
    )
    params.append(int(n))
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"merchants": [{"name": r[0], "total": r[1]} for r in rows]}


TOOLS = {
    "total_spent": total_spent,
    "top_merchants": top_merchants,
}

TOOL_SPECS = """
Available tools (each returns a small JSON object — no large data is ever
passed back to you):

1. total_spent(days: int, category: str | null)
   Returns {count, inflow, outflow, net} for the last `days` days.
   `outflow` is negative (spending). Optional category filter.

2. top_merchants(days: int, category: str | null, n: int = 5)
   Returns {merchants: [{name, total}, ...]} ranked by spending.

Valid categories: Groceries, Food Delivery, Dining, Transport,
ATM / Cash, Shopping, Entertainment, Utilities, Healthcare,
Health & Fitness, Housing, Charity, Income, Transfers / Other.
"""

SYSTEM_PROMPT = f"""You are a financial assistant with access to the user's
bank transactions through tools. Respond with valid JSON only, in one of
these two shapes:

  {{"action": "tool_call", "tool": "<name>", "args": {{...}}}}
  {{"action": "answer", "text": "<one or two sentences>"}}

Use tool_call when you need data. Use answer when you have enough.
Never invent numbers — always fetch them via a tool first.
{TOOL_SPECS}"""


# ---------------------------------------------------------------------------
# Ollama call
# ---------------------------------------------------------------------------

def chat(messages: list[dict]) -> str:
    r = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": messages,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0},
        },
        timeout=600,
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------

def run_agent(question: str) -> None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    for turn in range(1, MAX_TURNS + 1):
        print(f"\n--- turn {turn} ---", flush=True)
        raw = chat(messages)
        print(f"LLM said: {raw}", flush=True)

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            print("(could not parse JSON, stopping)", flush=True)
            return

        action = parsed.get("action")

        if action == "answer":
            print(f"\nFINAL ANSWER: {parsed.get('text', '(empty)')}", flush=True)
            return

        if action == "tool_call":
            tool_name = parsed.get("tool")
            args = parsed.get("args", {}) or {}
            fn = TOOLS.get(tool_name)
            if fn is None:
                tool_result = {"error": f"unknown tool {tool_name!r}"}
            else:
                try:
                    tool_result = fn(**args)
                except Exception as e:
                    tool_result = {"error": f"{type(e).__name__}: {e}"}

            print(f"-> ran {tool_name}({args}) -> {tool_result}", flush=True)

            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "tool",
                "content": json.dumps(tool_result, default=str),
            })
            continue

        print(f"(unexpected action {action!r}, stopping)", flush=True)
        return

    print("(hit max turns without a final answer)", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python3 agent.py "your question"')
        sys.exit(1)
    run_agent(" ".join(sys.argv[1:]))
