"""
Transaction categorizer with two modes:
  - rule-based (default, fast, brittle on new merchants)
  - AI-based   (Ollama + Qwen 2.5, slower first time, good on unseen merchants)

Toggle via env var: USE_AI_CATEGORIZER=true

AI results are cached in the merchant_categories table — same merchant is
only sent to the LLM once, ever. Subsequent lookups are a single SQL row read.
"""
import os

from logging_config import log
from models import MerchantCategory, db

USE_AI = os.getenv("USE_AI_CATEGORIZER", "true").lower() == "true"

# Hand-curated overrides for cases where the LLM is reliably wrong.
# Checked BEFORE the LLM (and before the cache) — keep this list small.
# Match is a case-insensitive substring check on the merchant name.
_OVERRIDES = [
    # Utilities / telcos
    ("telekom",          "Utilities"),         # Deutsche Telekom, T-Mobile, etc.
    ("vodafone",         "Utilities"),
    # Banks (interbank transfers, not income)
    ("deutsche bank",    "Transfers / Other"),
    ("commerzbank",      "Transfers / Other"),
    # Indian IT employers (LLM kept routing these to Housing — clear bug)
    ("infosys",          "Income"),
    ("wipro",            "Income"),
    ("tata consultancy", "Income"),
    ("freelance",        "Income"),
    # Specific retailers the LLM mishandled
    ("heinemann",        "Shopping"),         # duty-free retailer, not Housing
    # Finnish-specific merchants
    ("helsinkimissio",   "Charity"),          # Finnish charity, not Transport
    ("kela",             "Income"),           # Finnish social security benefits
]

CATEGORIES = [
    "Groceries", "Food Delivery", "Dining", "Transport", "ATM / Cash",
    "Shopping", "Entertainment", "Utilities", "Healthcare",
    "Health & Fitness", "Housing", "Charity", "Income",
    "Transfers / Other",
]

_RULES = [
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
    if not merchant:
        return "Transfers / Other"
    m = merchant.lower()
    for category, keywords in _RULES:
        if any(kw in m for kw in keywords):
            return category
    return "Transfers / Other"


def _categorize_by_ai(merchant: str) -> str:
    """Ask Ollama / Qwen to classify the merchant. Returns one of CATEGORIES."""
    import ollama

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
        model="qwen2.5:3b",
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )
    answer = response["message"]["content"].strip()
    # Defensive: the model occasionally adds punctuation. Match against the allowed list.
    for cat in CATEGORIES:
        if cat.lower() in answer.lower():
            return cat
    log.warning("categorize.ai.unknown_label", extra={
        "event": "categorize.ai.unknown_label", "merchant": merchant, "raw": answer[:100],
    })
    return "Transfers / Other"


def categorize_with_confidence(merchant: str) -> dict:
    """Ask the LLM for category, confidence, and reasoning as JSON.

    Returns a dict like:
        {"category": "Utilities", "confidence": "high", "reasoning": "..."}

    On parse failure, falls back to rules with confidence='low'.
    """
    import json
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
        model="qwen2.5:3b",
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )
    raw = response["message"]["content"].strip()

    # Strip markdown fences if the model wrapped its output: ```json ... ```
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    # Also: model sometimes adds a trailing comment after the JSON; isolate the {...} block
    if "{" in raw and "}" in raw:
        raw = raw[raw.index("{"): raw.rindex("}") + 1]

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
    """Public API. Routes to AI or rules depending on env var; AI mode is cached in DB."""
    if not merchant:
        return "Transfers / Other"

    if not USE_AI:
        return _categorize_by_rules(merchant)

    # Manual overrides — beat both cache and LLM for known stubborn cases
    m_lower = merchant.lower()
    for keyword, category in _OVERRIDES:
        if keyword in m_lower:
            return category

    cached = MerchantCategory.query.filter_by(merchant=merchant).first()
    if cached:
        return cached.category

    try:
        category = _categorize_by_ai(merchant)
        source = "ai"
    except Exception as e:
        log.warning("categorize.ai.failed", extra={
            "event": "categorize.ai.failed", "merchant": merchant, "error": str(e)[:200],
        })
        category = _categorize_by_rules(merchant)
        source = "rule"

    db.session.add(MerchantCategory(merchant=merchant, category=category, source=source))
    db.session.commit()
    log.info("categorize.cached", extra={
        "event": "categorize.cached", "merchant": merchant, "category": category, "source": source,
    })
    return category
