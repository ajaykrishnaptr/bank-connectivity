"""
Transaction categorization — turns a raw merchant name into one of a
fixed list of categories (Groceries, Utilities, Income, ...).

The public entry point is `categorize(merchant)`. It's a four-layer
waterfall, designed so cheap deterministic answers come first and the
LLM is only consulted as a last resort:

    1. Manual overrides   — hand-curated keyword list, beats every other
                            layer. Used for cases where the LLM is
                            reliably wrong (e.g. "Infosys" → Income, not
                            Housing). Keep this list small.
    2. DB cache           — MerchantCategory table. Same merchant string
                            is only sent to the LLM once, ever.
    3. LLM (Ollama/Qwen)  — local, ~100ms per call. Result is written
                            back to the cache.
    4. Rule-based fallback — pure-Python keyword match. Used when AI is
                            disabled (USE_AI_CATEGORIZER=false) or when
                            the LLM call raises.

`categorize_with_confidence(merchant)` is a separate code path used by
the GenAI demo and the dashboard's "explain" UI: same prompt idea, but
the model returns a JSON object with a confidence and one-sentence
reasoning instead of a bare label. It does NOT go through the cache,
so call it sparingly.
"""
from __future__ import annotations

import json
import os

from logging_config import log
from models import MerchantCategory, db

# Toggle the AI path with an env var. False keeps the categorizer fully
# deterministic, useful for tests or when Ollama isn't running.
USE_AI = os.getenv("USE_AI_CATEGORIZER", "true").lower() == "true"

# Keep these in lock-step: changing the model name without re-evaluating
# prompt quality has bitten us before.
_OLLAMA_MODEL = "qwen2.5:3b"

# Hand-curated overrides for cases where the LLM is reliably wrong.
# Checked BEFORE the cache and the LLM — keep this list short, every
# entry is essentially a confession that the prompt isn't good enough.
# Match is a case-insensitive substring check on the merchant name.
_OVERRIDES: list[tuple[str, str]] = [
    # Utilities / telcos
    ("telekom",          "Utilities"),         # Deutsche Telekom, T-Mobile, etc.
    ("vodafone",         "Utilities"),
    # Banks (interbank transfers, not income)
    ("deutsche bank",    "Transfers / Other"),
    ("commerzbank",      "Transfers / Other"),
    # Indian IT employers — the LLM keeps routing these to Housing
    # because "Infosys Ltd" reads as a real-estate name to it.
    ("infosys",          "Income"),
    ("wipro",            "Income"),
    ("tata consultancy", "Income"),
    ("freelance",        "Income"),
    # Specific retailers the LLM mishandled
    ("heinemann",        "Shopping"),          # duty-free retailer, not Housing
    # Finnish-specific merchants
    ("helsinkimissio",   "Charity"),           # Finnish charity, not Transport
    ("kela",             "Income"),            # Finnish social-security benefits
]

# The full list of allowed labels. Both prompts reference this list, and
# we re-check the model's output against it (the LLM occasionally
# invents a label like "Salary" which is close but not in our taxonomy).
CATEGORIES = [
    "Groceries", "Food Delivery", "Dining", "Transport", "ATM / Cash",
    "Shopping", "Entertainment", "Utilities", "Healthcare",
    "Health & Fitness", "Housing", "Charity", "Income",
    "Transfers / Other",
]

# Keyword rules for the no-AI fallback. First match wins, and the order
# of the list is intentional — generic words ("shop", "store") sit at
# the bottom so they don't shadow more specific merchants.
_RULES: list[tuple[str, list[str]]] = [
    ("Groceries",        ["dmart", "big bazaar", "reliance fresh", "more supermarket", "spencer",
                          "k market", "wallmart", "walmart", "lidl", "spar", "prisma", "alepa", "s-market"]),
    ("Food Delivery",    ["swiggy", "zomato"]),
    ("Dining",           ["café", "cafe", "coffee day", "starbucks", "mcdonald", "kfc", "pizza",
                          "domino", "restaurant", "bistro", "eatery"]),
    ("Transport",        ["ola cabs", "uber", "irctc", "indigo", "air india", "avis", "taxi",
                          "metro", "bus ", "train", "ryanair", "finnair"]),
    ("ATM / Cash",       ["atm", "nosto"]),
    ("Shopping",         ["flipkart", "amazon", "myntra", "ajio", "heinemann", "retail", "shop", "store"]),
    ("Entertainment",    ["bookmyshow", "netflix", "hotstar", "prime video", "spotify", "cinema", "pvr"]),
    ("Utilities",        ["jio fiber", "jio ", "bses", "airtel", "tata power", "electricity",
                          "broadband", "fiber", "water board"]),
    ("Healthcare",       ["apollo", "medplus", "1mg", "pharmacy", "hospital", "clinic"]),
    ("Health & Fitness", ["sports club", "gym", "fitness", "cross  sports"]),
    ("Housing",          ["appartment", "vuokra", "rent", "housing"]),
    ("Charity",          ["helsinkimissio", " ry", " rf "]),
    ("Income",           ["salary", "kela", "fpa", "kansaneläke", "bonus", "freelance"]),
]


def _categorize_by_rules(merchant: str) -> str:
    """Pure keyword lookup. Returns 'Transfers / Other' if nothing matches."""
    if not merchant:
        return "Transfers / Other"
    m = merchant.lower()
    for category, keywords in _RULES:
        if any(kw in m for kw in keywords):
            return category
    return "Transfers / Other"


def _match_override(merchant: str) -> str | None:
    """If the merchant matches any hand-curated override, return that
    category. Otherwise None (so the caller falls through to the cache
    or the LLM)."""
    m = merchant.lower()
    for keyword, category in _OVERRIDES:
        if keyword in m:
            return category
    return None


