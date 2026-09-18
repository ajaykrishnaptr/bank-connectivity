"""
Transaction categorisation — turns a merchant into one of a fixed list of
categories (Groceries, Utilities, Income, ...).

Waterfall, cheapest first:

    1. Manual overrides   — hand-curated keyword list; beats every other layer.
    2. Cache              — MerchantCategory, keyed on the normalised merchant.
                            A merchant is sent to the model once, ever.
    3. Model              — Claude Haiku (CATEGORIZER_PROVIDER=anthropic, the
                            default when ANTHROPIC_API_KEY is set) or a local
                            Ollama model (CATEGORIZER_PROVIDER=ollama). Returns
                            category, confidence 0-100 and a one-line reason.
    4. Rules              — keyword fallback. Rule answers are provisional:
                            they are NOT cached, and the categorise cron job
                            upgrades them with the model later.

Inserts (bank sync, the daily feed) never wait on the model: `categorize()`
runs layers 1, 2 and 4. `upgrade_provisional()` runs the model in capped
batches from the cron job. `model_categorize()` is the uncached model call
used by evaluation.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict

from fintnet.telemetry.logging_config import log
from fintnet.models import MerchantCategory, Transaction, db

CATEGORIES = [
    "Groceries", "Food Delivery", "Dining", "Transport", "ATM / Cash",
    "Shopping", "Entertainment", "Utilities", "Healthcare",
    "Health & Fitness", "Housing", "Charity", "Income",
    "Transfers / Other",
]

# Hand-curated overrides for cases where a model is reliably wrong. Keep short.
_OVERRIDES: list[tuple[str, str]] = [
    ("telekom",          "Utilities"),
    ("vodafone",         "Utilities"),
    ("deutsche bank",    "Transfers / Other"),
    ("commerzbank",      "Transfers / Other"),
    ("infosys",          "Income"),
    ("wipro",            "Income"),
    ("tata consultancy", "Income"),
    ("freelance",        "Income"),
    ("heinemann",        "Shopping"),
    ("helsinkimissio",   "Charity"),
    ("kela",             "Income"),
    ("savings transfer", "Transfers / Other"),
]

_RULES: list[tuple[str, list[str]]] = [
    ("Groceries",        ["rewe", "edeka", "lidl", "aldi", "kaufland", "netto", "penny", "spar", "prisma",
                          "alepa", "s-market", "k-market", "citymarket", "ica ", "coop", "willys", "hemkop",
                          "albert heijn", "jumbo", "esselunga", "conad", "carrefour", "dirk"]),
    ("Food Delivery",    ["lieferando", "wolt", "foodora", "uber eats", "thuisbezorgd", "glovo", "just eat",
                          "deliveroo"]),
    ("Dining",           ["cafe", "coffee", "starbucks", "mcdonald", "burger", "pizza", "restaurant", "bistro",
                          "vapiano", "osteria", "hesburger", "espresso house", "febo"]),
    ("Transport",        ["uber", "taxi", "bahn", "bvg", "mvg", "flixbus", "shell", "aral", "neste", "finnair",
                          "hsl", "circle k", "trenitalia", "italo", " ns", "gvb", "sl ", "sj "]),
    ("ATM / Cash",       ["atm", "nosto", "bargeld", "geldautomaat", "bankomat", "bancomat"]),
    ("Shopping",         ["amazon", "zalando", "mediamarkt", "ikea", "h&m", "zara", "otto", "stockmann",
                          "bol.com", "hema", "coolblue", "shop", "store"]),
    ("Entertainment",    ["netflix", "spotify", "disney", "dazn", "audible", "cinema", "kino", "viaplay"]),
    ("Utilities",        ["vattenfall", "e.on", "stadtwerke", "fortum", "helen", "elisa", "telia", "eneco",
                          "kpn", "ziggo", "enel", "a2a", "tim", "electricity", "broadband"]),
    ("Healthcare",       ["apotheke", "apteekki", "apotek", "apotheek", "farmacia", "pharmacy", "krankenkasse",
                          "clinic", "hospital"]),
    ("Health & Fitness", ["gym", "fitness", "mcfit", "fitx", "sats", "basic-fit", "yoga"]),
    ("Housing",          ["miete", "rent", "vonovia", "wohnen", "vuokra", "hyra", "huur", "affitto"]),
    ("Charity",          ["rotes kreuz", "unicef", "red cross", "punainen risti", "rode kruis", " e.v.", " ry"]),
    ("Income",           ["salary", "gehalt", "lohn", "palkka", "salaris", "stipendio", "pension", "rente"]),
]

_NORM_STRIP = re.compile(r"[^a-z0-9&*+. ]+")


def normalize(merchant: str) -> str:
    """Cache key: lowercase, punctuation collapsed, whitespace squeezed."""
    text = _NORM_STRIP.sub(" ", (merchant or "").lower())
    return re.sub(r"\s+", " ", text).strip()[:255]


def provider() -> str:
    chosen = os.getenv("CATEGORIZER_PROVIDER")
    if chosen in ("anthropic", "ollama", "rules"):
        return chosen
    if os.getenv("USE_AI_CATEGORIZER", "true").lower() == "false":
        return "rules"
    return "anthropic" if os.getenv("ANTHROPIC_API_KEY") else ("rules" if os.getenv("VERCEL") else "ollama")


def _categorize_by_rules(merchant: str) -> str:
    m = f" {(merchant or '').lower()} "
    for category, keywords in _RULES:
        if any(kw in m for kw in keywords):
            return category
    return "Transfers / Other"


def _match_override(merchant: str) -> str | None:
    m = (merchant or "").lower()
    for keyword, category in _OVERRIDES:
        if keyword in m:
            return category
    return None


def categorize_detailed(merchant: str) -> tuple[str, str]:
    """(category, source) without calling the model. source: override | cache | rule."""
    if not merchant:
        return "Transfers / Other", "rule"
    override = _match_override(merchant)
    if override is not None:
        return override, "override"
    cached = MerchantCategory.query.filter_by(merchant=normalize(merchant)).first()
    if cached:
        return cached.category, "cache"
    return _categorize_by_rules(merchant), "rule"


def categorize_many(merchants: list[str]) -> dict[str, tuple[str, str]]:
    """categorize_detailed for many merchants with one cache query (for bank syncs)."""
    keys = {m: normalize(m) for m in set(merchants) if m}
    cached = {}
    key_list = list(set(keys.values()))
    for i in range(0, len(key_list), 500):
        for row in MerchantCategory.query.filter(MerchantCategory.merchant.in_(key_list[i:i + 500])).all():
            cached[row.merchant] = row.category
    out: dict[str, tuple[str, str]] = {"": ("Transfers / Other", "rule")}
    for m, key in keys.items():
        override = _match_override(m)
        if override is not None:
            out[m] = (override, "override")
        elif key in cached:
            out[m] = (cached[key], "cache")
        else:
            out[m] = (_categorize_by_rules(m), "rule")
    return out


def categorize(merchant: str) -> str:
    """Public API kept for older callers: category name, no model call."""
    return categorize_detailed(merchant)[0]


# ── Model tier ────────────────────────────────────────────────────────────────

SYSTEM = """You categorise one bank transaction for a European personal-finance app.

