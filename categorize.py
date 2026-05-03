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
    ("telekom",         "Utilities"),         # Deutsche Telekom, T-Mobile, etc.
    ("vodafone",        "Utilities"),         # Telco
    ("deutsche bank",   "Transfers / Other"), # Interbank transfers, not income
    ("commerzbank",     "Transfers / Other"),
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