def _extract_json_blob(raw: str) -> str:
    """Extract just the {...} from a model response.

    The model usually replies with bare JSON but sometimes wraps it in
    ```json fences``` or appends a trailing comment. We strip those
    here so json.loads doesn't choke.
    """
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Drop the opening fence; drop the closing fence if present.
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    if "{" in raw and "}" in raw:
        raw = raw[raw.index("{"): raw.rindex("}") + 1]
    return raw


def _categorize_by_ai(merchant: str) -> str:
    """Ask the local Ollama model to classify the merchant.

    Returns one of CATEGORIES — if the model invents something we
    don't recognise, we log it and return 'Transfers / Other' so the
    system always produces a valid label.
    """
    import ollama  # local import — only needed when AI mode is enabled.

    examples = """Examples:
Merchant: Tata Power -> Utilities
Merchant: BSES Yamuna -> Utilities
Merchant: Vattenfall -> Utilities
Merchant: BVG -> Transport
Merchant: Uber -> Transport
Merchant: DMart -> Groceries
Merchant: Lieferando -> Food Delivery
Merchant: FitX Gym -> Health & Fitness
Merchant: Salary - Acme Corp -> Income
Merchant: Apollo Pharmacy -> Healthcare"""

    prompt = f"""You are a transaction categorization assistant.

Pick exactly ONE category from this list that best matches the merchant.
Reply with ONLY the category name, nothing else.

Categories: {", ".join(CATEGORIES)}

{examples}

Now categorize this merchant:
Merchant: {merchant}
Category:"""

    response = ollama.chat(
        model=_OLLAMA_MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},  # zero temperature → reproducible answers
    )
    answer = response["message"]["content"].strip()

    # Defensive: the model occasionally adds punctuation around the label.
    # Match against the allowed list rather than trusting the raw string.
    for cat in CATEGORIES:
        if cat.lower() in answer.lower():
            return cat

    log.warning("categorize.ai.unknown_label", extra={
        "event": "categorize.ai.unknown_label", "merchant": merchant, "raw": answer[:100],
    })
    return "Transfers / Other"


def categorize_with_confidence(merchant: str) -> dict:
    """Return `{category, confidence, reasoning}` for a merchant.

    Used by the GenAI demo / dashboard explain panel. Bypasses the
    cache and the override table on purpose — if you only want a
    label, call `categorize()` instead, which is much cheaper.

    On JSON parse failure we fall back to the rule layer with
    confidence='low' so the caller always gets something usable.
    """
    import ollama

    prompt = f"""You are a transaction categorization assistant.

Given a merchant name, output a JSON object with three fields:
- category: exactly ONE category from this list:
  {", ".join(CATEGORIES)}
- confidence: one of "high", "medium", "low"
- reasoning: a one-sentence explanation (max 15 words)

Output ONLY the JSON, no other text. No markdown fences.

Examples:
Merchant: Tata Power
Output: {{"category": "Utilities", "confidence": "high", "reasoning": "Tata Power is an Indian electricity provider"}}

Merchant: Lieferando
Output: {{"category": "Food Delivery", "confidence": "high", "reasoning": "Lieferando is a German food delivery service"}}

Merchant: random unknown name
Output: {{"category": "Transfers / Other", "confidence": "low", "reasoning": "Unrecognised merchant, no clear category"}}

Now categorize this:
Merchant: {merchant}
Output:"""

    response = ollama.chat(
        model=_OLLAMA_MODEL,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )
    raw = _extract_json_blob(response["message"]["content"].strip())

    try:
        parsed = json.loads(raw)
        cat = parsed.get("category", "Transfers / Other")
        # Sanity-check: did the model invent a category outside our list?
        if cat not in CATEGORIES:
            cat = "Transfers / Other"
        return {
            "category":   cat,
            "confidence": parsed.get("confidence", "low"),
            "reasoning":  parsed.get("reasoning", "")[:200],
        }
    except (json.JSONDecodeError, AttributeError):
        log.warning("categorize.json.parse_failed", extra={
            "event": "categorize.json.parse_failed", "merchant": merchant, "raw": raw[:200],
        })
        return {
            "category":   _categorize_by_rules(merchant),
            "confidence": "low",
            "reasoning":  "JSON parse failed; fell back to rules",
        }


def categorize(merchant: str) -> str:
    """Public API. Returns a category name from CATEGORIES.

    Layered: override -> cache -> AI -> rules. See module docstring.
    """
    if not merchant:
        return "Transfers / Other"

    # AI mode disabled entirely — short-circuit straight to rules.
    if not USE_AI:
        return _categorize_by_rules(merchant)

    # 1. Manual overrides beat both cache and LLM.
    override = _match_override(merchant)
    if override is not None:
        return override

    # 2. Have we seen this merchant before?
    cached = MerchantCategory.query.filter_by(merchant=merchant).first()
    if cached:
        return cached.category

    # 3. Ask the LLM. Fall back to rules on any failure (network,
    # Ollama not running, model output garbled). Catching broad
    # Exception here is deliberate — this path must never bubble up
    # into the request handler.
    try:
        category = _categorize_by_ai(merchant)
        source = "ai"
    except Exception as e:  # noqa: BLE001 — see comment above
        log.warning("categorize.ai.failed", extra={
            "event": "categorize.ai.failed", "merchant": merchant, "error": str(e)[:200],
        })
        category = _categorize_by_rules(merchant)
        source = "rule"

    # Cache the answer so we never pay for this merchant again.
    db.session.add(MerchantCategory(merchant=merchant, category=category, source=source))
    db.session.commit()
    log.info("categorize.cached", extra={
        "event": "categorize.cached", "merchant": merchant, "category": category, "source": source,
    })
    return category
