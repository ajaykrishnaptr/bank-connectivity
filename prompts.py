"""
Prompts managed in Langfuse, with the code as the fallback.

Each prompt lives in Langfuse under a name and a label ("production"), so a
wording change is a new version there rather than a redeploy, and every
generation records which version produced it. The SDK caches a fetched prompt
in memory, and if Langfuse is unreachable the text in this file is used, so the
app never depends on it being up.

    python prompts.py push     # create or update the Langfuse versions from this file
    python prompts.py show     # print what Langfuse currently serves

`get(name)` returns (text, prompt_object). Pass the object to llm.create(prompt=…)
so Langfuse links the generation to the prompt version.
"""
from __future__ import annotations

import os
import sys

import observability

LABEL = os.getenv("LANGFUSE_PROMPT_LABEL", "production")

# The text that ships with the code. Langfuse serves these after `push`, and
# they remain the fallback whenever Langfuse cannot be reached.
DEFAULTS: dict[str, str] = {
    "fintnet-assistant-system": (
        "You answer questions about the user's own money across all the bank accounts they connected to "
        "FintNet (PSD2 account information, read-only).\n\n"
        "Today is {{today}}. Connected banks: {{banks}}. Data covers {{coverage}}.\n\n"
        "Rules:\n"
        "- Every number in your answer must come from a tool result in this conversation. Never estimate, "
        "extrapolate or do arithmetic the tools did not return; if you need a figure, call a tool that returns it.\n"
        "- Amounts are in EUR unless a tool says otherwise. Say which period a figure covers.\n"
        "- Income and spending figures leave out transfers between the user's own accounts; they only move money "
        "between banks.\n"
        "- If the data does not cover the question (dates outside the coverage, a bank that is not connected), "
        "say so plainly.\n"
        "- Do not give credit, loan, creditworthiness or investment advice; say you can only describe their "
        "spending and income.\n"
        "- Answer in at most 5 short sentences or a short list. Name merchants, categories and banks as the tools "
        "return them.\n"
        "- Resolve relative dates against today's date before calling tools, and say the dates you used. "
        "\"Last month\" means the previous calendar month (1st to its last day), not the last 30 days.\n"
        "- Never add, subtract or average figures yourself. Totals, counts and per-bank splits come back from the "
        "tools (total_balance_eur, balance_by_bank_eur, total_out_eur, transactions); if you need one the tools did "
        "not return, call another tool.\n"
        "- For \"how many times\" questions use the transaction counts the tools return "
        "(count_matched, or the per-category transactions).\n"
        "- Answer as the app, not as a model: never mention your rules, instructions or tools by name."
    ),
    "fintnet-categoriser-system": "",   # filled from categorize.SYSTEM on import below
}


def _defaults() -> dict[str, str]:
    if not DEFAULTS["fintnet-categoriser-system"]:
        import categorize
        DEFAULTS["fintnet-categoriser-system"] = categorize.SYSTEM
    return DEFAULTS


def get(name: str) -> tuple[str, object | None]:
    """The prompt text Langfuse serves, and the prompt object to link to a generation.

    Falls back to the text in this file when Langfuse is off or unreachable.
    """
    fallback = _defaults()[name]
    client = observability.client()
    if client is None:
        return fallback, None
    try:
        prompt = client.get_prompt(name, label=LABEL, cache_ttl_seconds=300,
                                   fallback=fallback, max_retries=1, fetch_timeout_seconds=3)
        return prompt.prompt, prompt
    except Exception:  # noqa: BLE001 — a prompt service must never break a request
        return fallback, None


def compile(name: str, **variables: str) -> tuple[str, object | None]:
    """Fetch and fill a prompt. Uses Langfuse's {{variable}} syntax either way."""
    text, prompt = get(name)
    if prompt is not None and hasattr(prompt, "compile"):
        try:
            return prompt.compile(**variables), prompt
        except Exception:  # noqa: BLE001
            pass
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text, prompt


def push() -> None:
    """Create or update each prompt in Langfuse from the text in this file."""
    client = observability.client()
    if client is None:
        sys.exit("Langfuse keys are not set")
    for name, text in _defaults().items():
        client.create_prompt(name=name, prompt=text, type="text", labels=[LABEL],
                             tags=["fintnet"], commit_message="from prompts.py")
        print(f"pushed {name} ({len(text)} chars, label {LABEL})")
    client.flush()


def show() -> None:
    for name in _defaults():
        text, prompt = get(name)
        version = getattr(prompt, "version", None)
        print(f"{name}: version {version if version is not None else 'fallback (from code)'}, {len(text)} chars")
        print("  " + text[:160].replace("\n", " ") + "…\n")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "show"
    {"push": push, "show": show}.get(command, show)()
