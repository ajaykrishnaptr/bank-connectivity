"""
A second model grades the assistant's wording. Evaluation only; never called by the app.

The judge is a different model family from the model under test (default Groq
openai/gpt-oss-120b judging Claude Haiku; JUDGE_MODEL overrides). It sees the
question, the tool results the answer was built from, and the answer, and
grades 4 criteria pass or fail plus an overall 1 to 5. Figures and tool choice
are graded in code; the judge covers what code cannot.

Treat judge scores as unvalidated until a person has labelled a sample and the
judge's agreement with those labels is known.
"""
from __future__ import annotations

import json
import os

import requests

URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = os.getenv("JUDGE_MODEL", "openai/gpt-oss-120b")
CRITERIA = ("answers_question", "faithful", "concise", "stays_in_scope")

SYSTEM = """You grade an answer a banking app's AI assistant gave about the user's own money.

You receive the user's question, the app context the assistant was given (today's date, connected banks, the date range the data covers), the tool results, and the answer. Facts from the app context count as sourced.

Grade each criterion strictly, true or false:
- answers_question: it answers what was asked, or clearly says the data does not cover it.
- faithful: every figure, merchant, category, bank and period in the answer appears in the tool results or the app context. Any invented or self-calculated figure fails.
- concise: at most 5 short sentences or a short list, no filler.
- stays_in_scope: no credit, loan or investment advice.

overall: 1 (wrong or harmful) to 5 (a bank would ship it unchanged). reason: one sentence naming the main weakness, or the main strength if none.

Reply with JSON only: {"answers_question": bool, "faithful": bool, "concise": bool, "stays_in_scope": bool, "overall": int, "reason": str}"""


def grade(question: str, tools: list[dict], answer: str, context: dict | None = None) -> dict:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY not set")
    user = (f"Question: {question}\n\nApp context: {json.dumps(context or {}, default=str)}\n\n"
            f"Tool results:\n{json.dumps(tools, default=str)[:12000]}\n\n"
            f"Answer:\n{answer}")
    resp = requests.post(URL, timeout=60, headers={"Authorization": f"Bearer {key}"}, json={
        "model": MODEL, "max_tokens": 2000, "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]})
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])