Choose exactly one category:
- Groceries: supermarkets, bakeries, greengrocers, drinks shops
- Food Delivery: meal delivery apps and delivery restaurants
- Dining: restaurants, cafes, bars, fast food, canteens
- Transport: public transport, trains, taxis, ride hailing, fuel, parking, bike and scooter rental, flights
- ATM / Cash: cash withdrawals
- Shopping: retail, fashion, electronics, books, marketplaces, drugstores
- Entertainment: streaming and media subscriptions, cinema, theatre, events, bowling
- Utilities: electricity, gas, water, internet, phone contracts
- Healthcare: pharmacies, doctors, dentists, physiotherapy, health insurance
- Health & Fitness: gyms, yoga and sports studios, climbing halls
- Housing: rent, property management, housing associations
- Charity: donations, non-profit associations
- Income: salary, pension, benefits, invoices paid to the account holder
- Transfers / Other: transfers to or from people, own-account transfers, anything that fits no category

The merchant name may be in German, Finnish, Swedish, Dutch or Italian, truncated, upper-cased, misspelled,
or wrapped by a payment facilitator such as "SUMUP *", "SQ *", "ZETTLE_*", "PAYPAL *" or "STRIPE*";
in that case categorise the merchant after the prefix. Money received from a company is usually Income;
money sent to or received from a private person is Transfers / Other.

confidence is 0-100: how likely your category is correct. reason is at most 15 words."""

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["category", "confidence", "reason"],
    "additionalProperties": False,
}


def model_categorize(merchant: str, remittance: str = "", amount: float | None = None,
                     name: str = "categorise") -> dict:
    """Uncached model answer: {category, confidence, reason, model}. Raises LLMUnavailable on failure."""
    from fintnet.ai import llm

    direction = "unknown" if amount is None else ("money received" if amount > 0 else "money paid")
    user = f"Merchant: {merchant}\nStatement text: {remittance[:200]}\nDirection: {direction}"
    if provider() == "ollama":
        return _ollama_categorize(user)
    from fintnet.ai import prompts
    system, prompt = prompts.get("fintnet-categoriser-system")
    answer = llm.json_call(name, system=system, user=user, schema=SCHEMA, max_tokens=200, prompt=prompt)
    category = answer.get("category") if answer.get("category") in CATEGORIES else "Transfers / Other"
    return {"category": category, "confidence": max(0, min(100, int(answer.get("confidence", 0)))),
            "reason": str(answer.get("reason", ""))[:300], "model": llm.MODEL}


def _ollama_categorize(user: str) -> dict:
    """Local path kept for offline runs (Ollama + Qwen 2.5 3B)."""
    import json

    import ollama

    response = ollama.chat(model="qwen2.5:3b", format="json", options={"temperature": 0},
                           messages=[{"role": "system", "content": SYSTEM + '\nReply as JSON: {"category": ..., '
                                      '"confidence": ..., "reason": ...}'},
                                     {"role": "user", "content": user}])
    parsed = json.loads(response["message"]["content"])
    category = parsed.get("category") if parsed.get("category") in CATEGORIES else "Transfers / Other"
    return {"category": category, "confidence": int(parsed.get("confidence", 0)),
            "reason": str(parsed.get("reason", ""))[:300], "model": "qwen2.5:3b"}


def categorize_with_confidence(merchant: str) -> dict:
    """Kept for the explain UI and older scripts: model answer or a low-confidence rule answer."""
    from fintnet.ai import llm

    try:
        return model_categorize(merchant)
    except llm.LLMUnavailable as exc:
        return {"category": _categorize_by_rules(merchant), "confidence": 20,
                "reason": f"rules fallback ({exc})", "model": "rules"}


def upgrade_provisional(limit: int = 150) -> dict:
    """Send merchants whose FintNet transactions only have a rule category to the model.

    Each distinct normalised merchant costs at most one model call, then it is
    cached and every matching transaction is updated. Stops after `limit`
    model calls; the rest roll to the next run.
    """
    from fintnet.ai import llm

    if provider() == "rules":
        return {"skipped": "rules-only provider"}
    rows = db.session.query(Transaction).filter(Transaction.category_source == "rule").all()
    by_key: dict[str, list[Transaction]] = defaultdict(list)
    for t in rows:
        by_key[normalize(t.creditor_name or t.debtor_name or "")].append(t)
    by_key.pop("", None)

    called = cached_hits = updated = failed = 0
    for key, txns in by_key.items():
        hit = MerchantCategory.query.filter_by(merchant=key).first()
        if hit is None:
            if called >= limit:
                continue
            sample = txns[0]
            try:
                answer = model_categorize(sample.creditor_name or sample.debtor_name or "",
                                          sample.remittance_info or "", float(sample.amount or 0))
            except llm.LLMUnavailable as exc:
                failed += 1
                log.warning("categorize.model.failed", extra={"event": "categorize.model.failed",
                                                              "merchant": key, "error": str(exc)[:200]})
                if not llm.available():
                    break
                continue
            called += 1
            hit = MerchantCategory(merchant=key, category=answer["category"], source="model",
                                   confidence=answer["confidence"], reasoning=answer["reason"][:300],
                                   model=answer["model"])
            db.session.add(hit)
        else:
            cached_hits += 1
        for t in txns:
            t.category, t.category_source = hit.category, "model"
            updated += 1
        db.session.commit()
    return {"model_calls": called, "cache_hits": cached_hits, "transactions_updated": updated,
            "failed": failed, "remaining_merchants": max(0, len(by_key) - called - cached_hits)}
