"""
The Flask app — entry point for the bank-connectivity web UI.

Layout of this file (top to bottom):

  1. Imports + global constants.
  2. Pure analytics helpers: date-range parsing, month-over-month delta,
     recurring-payment detection, "wasted spend" signal generator.
  3. Flask + login setup, plus a one-shot schema migration block.
  4. DB-scoping helpers (`_acct_query`, etc.) that always restrict
     queries to the logged-in user.
  5. Connection bookkeeping: `_get_connection`, `_upsert_connection`,
     `_fetch_and_store` — these are bank-agnostic wrappers that the
     route handlers call after a successful OAuth/consent flow.
  6. Route handlers, grouped by area: home, auth, analytics
     (aggregation/dashboard/spending/recurring), the money-questions
     assistant (/ask), operations (/ops), one section per bank
     (UniCredit, Commerzbank, Nordea, ING, synthetic bank), and
     per-account detail views. Cron jobs live in cron.py and the
     synthetic bank's own API in synthbank/api.py.

Things this file deliberately does NOT do:
  * Talk to bank APIs directly — each bank lives in its own *_client.py.
  * Categorise transactions — categorize.py owns that.
  * Persist account/transaction rows — db_utils.py owns that.

Keep route handlers small. Anything more than ~30 lines of logic
belongs in a helper above so the routes stay readable.
"""
import os
import time
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

# .env must be loaded before any module that reads env vars at import time,
# and certificates must be materialised before any bank client reads its
# certificate paths (runtime_certs is a no-op without the *_B64 variables).
load_dotenv()
import runtime_certs  # noqa: E402,F401
import spl  # noqa: E402

import assistant  # noqa: E402
import auth  # noqa: E402
import categorize as cat  # noqa: E402
import commerzbank_client  # noqa: E402
import cron  # noqa: E402
import currency_utils  # noqa: E402
import db_utils  # noqa: E402
import eventlog  # noqa: E402
import ing_client  # noqa: E402
import llm  # noqa: E402
import nordea_client  # noqa: E402
import psd2_client  # noqa: E402
import synthbank_client  # noqa: E402
from logging_config import log  # noqa: E402
from models import Account, BankConnection, DismissedAlert, JobRun, Transaction, User, db  # noqa: E402
from synthbank import api as synthbank_api  # noqa: E402
from synthbank import store as synthbank_store  # noqa: E402

# Brand colour per bank. Used for chart bars, donut slices, and the
# coloured pill next to merchant names on the dashboard. Adding a new
# bank? Add it here too — `BANK_COLORS.get(bank, "#95a5a6")` falls back
# to grey, which looks fine but lazy.
BANK_COLORS = {
    "commerzbank":  "#e67e22",
    "nordea":       "#3498db",
    "unicredit":    "#c0392b",
    "ing":          "#FF6200",
    "synthbank":    "#6d28d9",
}

# Display names for the storage ids above; "ing".capitalize() would give "Ing".
BANK_NAMES = {
    "commerzbank": "Commerzbank",
    "nordea":      "Nordea",
    "unicredit":   "UniCredit",
    "ing":         "ING",
    "synthbank":   "Synthetic Bank",
}


def _bank_name(bank: str | None) -> str:
    return BANK_NAMES.get(bank or "", (bank or "").capitalize())

# Heuristic thresholds used by the analytics helpers. Tuning these
# changes which alerts the dashboard surfaces — keep them named so
# nobody has to guess what 0.08 means.
_FIXED_AMOUNT_CV_THRESHOLD = 0.08   # coeff. of variation below which we call a charge "fixed"
_PRICE_CREEP_PCT           = 5.0    # % rise from early to recent average that triggers a "price creep" alert
_BURDEN_PCT_FLAGGED        = 20.0   # % of income going to fixed costs that flips burden from info → warning
_LAPSE_WINDOW_DAYS         = 90     # window for adjacent-spend / lapse heuristics
_USUAL_AMOUNT_BAND         = 0.20   # charges within ±20% of the median count as the usual amount
_USUAL_AMOUNT_SHARE        = 0.75   # the usual amount must cover this share of charges to ignore the rest


def _parse_date_range(req, default: str = "month") -> tuple[date, date]:
    """Read a `from`/`to` date pair from query string args.

    Returns a `(date_from, date_to)` tuple. If the query strings are
    missing or malformed we fall back to a sensible default:
      * default="month" -> first of this month .. today  (dashboard)
      * default="3m"    -> 90 days ago        .. today  (spending)

    The two `try/except` blocks are independent so a malformed `from`
    doesn't lose a valid `to`.
    """
    today = date.today()
    try:
        date_from = date.fromisoformat(req.args.get("from", ""))
    except ValueError:
        date_from = date(today.year, today.month, 1) if default == "month" \
            else today - timedelta(days=89)
    try:
        date_to = date.fromisoformat(req.args.get("to", ""))
    except ValueError:
        date_to = today
    return date_from, date_to


def _mom_delta(current: float, prev: float) -> tuple[float | None, str]:
    """Month-over-month percentage change, plus a direction label.

    Returns:
        (None, "new")           — no comparison possible, prev period was 0
        (pct, "up" | "down")    — pct is positive for an increase

    Used to colour the little arrows next to dashboard KPIs.
    """
    if prev == 0:
        return None, "new"
    pct = round((current - prev) / abs(prev) * 100, 1)
    return pct, "up" if pct > 0 else "down"


def _txn_currency(t, a) -> str:
    """ISO currency of one transaction, falling back to its account, then EUR."""
    return t.currency or a.currency or "EUR"


def _money(amount, currency: str | None) -> str:
    """Format an amount with its currency: "€12.30" for EUR, "SEK 12.30" otherwise."""
    currency, value = currency or "EUR", float(amount)
    sign = "-" if value < 0 else ""
    return f"{sign}€{abs(value):.2f}" if currency == "EUR" else f"{sign}{currency} {abs(value):.2f}"


def _detect_recurring():
    """Identify recurring expenses, recurring income, and return all
    booked transactions in a single pass.

    Returns:
        (expenses, income, all_txns)

    `expenses` and `income` are lists of dicts (one per merchant+bank
    pair) sorted with the most-frequent / largest first. Each dict has
    `is_fixed` set to True if the amount barely varies — that's the
    signal we use elsewhere to call something a subscription rather
    than a noisy recurring charge like a utility bill.

    `all_txns` is exposed because the caller usually wants the raw
    transactions too and we'd rather not re-query the DB.
    """
    own = db_utils.own_ibans(current_user.id)
    all_txns = [
        (t, a) for t, a in
        db.session.query(Transaction, Account)
        .join(Account, Transaction.account_id == Account.id)
        .filter(Transaction.status == "booked",
                Account.user_id == current_user.id)
        .all()
        if not db_utils.is_internal(t, own)  # own-account transfers are neither expenses nor income
    ]

    rates = currency_utils.get_rates("EUR")

    def _grouped_by_sign(sign: int) -> list[dict]:
        # sign = -1 => only outflows (expenses)
        # sign = +1 => only inflows  (income)
        # Grouped per currency too, so an average never mixes SEK with EUR.
        by_merchant: dict[tuple[str, str, str], list] = defaultdict(list)
        for t, a in all_txns:
            if (float(t.amount) < 0) == (sign < 0):
                merchant = t.creditor_name or t.debtor_name or ""
                if merchant:
                    by_merchant[(merchant, a.bank, _txn_currency(t, a))].append((t, a))

        results = []
        for (merchant, bank, currency), txns in by_merchant.items():
            # A transaction is "recurring" only if it shows up in at
            # least two distinct calendar months — otherwise it's a
            # one-off that happened twice in the same month.
            months = {(t.booking_date.year, t.booking_date.month)
                      for t, _ in txns if t.booking_date}
            if len(months) < 2:
                continue

            amounts = [abs(float(t.amount)) for t, _ in txns]
            # A one-off extra charge at a subscription merchant (an in-app
            # purchase, a gift card) should not turn the monthly charge into a
            # "variable" one. When most charges sit near the median, judge the
            # amount on those charges only.
            usual_mid = median(amounts)
            usual = [a for a in amounts if abs(a - usual_mid) <= _USUAL_AMOUNT_BAND * usual_mid]
            if len(usual) >= _USUAL_AMOUNT_SHARE * len(amounts):
                amounts = usual
            avg = sum(amounts) / len(amounts)
            # Coefficient of variation = stdev / mean. Small CV means
            # the amount barely changes (Netflix), large CV means it
            # swings a lot (electricity). We use CV (not raw stdev) so
            # the threshold is dimensionless and works across currencies.
            cv = (sum((a - avg) ** 2 for a in amounts) / len(amounts)) ** .5 / avg \
                if avg > 0 else 0

            last_t = max((t for t, _ in txns if t.booking_date),
                         key=lambda t: t.booking_date)

            # Project the next likely charge: same day of month, one
            # month after the last seen charge. Roll December → January
            # and clip "31st" in a 30-day month to the 28th to avoid
            # ValueError.
            next_date = None
            if last_t.booking_date:
                y, m = last_t.booking_date.year, last_t.booking_date.month + 1
                if m > 12:
                    m, y = 1, y + 1
                try:
                    next_date = date(y, m, last_t.booking_date.day)
                except ValueError:
                    next_date = date(y, m, 28)

            results.append({
                "merchant":    merchant,
                "bank":        bank,
                "color":       BANK_COLORS.get(bank, "#95a5a6"),
                "category":    last_t.category or "Other",
                "avg_amount":  round(avg, 2),
                "usual_mid":   usual_mid,
                "currency":    currency,
                "avg_eur":     round(currency_utils.to_eur(avg, currency, rates), 2),
                "occurrences": len(txns),
                "months":      len(months),
                "is_fixed":    cv < _FIXED_AMOUNT_CV_THRESHOLD,
                "last_date":   last_t.booking_date,
                "next_date":   next_date,
            })

        # Most months seen first; ties broken by largest average.
        return sorted(results, key=lambda x: (-x["months"], -x["avg_eur"]))

    return _grouped_by_sign(-1), _grouped_by_sign(+1), all_txns


# Keyword lists used by the lapse heuristics. Lowercase, substring-matched.
_TRANSIT_KEYWORDS   = ["bvg", "hvv", "mvv", "vbb", "rnv", "vgn", "transit",
                       "monatsticket", "deutschlandticket"]
_RIDESHARE_KEYWORDS = ["uber", "taxi", "bolt", "free now", "freenow", "mytaxi"]
_GYM_KEYWORDS       = ["gym", "fitness", "sport", "mcfit", "planet fitness",
                       "urban sports", "holmes place"]
_INSURANCE_KEYWORDS = ["krankenkasse", "insurance", "versicherung", "tk ", "aok", "barmer"]

# Categories where having two of the same thing is a smell ("two streaming services").
_REDUNDANCY_CATEGORIES  = {"Entertainment", "Health & Fitness"}
# Categories prone to silent price hikes — these are the ones we sweep for "price creep".
_PRICE_CREEP_CATEGORIES = {"Entertainment", "Utilities", "Health", "Healthcare", "Health & Fitness"}


def _detect_waste(fixed, all_recurring, income, all_txns):
    """Generate "wasted spend" alerts the dashboard surfaces.

    Each dict in the returned list has a unique `key` so the user can
    dismiss alerts individually (see DismissedAlert table).

    Four families of signals, in order:
      1. Redundant   — two fixed-cost subs in the same redundancy category.
      2. Price creep — a fixed sub whose amount has climbed >5% comparing
                       the first two charges to the last two.
      3. Lapse       — heuristic guesses that a sub isn't being used:
                         a) transit pass + recent rideshare spend
                         b) gym membership + zero adjacent health spend in 90d
      4. Burden      — fixed costs as a percentage of average income.
    """
    signals: list[dict] = []
    today = date.today()
    cutoff_lapse = today - timedelta(days=_LAPSE_WINDOW_DAYS)

    # Per-merchant indexes built once and reused by every signal:
    #   merchant_charges  -> [(date, amount), ...] for outflows
    #   merchant_currency -> {currency: count} so we can pick a dominant currency to format in
    merchant_charges:  dict[str, list]  = defaultdict(list)
    merchant_currency: dict[str, dict]  = defaultdict(lambda: defaultdict(int))
    for t, a in all_txns:
        if float(t.amount) < 0 and t.booking_date:
            key = t.creditor_name or t.debtor_name or ""
            if key:
                merchant_charges[key].append((t.booking_date, abs(float(t.amount))))
                merchant_currency[key][t.currency or a.currency or "EUR"] += 1

    def _dominant_currency(merchant: str) -> str:
        """Most-common currency seen for this merchant, EUR if unknown."""
        counts = merchant_currency.get(merchant, {})
        return max(counts, key=counts.get) if counts else "EUR"

    def _fmt(amount: float, merchant: str) -> str:
        """Format an amount in the merchant's dominant currency."""
        cur = _dominant_currency(merchant)
        symbol = "€" if cur == "EUR" else cur + " "
        return f"{symbol}{amount:.2f}"

    # ── 1. Redundant category ────────────────────────────────────────────────
    # Same service on two bank accounts should count once: dedupe by
    # merchant name and keep the larger-amount row (slightly conservative).
    cat_groups: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in fixed:
        if r["category"] in _REDUNDANCY_CATEGORIES:
            name = r["merchant"]
            existing = cat_groups[r["category"]].get(name)
            if existing is None or r["avg_amount"] > existing["avg_amount"]:
                cat_groups[r["category"]][name] = r
    for category_name, by_name in cat_groups.items():
        items = list(by_name.values())
        if len(items) >= 2:
            same_cur = len({i["currency"] for i in items}) == 1
            total = round(sum(i["avg_amount"] if same_cur else i["avg_eur"] for i in items), 2)
            sorted_names = sorted(i["merchant"] for i in items)
            signals.append({
                "type": "redundant", "severity": "warning",
                "key": f"redundant:{category_name}:{','.join(sorted_names)}",
                "category": category_name,
                "services": [{"merchant": i["merchant"], "avg_amount": i["avg_amount"],
                               "avg_eur": i["avg_eur"], "fmt": _money(i["avg_amount"], i["currency"])}
                              for i in items],
                "total_monthly": total,
                "total_fmt": _money(total, items[0]["currency"] if same_cur else "EUR"),
                "message": f"Are you actually using all {len(items)}? You're paying for {', '.join(i['merchant'] for i in items)} every month.",
            })

    # ── 2. Price creep ───────────────────────────────────────────────────────
    # Compare the earliest charges with the latest; flag if the price stepped
    # up by more than _PRICE_CREEP_PCT. Need at least 4 charges.
    for r in fixed:
        if r["category"] not in _PRICE_CREEP_CATEGORIES:
            continue
        # Compare only charges near the usual amount, so a one-off extra
        # purchase does not read as a price rise.
        band = _USUAL_AMOUNT_BAND * r["usual_mid"]
        charges = sorted((c for c in merchant_charges.get(r["merchant"], []) if abs(c[1] - r["usual_mid"]) <= band),
                         key=lambda x: x[0])
        if len(charges) < 4:
            continue
        # Compare the earliest and latest 3 charges (2 when history is short).
        # A price rise is a step up: every recent charge above every early one.
        # Without that, a bill that varies by a few percent each month reads as a rise.
        n = 3 if len(charges) >= 6 else 2
        early, recent = [amt for _, amt in charges[:n]], [amt for _, amt in charges[-n:]]
        early_avg, recent_avg = mean(early), mean(recent)
        stepped_up = min(recent) > max(early)
        if early_avg > 0 and stepped_up:
            pct = (recent_avg - early_avg) / early_avg * 100
            if pct > _PRICE_CREEP_PCT:
                signals.append({
                    "type": "price_creep", "severity": "warning",
                    "key": f"price_creep:{r['merchant']}",
                    "merchant": r["merchant"],
                    "early_avg": round(early_avg, 2),
                    "recent_avg": round(recent_avg, 2),
                    "early_fmt": _fmt(early_avg, r["merchant"]),
                    "recent_fmt": _fmt(recent_avg, r["merchant"]),
                    "crept_eur": round(currency_utils.to_eur(recent_avg - early_avg, r["currency"],
                                                             currency_utils.get_rates("EUR")), 2),
                    "pct_increase": round(pct, 1),
                    "message": f"Did you notice {r['merchant']} raised their price? You were paying {_fmt(early_avg, r['merchant'])} — now it's {_fmt(recent_avg, r['merchant'])}.",
                })

    # ── 3a. Transit pass + rideshare overlap ─────────────────────────────────
    # If the user has held a transit pass for 3+ months but is also
    # taking lots of Ubers, one of those is almost certainly waste.
    transit_subs = [r for r in all_recurring
                    if any(kw in r["merchant"].lower() for kw in _TRANSIT_KEYWORDS)
                    and r["months"] >= 3]
    if transit_subs:
        rideshare_txns = [(t, a) for t, a in all_txns
                         if t.booking_date and t.booking_date >= cutoff_lapse
                         and float(t.amount) < 0
                         and any(kw in (t.creditor_name or t.debtor_name or "").lower()
                                 for kw in _RIDESHARE_KEYWORDS)]
        if len(rideshare_txns) >= 3:
            rideshare_total = round(sum(abs(float(t.amount)) for t, _ in rideshare_txns), 2)
            sub = transit_subs[0]
            signals.append({
                "type": "lapse", "severity": "info",
                "key": f"lapse:{sub['merchant']}:transit",
                "merchant": sub["merchant"],
                "reason": "transit_rideshare_overlap",
                "subscription_monthly": sub["avg_amount"],
                "subscription_monthly_eur": sub["avg_eur"],
                "conflicting_count": len(rideshare_txns),
                "conflicting_amount": rideshare_total,
                "window_days": _LAPSE_WINDOW_DAYS,
                "message": f"You have a {sub['merchant']} pass but took {len(rideshare_txns)} Uber or taxi rides recently. Are you still using the pass?",
            })

    # ── 3b. Gym membership with no adjacent health spend ─────────────────────
    # Insurance auto-debits don't count — they're not evidence the user
    # is actively engaged with health services.
    gym_subs = [r for r in fixed
                if r["category"] == "Health & Fitness"
                or any(kw in r["merchant"].lower() for kw in _GYM_KEYWORDS)]
    if gym_subs:
        health_recent = [t for t, a in all_txns
                         if t.booking_date and t.booking_date >= cutoff_lapse
                         and float(t.amount) < 0
                         and t.category in ("Health & Fitness", "Healthcare")
                         and not any(kw in (t.creditor_name or t.debtor_name or "").lower()
                                     for kw in _INSURANCE_KEYWORDS)]
        if len(health_recent) == 0:
            sub = gym_subs[0]
            signals.append({
                "type": "lapse", "severity": "info",
                "key": f"lapse:{sub['merchant']}:gym",
                "merchant": sub["merchant"],
                "reason": "gym_no_adjacent_spend",
                "subscription_monthly": sub["avg_amount"],
                "subscription_monthly_eur": sub["avg_eur"],
                "window_days": _LAPSE_WINDOW_DAYS,
                "message": f"Your {sub['merchant']} membership is still charging. When did you last go?",
            })

    # ── 4. Subscription burden ───────────────────────────────────────────────
    # Same dedup trick as the redundancy signal: a sub appearing on two
    # bank accounts counts once.
    # One currency across fixed charges and income: show it natively.
    # Mixed currencies: add them up in EUR, never SEK plus EUR.
    burden_currencies = {r["currency"] for r in fixed + income}
    native = len(burden_currencies) == 1
    amount_key = "avg_amount" if native else "avg_eur"
    unique_fixed  = {(r["merchant"], r["currency"]): r[amount_key] for r in fixed}
    unique_income = {(r["merchant"], r["currency"]): r[amount_key] for r in income}
    monthly_fixed_total = round(sum(unique_fixed.values()), 2)
    monthly_income_avg  = round(sum(unique_income.values()), 2)
    if monthly_income_avg > 0:
        pct = round(monthly_fixed_total / monthly_income_avg * 100, 1)
        inc_cur = next(iter(burden_currencies)) if native else "EUR"
        inc_sym = "€" if inc_cur == "EUR" else inc_cur + " "
        signals.append({
            "type": "burden",
            "severity": "warning" if pct > _BURDEN_PCT_FLAGGED else "info",
            "monthly_fixed": monthly_fixed_total,
            "monthly_income": monthly_income_avg,
            "monthly_fixed_fmt": f"{inc_sym}{monthly_fixed_total:.2f}",
            "monthly_income_fmt": f"{inc_sym}{monthly_income_avg:.2f}",
            "pct": pct,
            "flagged": pct > _BURDEN_PCT_FLAGGED,
            "message": f"{inc_sym}{monthly_fixed_total:.2f} leaves your account automatically every month. Does that feel right?",
        })

    return signals


def _total_potential_savings(signals):
    """Roll the waste signals into one number — "money you could save
    per month" — FintNet's North Star metric.

    Deliberately conservative so the headline figure is defensible:
      * redundant  → cancel all but the single most-expensive sub
      * price_creep → only the amount the price has crept up
      * lapse       → the full monthly cost of the likely-unused sub
      * burden      → excluded; it's a ratio, not a recoverable amount

    Amounts are converted to EUR before they are added up.

    Returns (total, breakdown) where breakdown maps signal type → € saved.
    """
    breakdown: dict[str, float] = defaultdict(float)
    for s in signals:
        t = s.get("type")
        if t == "redundant":
            amounts = [svc["avg_eur"] for svc in s.get("services", [])]
            if len(amounts) >= 2:
                # Keep the priciest, cancel the rest.
                breakdown["redundant"] += sum(amounts) - max(amounts)
        elif t == "price_creep":
            breakdown["price_creep"] += max(0.0, s.get("crept_eur", 0) or 0)
        elif t == "lapse":
            breakdown["lapse"] += s.get("subscription_monthly_eur", 0) or 0
    total = round(sum(breakdown.values()), 2)
    return total, {k: round(v, 2) for k, v in breakdown.items()}


# ── Flask app + DB + login setup ─────────────────────────────────────────────
# Each per-bank redirect URI is a distinct route because the OAuth /
# consent flows can't all reuse the same endpoint — they post different
# query parameters and we want one handler per bank to keep things
# clear. Defaults are localhost so a fresh checkout runs without env vars.
app = Flask(__name__)
# A fixed secret is required on Vercel: login and OAuth state cookies must be
# valid on every function instance, and synthetic bank consents are signed with it.
if os.getenv("VERCEL") and not os.getenv("FLASK_SECRET_KEY"):
    raise RuntimeError("FLASK_SECRET_KEY must be set on Vercel")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("VERCEL"))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SANDBOX_BASE_URL"]        = os.getenv("SANDBOX_BASE_URL",     "https://api-sandbox.unicredit.it")
app.config["REDIRECT_URI"]            = os.getenv("REDIRECT_URI",        "http://localhost:5000/callback")
app.config["CB_REDIRECT_URI"]         = os.getenv("CB_REDIRECT_URI",     "http://localhost:5000/commerzbank/callback")
app.config["NORDEA_REDIRECT_URI"]     = os.getenv("NORDEA_REDIRECT_URI", "http://localhost:5000/nordea/callback")
app.config["ING_REDIRECT_URI"]        = os.getenv("ING_REDIRECT_URI",    "http://localhost:5000/ing/callback")
# On Vercel (and other serverless hosts) the project directory is
# read-only — only /tmp is writable — so the SQLite file has to live
# there. Locally we keep the default instance-folder DB. DATABASE_URL
# overrides both.
_default_db = "sqlite:////tmp/ais.db" if os.getenv("VERCEL") else "sqlite:///ais.db"
_db_url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or _default_db
# Neon hands out postgres:// or postgresql:// URLs; SQLAlchemy needs the
# psycopg 3 driver named explicitly.
if _db_url.startswith(("postgres://", "postgresql://")):
    _db_url = "postgresql+psycopg://" + _db_url.split("://", 1)[1]
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Serverless Postgres drops idle connections; check before use.
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# True on the hosted app (Vercel). Live sandbox connections work there too;
# templates use it only to label the synthetic bank data honestly.
IS_HOSTED = bool(os.getenv("VERCEL"))

# The product and the internal view are the same app on two hostnames.
# OPS_HOST serves only the operations view; every other host serves only the
# product. Unset (local development) means one host serves both.
OPS_HOST = (os.getenv("OPS_HOST") or "").lower()
PRODUCT_URL = os.getenv("PRODUCT_URL", "https://fintnet.ai")
# Endpoints the operations host serves; everything else there goes to the product.
_OPS_ENDPOINTS = {"ops", "login", "logout", "signup", "static"}


def _on_ops_host() -> bool:
    return bool(OPS_HOST) and request.host.split(":")[0].lower() == OPS_HOST


@app.before_request
def _start_timer():
    request.environ["_started"] = time.time()


@app.after_request
def _log_request(response):
    """One event per HTTP request: the access log of the app."""
    endpoint = (request.endpoint or "").split(".")[0]
    if endpoint != "static":
        log.info("http.request", extra={
            "event": "http.request", "method": request.method, "path": request.path,
            "endpoint": request.endpoint, "status_code": response.status_code,
            "latency_ms": int((time.time() - request.environ.get("_started", time.time())) * 1000),
            "user_id": getattr(current_user, "id", None) if current_user else None,
            "host": request.host.split(":")[0], "view": "ops" if _on_ops_host() else "product",
        })
    return response


@app.before_request
def _split_product_and_ops():
    """Keep the two views apart: no operations page on the product host, and no
    product pages on the operations host."""
    endpoint = (request.endpoint or "").split(".")[0]
    if _on_ops_host():
        if endpoint == "index":            # the operations host opens on the operations view
            return redirect(url_for("ops"))
        if endpoint and endpoint not in _OPS_ENDPOINTS and not endpoint.startswith("cron"):
            return redirect(PRODUCT_URL)
    elif OPS_HOST and endpoint == "ops":
        abort(404)


app.jinja_env.filters["money"] = _money
app.jinja_env.filters["bank_name"] = _bank_name
app.jinja_env.tests["generated"] = lambda account: bool(account and (account.resource_id or "").startswith("SB-"))


@app.context_processor
def inject_flags():
    return {"is_hosted": IS_HOSTED, "assistant_enabled": llm.available(),
            "ops_view": _on_ops_host(), "product_url": PRODUCT_URL}


app.register_blueprint(cron.bp)
app.register_blueprint(synthbank_api.bp)


# ── One-shot schema init + migration (runs at import time) ───────────────────
# This deliberately runs at import time so a fresh checkout's first
# request has a working DB. The ALTER TABLE is a hand-rolled migration
# from before BankConnection.user_id existed; SQLite has no native
# migration framework, so the cheapest fix is "is the column there? if
# not, add it". If you migrate to Alembic later, this whole block
# should move into the migration scripts.
with app.app_context():
    db.create_all()
    # create_all never alters existing tables: add columns introduced later.
    from sqlalchemy import inspect as _inspect, text as _text
    for _table, _name, _ddl in (("sb_accounts", "bank", "VARCHAR(20)"),
                                ("sb_accounts", "role", "VARCHAR(10) NOT NULL DEFAULT 'main'"),
                                ("sb_transactions", "counterparty_iban", "VARCHAR(34)"),
                                ("transactions", "counterparty_iban", "VARCHAR(34)"),
                                ("users", "last_login_at", "TIMESTAMP")):
        if _name not in {c["name"] for c in _inspect(db.engine).get_columns(_table)}:
            with db.engine.begin() as _conn:
                _conn.execute(_text(f"ALTER TABLE {_table} ADD COLUMN {_name} {_ddl}"))


# ── DB-scoping helpers ───────────────────────────────────────────────────────
# Every query below filters by current_user.id so a logged-in user can
# never see another user's data. Routes should always start from one of
# these (or BankConnection.query) — never use bare Account.query, since
# that would return rows across all users.

def _acct_query():
    """Accounts owned by the currently logged-in user."""
    return Account.query.filter(Account.user_id == current_user.id)


def _txn_acct_query():
    """(Transaction, Account) pairs owned by the current user. Used
    when we need both the txn and its parent account in one go."""
    return (
        db.session.query(Transaction, Account)
        .join(Account, Transaction.account_id == Account.id)
        .filter(Account.user_id == current_user.id)
    )


def _get_connection(bank: str) -> BankConnection | None:
    """Return the active BankConnection for current_user + bank, or None."""
    return BankConnection.query.filter_by(
        user_id=current_user.id, bank=bank, status="active"
    ).first()


def _bank_evidence() -> dict[str, dict]:
    """Per bank, what the current user's connection actually is.

    The live sandbox rows carry the bank's API host and the consent or token the
    bank issued, so the Connect page can show that the connection is a real PSD2
    sandbox call and not generated data."""
    hosts = {"unicredit": urlparse(app.config["SANDBOX_BASE_URL"]).netloc,
             "commerzbank": urlparse(commerzbank_client.BASE_URL).netloc,
             "nordea": urlparse(nordea_client.BASE_URL).netloc,
             "ing": urlparse(ing_client.BASE_URL).netloc}
    out: dict[str, dict] = {}
    for bank, host in hosts.items():
        live = _get_connection(bank)
        gen = _get_connection(synthbank_store.GEN_PREFIX + bank)
        accounts = _acct_query().filter(Account.bank == bank).all()
        live_accounts = [a for a in accounts if not (a.resource_id or "").startswith("SB-")]
        gen_accounts = [a for a in accounts if (a.resource_id or "").startswith("SB-")]
        reference = (live.consent_id or live.access_token or "") if live else ""
        out[bank] = {
            "api_host": host,
            "live": bool(live),
            "generated": bool(gen),
            "reference": ("…" + reference[-8:]) if reference else "",
            "reference_kind": "consent" if (live and live.consent_id) else ("token" if live else ""),
            "live_accounts": len(live_accounts),
            "generated_accounts": len(gen_accounts),
            "last_sync": max([a.fetched_at for a in accounts if a.fetched_at], default=None),
        }
    return out


def _demo_profile() -> dict | None:
    """Name, home bank and banks with generated accounts for a demo login; None for sign-ups."""
    cust = _demo_customer()
    if cust is None:
        return None
    return {"name": cust.name, "home_bank": synthbank_store.home_bank(cust.customer_id),
            "banks": synthbank_store.generated_banks(cust.customer_id)}


def _demo_customer():
    """The generated-data customer behind a demo login, or None for sign-ups."""
    return synthbank_store.customer_for_email(current_user.email)


def _live_blocked(bank: str) -> bool:
    """A live sandbox holds only its own test user, so a demo login may use the
    live flow only at its home bank. Other banks go through /connect/<bank>."""
    cust = _demo_customer()
    return cust is not None and synthbank_store.home_bank(cust.customer_id) != bank


def _upsert_connection(bank: str, access_token: str | None = None,
                       consent_id: str | None = None) -> BankConnection:
    """Save (or refresh) a BankConnection, then immediately sync the bank.

    Each bank uses one of `access_token` or `consent_id`, never both.
    The caller passes whichever its OAuth/consent flow yielded.

    After persisting we trigger a fetch so the user sees their data on
    the very next request — `_fetch_and_store` is what actually hits
    the bank's API and writes accounts / transactions to the DB.
    """
    conn = BankConnection.query.filter_by(user_id=current_user.id, bank=bank).first()
    is_new = conn is None
    if conn:
        conn.access_token = access_token
        conn.consent_id   = consent_id
        conn.status       = "active"
    else:
        conn = BankConnection(
            user_id=current_user.id, bank=bank,
            access_token=access_token, consent_id=consent_id,
        )
        db.session.add(conn)
    db.session.commit()
    log.info("connection.upsert", extra={
        "event": "connection.upsert", "user_id": conn.user_id,
        "bank": bank, "is_new": is_new,
    })
    _fetch_and_store(bank, conn)
    # A demo login connecting its home bank live also gets its generated
    # accounts at that bank, so one connect shows the whole relationship.
    cust = _demo_customer() if bank in synthbank_store.LIVE_BANKS else None
    if cust is not None and bank in synthbank_store.generated_banks(cust.customer_id):
        consent = synthbank_client.create_consent(cust.customer_id, bank)
        _upsert_connection(synthbank_store.GEN_PREFIX + bank, consent_id=consent["consentId"])
    return conn


def _fetch_and_store(bank: str, conn: BankConnection) -> None:
    """Pull every account + every transaction from `bank` and upsert into the DB.

    The big if/elif chain here is intentional and not a dispatch dict,
    because each bank's call signature is slightly different (some
    take a token, some take a token + consent_id, etc). Wrapping that
    in a uniform interface would mean another layer of indirection
    that hides the per-bank quirks rather than documenting them.

    ING is the only bank where we tolerate a per-account failure
    mid-loop — its sandbox sometimes 403s on individual accounts —
    and skip just that one. For the others, an exception bubbles up
    so the caller's flash() shows the user what went wrong.
    """
    import time as _time
    t0 = _time.time()
    gen_bank = synthbank_store.generated_bank(bank)
    if gen_bank:
        # Generated accounts: read from the synthetic bank, stored under the real
        # bank's name so they add up with that bank's live sandbox accounts.
        saved = db_utils.upsert_accounts(gen_bank, synthbank_client.get_accounts(conn.consent_id), user_id=conn.user_id)
        for acc in saved:
            db_utils.upsert_transactions(gen_bank, acc.resource_id,
                                         synthbank_client.get_transactions(conn.consent_id, acc.resource_id),
                                         user_id=conn.user_id)
        log.info("sync.complete", extra={
            "event": "sync.complete", "user_id": conn.user_id, "bank": gen_bank, "data": "generated",
            "account_count": len(saved), "latency_ms": int((_time.time() - t0) * 1000),
        })
        return saved
    if bank == "nordea":
        account_list = nordea_client.get_accounts(conn.access_token)
    elif bank == "commerzbank":
        token = commerzbank_client.get_oauth_token()
        account_list = commerzbank_client.get_accounts(token, conn.consent_id)
    elif bank == "ing":
        account_list = ing_client.get_accounts(conn.access_token)
    else:  # unicredit
        account_list = psd2_client.get_accounts(app.config["SANDBOX_BASE_URL"], conn.consent_id)
        for a in account_list:
            if a.get("ownerName") or not a.get("resourceId"):
                continue
            try:
                details = psd2_client.get_account_details(app.config["SANDBOX_BASE_URL"], conn.consent_id, a["resourceId"])
                a["ownerName"] = details.get("ownerName", "")
            except psd2_client.PSD2ApiError as e:
                log.warning("sync.owner_name.skipped", extra={
                    "event": "sync.owner_name.skipped", "bank": bank, "account_id": a["resourceId"],
                    "status_code": e.status_code, "reason": str(e)[:200]})

    saved = db_utils.upsert_accounts(bank, account_list, user_id=conn.user_id)

    for acc in saved:
        if bank == "nordea":
            txn_data = nordea_client.get_transactions(conn.access_token, acc.resource_id)
        elif bank == "commerzbank":
            token = commerzbank_client.get_oauth_token()
            txn_data = commerzbank_client.get_transactions(token, conn.consent_id, acc.resource_id)
        elif bank == "ing":
            try:
                txn_data = ing_client.get_transactions(conn.access_token, acc.resource_id)
            except ing_client.INGApiError as e:
                log.warning("sync.account.skipped", extra={
                    "event": "sync.account.skipped", "user_id": conn.user_id,
                    "bank": bank, "account_id": acc.resource_id,
                    "status_code": e.status_code, "reason": str(e)[:200],
                })
                continue
        else:
            txn_data = psd2_client.get_transactions(
                app.config["SANDBOX_BASE_URL"], conn.consent_id, acc.resource_id)
        db_utils.upsert_transactions(bank, acc.resource_id, txn_data, user_id=conn.user_id)

    # Live sandbox data is small: let the model categorise a few new merchants
    # straight away. Generated accounts wait for the categorise cron job.
    if llm.available():
        try:
            cat.upgrade_provisional(limit=int(os.getenv("SYNC_CATEGORISE_LIMIT", "10")))
        except Exception as exc:  # noqa: BLE001 — categorisation must never break a sync
            db.session.rollback()
            log.warning("sync.categorise.failed", extra={"event": "sync.categorise.failed", "error": str(exc)[:200]})

    log.info("sync.complete", extra={
        "event": "sync.complete", "user_id": conn.user_id, "bank": bank, "data": "live sandbox",
        "account_count": len(saved), "latency_ms": int((_time.time() - t0) * 1000),
    })


# ── Home ─────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    """Home page: list of bank connections + dismissable "wasted spend" cards."""
    connections   = {c.bank: c for c in BankConnection.query.filter_by(user_id=current_user.id).all()}
    active_conns  = sum(1 for c in connections.values() if c.status == "active")
    account_count = _acct_query().count()

    # Two-step txn count avoids loading the full Account rows just to count their txns.
    account_ids = [a.id for a in _acct_query().with_entities(Account.id).all()]
    txn_count   = Transaction.query.filter(Transaction.account_id.in_(account_ids)).count() if account_ids else 0

    # Skip the analytics work for empty accounts — _detect_recurring is
    # cheap but pointless when there's nothing to analyse.
    waste = []
    potential_savings = 0.0
    savings_breakdown: dict[str, float] = {}
    if active_conns > 0 and txn_count > 0:
        expenses, income, all_txns = _detect_recurring()
        fixed       = [r for r in expenses if r["is_fixed"]]
        all_signals = _detect_waste(fixed, expenses, income, all_txns)
        dismissed   = {d.alert_key for d in
                       DismissedAlert.query.filter_by(user_id=current_user.id).all()}
        waste = [s for s in all_signals if s.get("key") not in dismissed]
        # North Star: compute from the *un-dismissed* signals so the
        # headline number always matches the alert cards on the page
        # (and shrinks the moment a user dismisses one).
        potential_savings, savings_breakdown = _total_potential_savings(waste)

    return render_template("index.html",
        connections=connections,
        active_conns=active_conns,
        account_count=account_count,
        txn_count=txn_count,
        waste=waste,
        potential_savings=potential_savings,
        savings_breakdown=savings_breakdown,
        sandbox_login=SANDBOX_LOGIN,
        demo=_demo_profile(),
        evidence=_bank_evidence(),
    )


@app.route("/dismiss-alert", methods=["POST"])
@login_required
def dismiss_alert():
    """Hide one of the dashboard alert cards. The card is re-shown next
    time the underlying signal regenerates with a different `key`
    (e.g. a new merchant joins the redundancy set)."""
    key = request.form.get("key", "").strip()
    if key:
        exists = DismissedAlert.query.filter_by(
            user_id=current_user.id, alert_key=key).first()
        if not exists:
            db.session.add(DismissedAlert(user_id=current_user.id, alert_key=key))
            db.session.commit()
    return redirect(url_for("index"))


@app.route("/disconnect/<bank>")
@login_required
def disconnect(bank):
    """Mark a bank connection as revoked. We deliberately do NOT delete
    accounts / transactions — historical analytics should keep working
    even after the user disconnects from the bank."""
    conns = BankConnection.query.filter(BankConnection.user_id == current_user.id,
                                        BankConnection.bank.in_([bank, synthbank_store.GEN_PREFIX + bank])).all()
    for conn in conns:
        conn.status = "revoked"
    if conns:
        db.session.commit()
        log.info("connection.disconnect", extra={
            "event": "connection.disconnect", "user_id": current_user.id, "bank": bank,
        })
    flash(f"Disconnected from {_bank_name(bank)}.", "info")
    return redirect(url_for("index"))


# ── Auth ─────────────────────────────────────────────────────────────────────

# Demo personas surfaced on the login page so anyone trying the hosted
# app can sign in with one click instead of hunting for credentials.
# Emails and the shared password must match what `seed_data.py` creates;
# `banks` is just a human-readable label for the card.
DEMO_PASSWORD = "TestPass123"
# Each persona is the test user that exists in that bank's PSD2 sandbox, so the
# sandbox SCA screen and the returned account owner match the login.
# `banks` lists where the persona holds accounts: the live sandbox bank first,
# then banks with generated accounts. `sandbox` tells the viewer what the bank's
# own sandbox asks for at the consent step.
DEMO_LOGIN = [
    {"name": "Thomas Mann",   "email": "thomas.mann@example.de",  "banks": "Commerzbank (live sandbox) · ING",
     "sandbox": "Commerzbank PSU-ID DE80480800200405423400 · consent pre-approved"},
    {"name": "Aino Salo",     "email": "aino.salo@example.fi",    "banks": "Nordea FI (live sandbox) · UniCredit",
     "sandbox": "Nordea FI · the sandbox approves the consent, no bank login"},
    {"name": "Margit Alros",  "email": "margit.alros@example.se", "banks": "Nordea SE (live sandbox, SEK) · Commerzbank · ING · UniCredit",
     "sandbox": "Nordea SE · choose SE, the sandbox approves the consent"},
    {"name": "A van Dijk",    "email": "a.vandijk@example.nl",    "banks": "ING NL (live sandbox) · Commerzbank",
     "sandbox": "ING · pick profile \"Hr A van Dijk, Mw B Mol-van Dijk\""},
    {"name": "Mario Rossi",   "email": "mario.rossi@example.it",  "banks": "UniCredit IT (live sandbox) · Commerzbank",
     "sandbox": "UniCredit bank login · ituser2bgk / pwituser2bgk"},
]

# What each bank's sandbox asks for at the consent step, shown on its Connect card.
# `values` are (label, value) pairs with a copy button; `note` is the step to take.
SANDBOX_LOGIN = {
    "unicredit":   {"values": [("Username", "ituser2bgk"), ("Password", "pwituser2bgk")],
                    "note": "Sign in on UniCredit's page, grant the consent, then press Proceed."},
    "commerzbank": {"values": [("PSU-ID", "DE80480800200405423400")],
                    "note": "The sandbox consent is pre-approved: press Authorize."},
    "nordea":      {"values": [],
                    "note": "No bank login: Nordea's sandbox approves the consent. Choose FI or SE."},
    "ing":         {"values": [("Profile", "Hr A van Dijk, Mw B Mol-van Dijk")],
                    "note": "Pick the profile on ING's page, then paste the code from the example.com address bar."},
}


@app.route("/login", methods=["GET", "POST"])
def login():
    """Email + password sign-in. Honours `?next=` so flask-login can
    bounce users back to wherever they were trying to go.

    Signing in while another account is open switches to the new one: the old
    session is dropped first, so nothing of one login reaches the next.
    """
    if current_user.is_authenticated and request.method == "GET" and _on_ops_host():
        return redirect(url_for("ops"))
    if current_user.is_authenticated and request.method == "GET":
        # Show the form instead of bouncing home, so a viewer can switch persona.
        return render_template("login.html", demo_users=[] if _on_ops_host() else DEMO_LOGIN,
                               demo_password=DEMO_PASSWORD, signed_in_as=current_user.email)
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            previous = current_user.email if current_user.is_authenticated else None
            logout_user()
            session.clear()     # never carry one account's session into another
            login_user(user)
            user.last_login_at = datetime.now(timezone.utc)
            db.session.commit()
            log.info("auth.login.success", extra={"event": "auth.login.success",
                                                  "user_id": user.id, "email": email,
                                                  "switched_from": previous})
            # On the operations host the product pages are elsewhere: land on the view itself.
            default = url_for("ops") if _on_ops_host() else url_for("index")
            return redirect(request.args.get("next") or default)
        # Don't tell the attacker which half was wrong.
        log.warning("auth.login.failed", extra={"event": "auth.login.failed", "email": email})
        flash("Invalid email or password.", "error")
    return render_template("login.html", demo_users=[] if _on_ops_host() else DEMO_LOGIN,
                           demo_password=DEMO_PASSWORD,
                           signed_in_as=current_user.email if current_user.is_authenticated else None)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Create a new account. Validation cascade: presence -> match ->
    minimum length -> uniqueness. We log the user in immediately on
    success so the new dashboard appears without a second redirect."""
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email     = request.form.get("email", "").strip().lower()
        password  = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if not email or not password:
            flash("Email and password are required.", "error")
        elif password != password2:
            flash("Passwords do not match.", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
        else:
            # `generate_password_hash` uses a strong default (pbkdf2:sha256
            # at the time of writing); never store plaintext.
            user = User(
                email=email,
                password_hash=generate_password_hash(password),
                role="user",
            )
            db.session.add(user)
            db.session.commit()
            login_user(user)
            log.info("auth.signup", extra={"event": "auth.signup",
                                           "user_id": user.id, "email": email})
            flash("Account created. Welcome!", "success")
            return redirect(url_for("index"))
    return render_template("signup.html")


@app.route("/logout")
@login_required
def logout():
    """Clear both the session and flask-login's user_id cookie.
    `session.clear()` alone leaves flask-login state intact."""
    log.info("auth.logout", extra={"event": "auth.logout", "user_id": current_user.id})
    session.clear()
    logout_user()
    return redirect(url_for("login"))


# ── Analytics ────────────────────────────────────────────────────────────────

@app.route("/aggregation")
@login_required
def aggregation():
    """Per-account list across all banks, with an EUR-converted total.

    The view shows balances in each account's native currency AND in
    EUR side-by-side, plus a donut chart of "money by bank". Foreign
    currency balances are converted via currency_utils (live ECB rates
    cached for an hour) so the EUR total is comparable.
    """
    accounts = _acct_query().order_by(Account.bank, Account.id).all()
    rates    = currency_utils.get_rates("EUR")

    rows: list[dict] = []
    bank_totals_eur: dict[str, float] = defaultdict(float)
    currency_totals: dict[str, float] = defaultdict(float)  # in EUR, keyed by source currency

    for acc in accounts:
        currency    = acc.currency or "EUR"
        balance_nat = sum(float(t.amount) for t in acc.transactions)
        balance_eur = currency_utils.to_eur(balance_nat, currency, rates)
        last_txn    = max((t.booking_date for t in acc.transactions if t.booking_date), default=None)
        is_foreign  = currency != "EUR"
        rows.append({
            "account":     acc,
            "currency":    currency,
            "balance_nat": round(balance_nat, 2),
            "balance_eur": round(balance_eur, 2),
            "is_foreign":  is_foreign,
            "flag":        currency_utils.CURRENCY_FLAGS.get(currency, ""),
            "txn_count":   len(acc.transactions),
            "last_txn":    last_txn,
            "color":       BANK_COLORS.get(acc.bank, "#95a5a6"),
        })
        bank_totals_eur[acc.bank] += balance_eur
        currency_totals[currency] += balance_eur

    total_balance_eur = round(sum(r["balance_eur"] for r in rows), 2)

    bank_summary = [
        {"bank": b, "total": round(t, 2), "color": BANK_COLORS.get(b, "#95a5a6")}
        for b, t in sorted(bank_totals_eur.items(), key=lambda x: -x[1])
    ]

    currency_summary = [
        {"currency": c, "total_eur": round(v, 2),
         "flag": currency_utils.CURRENCY_FLAGS.get(c, ""),
         "pct": round(v / total_balance_eur * 100, 1) if total_balance_eur else 0}
        for c, v in sorted(currency_totals.items(), key=lambda x: -x[1])
    ]
    multi_currency = len(currency_totals) > 1

    chart_labels = [
        f"{r['account'].owner_name or r['account'].iban} ({r['account'].bank.upper()})"
        for r in rows
    ]
    chart_values = [r["balance_eur"] for r in rows]
    chart_colors = [r["color"] for r in rows]

    return render_template("aggregation.html",
        rows=rows, bank_summary=bank_summary, total_balance=total_balance_eur,
        currency_summary=currency_summary, multi_currency=multi_currency,
        rates_date=currency_utils.rates_updated_at(),
        chart_labels=chart_labels, chart_values=chart_values, chart_colors=chart_colors,
        account_count=len(rows),
    )


@app.route("/dashboard")
@login_required
def dashboard():
    """Headline KPIs for the current period (default: this month) plus
    month-over-month deltas, six-month bar chart, top merchants, and
    recent transactions.

    The previous-period window is the same length as the current
    period and ends the day before — so "this month" compares to a
    same-length window ending the last day of last month, not "last
    calendar month" specifically.
    """
    date_from, date_to = _parse_date_range(request, default="month")
    period_days = (date_to - date_from).days + 1
    prev_to     = date_from - timedelta(days=1)
    prev_from   = prev_to   - timedelta(days=period_days - 1)

    # Pull every booked txn once; we slice it locally for the two
    # windows. Cheaper than two SQL queries on the typical row count.
    booked    = _txn_acct_query().filter(Transaction.status == "booked").all()
    own_ibans = db_utils.own_ibans(current_user.id)
    # Transfers between the user's own accounts stay out of every total below;
    # the recent-transactions list still shows them, tagged.
    all_rows  = [(t, a) for t, a in booked if not db_utils.is_internal(t, own_ibans)]
    all_banks = sorted(set(a.bank for _, a in all_rows))
    rates     = currency_utils.get_rates("EUR")

    def eur(t, a) -> float:
        """The transaction amount in EUR, so SEK and EUR accounts add up."""
        return currency_utils.to_eur(float(t.amount), _txn_currency(t, a), rates)

    def _in(t, d0, d1) -> bool:
        return t.booking_date and d0 <= t.booking_date <= d1

    period_rows = [(t, a) for t, a in all_rows if _in(t, date_from, date_to)]
    prev_rows   = [(t, a) for t, a in all_rows if _in(t, prev_from, prev_to)]

    def _totals(rows) -> tuple[float, float]:
        """(spent, income) — both returned as positive floats."""
        spent  = abs(sum(eur(t, a) for t, a in rows if t.amount < 0))
        income = sum(eur(t, a) for t, a in rows if t.amount > 0)
        return spent, income

    total_spent,  total_income  = _totals(period_rows)
    prev_spent,   prev_income   = _totals(prev_rows)
    net = total_income - total_spent

    delta_spent  = _mom_delta(total_spent,  prev_spent)
    delta_income = _mom_delta(total_income, prev_income)
    delta_net    = _mom_delta(total_income - total_spent, prev_income - prev_spent)

    bank_period = defaultdict(lambda: {"spent": 0.0, "income": 0.0})
    for t, a in period_rows:
        if t.amount < 0:
            bank_period[a.bank]["spent"] += abs(eur(t, a))
        else:
            bank_period[a.bank]["income"] += eur(t, a)
    bank_month_summary = [
        {"bank": b, "spent": round(v["spent"], 2), "income": round(v["income"], 2),
         "color": BANK_COLORS.get(b, "#95a5a6")}
        for b, v in sorted(bank_period.items(), key=lambda x: -x[1]["spent"])
    ]

    cat_totals = defaultdict(float)
    for t, a in period_rows:
        if t.amount < 0:
            cat_totals[t.category or "Other"] += abs(eur(t, a))
    cat_sorted = sorted(cat_totals.items(), key=lambda x: -x[1])

    bank_spent_period = sorted(
        [(b, round(v["spent"], 2)) for b, v in bank_period.items() if v["spent"] > 0],
        key=lambda x: -x[1]
    )

    # Build a list of the last 6 (year, month) pairs ending with the
    # current month. Inserting at position 0 keeps them in chronological
    # order without an extra reverse() at the end.
    today = date.today()
    months: list[tuple[int, int]] = []
    y, m = today.year, today.month
    for _ in range(6):
        months.insert(0, (y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    month_labels = [date(y, m, 1).strftime("%b %Y") for y, m in months]
    # Stacked bar chart: one dataset per bank, six bars each. Note the
    # nested filtering — done in Python because months is a small list
    # and a SQL group-by would need bank-by-month aggregations.
    monthly_by_bank = [{
        "label": _bank_name(bank),
        "data": [round(abs(sum(
            eur(t, a) for t, a in all_rows
            if a.bank == bank and t.booking_date
            and t.booking_date.year == y and t.booking_date.month == m
            and t.amount < 0
        )), 2) for y, m in months],
        "backgroundColor": BANK_COLORS.get(bank, "#95a5a6") + "cc",
        "borderColor": BANK_COLORS.get(bank, "#95a5a6"),
        "borderWidth": 1, "stack": "expenses",
    } for bank in all_banks]

    merchant_key = defaultdict(lambda: {"total": 0.0, "bank": ""})
    for t, a in period_rows:
        if t.amount < 0:
            k = (t.creditor_name or t.debtor_name or "Unknown", a.bank)
            merchant_key[k]["total"] += abs(eur(t, a))
            merchant_key[k]["bank"] = a.bank
    top_merchants = sorted(
        [{"name": k[0], "bank": k[1], "color": BANK_COLORS.get(k[1], "#95a5a6"),
          "total": round(v["total"], 2)} for k, v in merchant_key.items()],
        key=lambda x: -x["total"]
    )[:10]

    recent = sorted(
        [(t, a) for t, a in booked if t.booking_date],
        key=lambda x: x[0].booking_date, reverse=True
    )[:15]

    return render_template("dashboard.html",
        date_from=date_from, date_to=date_to,
        period_label=f"{date_from.strftime('%d %b')} – {date_to.strftime('%d %b %Y')}",
        total_spent=round(total_spent, 2), total_income=round(total_income, 2), net=round(net, 2),
        delta_spent=delta_spent, delta_income=delta_income, delta_net=delta_net,
        account_count=_acct_query().count(), bank_count=len(all_banks),
        bank_month_summary=bank_month_summary,
        cat_labels=[c for c, _ in cat_sorted], cat_values=[round(v, 2) for _, v in cat_sorted],
        bank_donut_labels=[_bank_name(b) for b, _ in bank_spent_period],
        bank_donut_values=[v for _, v in bank_spent_period],
        bank_donut_colors=[BANK_COLORS.get(b, "#95a5a6") for b, _ in bank_spent_period],
        month_labels=month_labels, monthly_by_bank=monthly_by_bank,
        top_merchants=top_merchants, recent=recent, own_ibans=own_ibans,
    )


@app.route("/spending")
@login_required
def spending():
    """Spending breakdown by category (default window: last 90 days).

    Unlike /dashboard, this view only looks at outflows — every chart
    and table is "where did the money go". The previous-period window
    is the same length and immediately preceding, so we can show a MoM
    delta per category.
    """
    date_from, date_to = _parse_date_range(request, default="3m")
    period_days = (date_to - date_from).days + 1
    prev_to     = date_from - timedelta(days=1)
    prev_from   = prev_to   - timedelta(days=period_days - 1)

    own = db_utils.own_ibans(current_user.id)

    def _fetch(d0, d1):
        """Outflow transactions in [d0, d1], joined with their account, without own-account transfers."""
        rows = (
            _txn_acct_query()
            .filter(Transaction.amount < 0,
                    Transaction.booking_date >= d0,
                    Transaction.booking_date <= d1)
            .order_by(Transaction.booking_date.desc())
            .all()
        )
        return [(t, a) for t, a in rows if not db_utils.is_internal(t, own)]

    rows      = _fetch(date_from, date_to)
    prev_rows = _fetch(prev_from, prev_to)
    all_banks = sorted(set(a.bank for _, a in rows))
    rates     = currency_utils.get_rates("EUR")

    def eur(t, a) -> float:
        return currency_utils.to_eur(float(t.amount), _txn_currency(t, a), rates)

    totals      = defaultdict(float)
    by_category = defaultdict(list)
    cat_by_bank = defaultdict(lambda: defaultdict(float))
    prev_totals = defaultdict(float)

    for txn, acc in rows:
        totals[txn.category]                += eur(txn, acc)
        by_category[txn.category].append((txn, acc))
        cat_by_bank[txn.category][acc.bank] += abs(eur(txn, acc))
    for txn, acc in prev_rows:
        prev_totals[txn.category] += abs(eur(txn, acc))

    sorted_totals = sorted(totals.items(), key=lambda x: x[1])
    categories    = [c for c, _ in sorted_totals]

    grouped_datasets = [{
        "label": _bank_name(b),
        "data": [round(cat_by_bank[c].get(b, 0), 2) for c in categories],
        "backgroundColor": BANK_COLORS.get(b, "#95a5a6") + "cc",
        "borderColor": BANK_COLORS.get(b, "#95a5a6"),
        "borderWidth": 1, "borderRadius": 3,
    } for b in all_banks]

    cat_bank_rows = {c: [(b, round(cat_by_bank[c].get(b, 0), 2)) for b in all_banks] for c in categories}
    cat_deltas    = {c: _mom_delta(abs(totals[c]), prev_totals.get(c, 0)) for c in categories}
    bank_totals   = {b: round(sum(cat_by_bank[c].get(b, 0) for c in categories), 2) for b in all_banks}
    grand_total   = round(sum(bank_totals.values()), 2)
    txn_count     = sum(len(v) for v in by_category.values())

    return render_template("spending.html",
        date_from=date_from, date_to=date_to,
        period_label=f"{date_from.strftime('%d %b')} – {date_to.strftime('%d %b %Y')}",
        totals=sorted_totals, by_category=by_category,
        all_banks=all_banks, categories=categories,
        grouped_datasets=grouped_datasets, cat_bank_rows=cat_bank_rows,
        cat_deltas=cat_deltas, bank_totals=bank_totals,
        grand_total=grand_total, bank_colors=BANK_COLORS, txn_count=txn_count,
    )


@app.route("/recurring")
@login_required
def recurring():
    """List of recurring expenses (split into fixed/variable) and
    recurring income, plus the same waste signals shown on /index.
    See `_detect_recurring` for the definition of "recurring"."""
    expenses, income, all_txns = _detect_recurring()
    fixed    = [r for r in expenses if r["is_fixed"]]
    variable = [r for r in expenses if not r["is_fixed"]]
    waste    = _detect_waste(fixed, expenses, income, all_txns)
    return render_template("recurring.html",
        fixed=fixed, variable=variable, income=income, waste=waste,
        monthly_fixed=round(sum(r["avg_eur"] for r in fixed), 2),
        monthly_all=round(sum(r["avg_eur"] for r in expenses), 2),
        monthly_income=round(sum(r["avg_eur"] for r in income), 2),
        bank_colors=BANK_COLORS,
    )


# ── UniCredit (mTLS + consent SCA) ───────────────────────────────────────────

@app.route("/unicredit/connect")
@login_required
def unicredit_connect():
    """Step 1 of the UniCredit flow: ask the bank to create a consent
    and redirect the user to its SCA (Strong Customer Authentication)
    page. Comes back to /callback when the user finishes."""
    if _live_blocked("unicredit"):
        return redirect(url_for("connect_bank", bank="unicredit"))
    try:
        sca_url = auth.initiate_consent_flow()
        return redirect(sca_url)
    except psd2_client.PSD2ApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/callback")
@login_required
def callback():
    """Step 2 of UniCredit: the user has finished SCA in the bank's
    UI. We re-check consent status — only "valid" means we can fetch
    data — and persist the connection."""
    if _live_blocked("unicredit"):
        return redirect(url_for("connect_bank", bank="unicredit"))
    try:
        status = auth.check_and_store_consent_status()
        if status == "valid":
            _upsert_connection("unicredit", consent_id=session.pop("consent_id", None))
            flash("UniCredit connected. Accounts fetched.", "success")
            return redirect(url_for("dashboard"))
        flash(f"Consent not yet valid (status: {status}). Complete SCA and try again.", "warning")
        return render_template("consent_pending.html", status=status)
    except psd2_client.PSD2ApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


# ── Commerzbank (OAuth + consent SCA) ────────────────────────────────────────

@app.route("/commerzbank/connect")
@login_required
def commerzbank_connect():
    """Show the Commerzbank consent form. We validate the OAuth client
    credentials up front so an obvious "wrong client_id" error
    surfaces here rather than after the user submits the consent form."""
    if _live_blocked("commerzbank"):
        return redirect(url_for("connect_bank", bank="commerzbank"))
    try:
        commerzbank_client.get_oauth_token()
        return render_template("cb_consent.html", consent_id=commerzbank_client.SANDBOX_CONSENT)
    except commerzbank_client.CommerzbankApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/commerzbank/authorize", methods=["POST"])
@login_required
def commerzbank_authorize():
    """User submitted the consent form. Re-fetch the consent status
    from the bank — only "valid" means SCA is complete and we can
    start pulling data."""
    if _live_blocked("commerzbank"):
        return redirect(url_for("connect_bank", bank="commerzbank"))
    consent_id = request.form.get("consent_id")
    if not consent_id:
        flash("Missing consent ID.", "error")
        return redirect(url_for("index"))
    try:
        token  = commerzbank_client.get_oauth_token()
        status = commerzbank_client.get_consent_status(token, consent_id)
        if status != "valid":
            flash(f"Consent not valid (status: {status}).", "warning")
            return render_template("consent_pending.html", status=status)
        _upsert_connection("commerzbank", consent_id=consent_id)
        flash("Commerzbank connected. Accounts fetched.", "success")
        return redirect(url_for("dashboard"))
    except commerzbank_client.CommerzbankApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


# ── Nordea (OAuth authorization_code + SCA redirect) ─────────────────────────

@app.route("/nordea/connect")
@login_required
def nordea_connect():
    """Show the Nordea country picker. The actual OAuth dance starts
    when the user posts to /nordea/authorize."""
    if _live_blocked("nordea"):
        return redirect(url_for("connect_bank", bank="nordea"))
    default = "SE" if (current_user.email or "").endswith(".se") else nordea_client.COUNTRY
    return render_template("nordea_consent.html", country=default)


@app.route("/nordea/authorize", methods=["POST"])
@login_required
def nordea_authorize():
    """Kick off Nordea's OAuth authorization_code flow.

    The sandbox skips the SCA UI entirely: `initiate_authorize`
    returns a redirect Location that already contains `?code=…`, so we
    can exchange it for a token immediately. In production the same
    Location points to Nordea's hosted SCA page and we let the browser
    follow it — they'll come back to /nordea/callback.
    """
    if _live_blocked("nordea"):
        return redirect(url_for("connect_bank", bank="nordea"))
    try:
        redirect_uri = app.config["NORDEA_REDIRECT_URI"]
        country = request.form.get("country") if request.form.get("country") in ("FI", "SE", "DK", "NO") else None
        location, state = nordea_client.initiate_authorize(redirect_uri, country)
        session["nordea_state"] = state
        params = parse_qs(urlparse(location).query)
        if "code" in params:
            token = nordea_client.exchange_code(params["code"][0], redirect_uri)
            _upsert_connection("nordea", access_token=token)
            flash("Nordea connected. Accounts fetched.", "success")
            return redirect(url_for("dashboard"))
        return redirect(location)
    except nordea_client.NordeaApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/nordea/callback")
@login_required
def nordea_callback():
    """Nordea's hosted SCA page redirects here with `?code=…` once the
    user authenticates. If `code` is missing we render a small form
    that lets the user paste it manually (useful in dev when the
    redirect target is unreachable)."""
    if _live_blocked("nordea"):
        return redirect(url_for("connect_bank", bank="nordea"))
    code = request.args.get("code")
    if not code:
        return render_template("nordea_code.html", sca_url=None)
    try:
        redirect_uri = app.config["NORDEA_REDIRECT_URI"]
        token = nordea_client.exchange_code(code, redirect_uri)
        session.pop("nordea_state", None)
        _upsert_connection("nordea", access_token=token)
        flash("Nordea connected. Accounts fetched.", "success")
        return redirect(url_for("dashboard"))
    except nordea_client.NordeaApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


# ── ING (mTLS + HTTP Signatures + OAuth2 authorization_code) ─────────────────

@app.route("/ing/connect")
@login_required
def ing_connect():
    """Start ING's OAuth dance. Validates the app-level token early so
    a misconfigured ING_CLIENT_ID surfaces here rather than after the
    user is bounced to ING's auth URL."""
    if _live_blocked("ing"):
        return redirect(url_for("connect_bank", bank="ing"))
    try:
        ing_client.get_app_token()
        state = str(uuid.uuid4())  # CSRF guard for the OAuth round-trip
        session["ing_state"] = state
        auth_url = ing_client.get_authorization_url(state)
        return redirect(auth_url)
    except ing_client.INGApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/ing/enter-code", methods=["GET", "POST"])
@login_required
def ing_enter_code():
    """ING's sandbox redirects to example.com (not back to us), so the
    user has to copy the authorization code out of their browser bar
    and paste it into this form. We accept either the bare code or the
    full URL — we'll parse the `code=` parameter out either way.
    """
    if _live_blocked("ing"):
        return redirect(url_for("connect_bank", bank="ing"))
    if request.method == "POST":
        raw = request.form.get("code", "").strip()
        if "code=" in raw:
            qs = urlparse(raw).query if raw.startswith("http") else raw
            parsed = parse_qs(qs)
            code = (parsed.get("code") or [""])[0]
        else:
            code = raw
        if not code:
            flash("Please paste the authorization code.", "error")
            return render_template("ing_code.html")
        try:
            customer_token = ing_client.exchange_code(code)
            session.pop("ing_state", None)
            _upsert_connection("ing", access_token=customer_token)
            flash("ING connected. Accounts fetched.", "success")
            return redirect(url_for("dashboard"))
        except ing_client.INGApiError as e:
            flash(str(e), "error")
            return render_template("ing_code.html")
    return render_template("ing_code.html")


# ── Synthetic bank (Berlin Group AIS test bank, simulated SCA) ───────────────

_LIVE_CONNECT = {"unicredit": "unicredit_connect", "commerzbank": "commerzbank_connect",
                 "nordea": "nordea_connect", "ing": "ing_connect"}


@app.route("/connect/<bank>")
@login_required
def connect_bank(bank):
    """Connect one of the 4 banks.

    Sign-ups use the bank's live sandbox flow. A demo login uses the live flow
    at its home bank (which also brings its generated accounts there); at any
    other bank it goes to that bank's generated sign-in, which lists its
    generated accounts or fails the login when it holds none there.
    """
    if bank not in _LIVE_CONNECT:
        abort(404)
    cust = _demo_customer()
    if cust is None or synthbank_store.home_bank(cust.customer_id) == bank:
        return redirect(url_for(_LIVE_CONNECT[bank]))
    consent = synthbank_client.create_consent(cust.customer_id, bank)
    log.info("consent.created", extra={"event": "consent.created", "bank": bank, "data": "generated",
                                       "user_id": current_user.id, "customer_id": cust.customer_id})
    return redirect(consent["_links"]["scaRedirect"]["href"])


@app.route("/synthbank/callback", methods=["POST"])
@login_required
def synthbank_callback():
    """The user approved access to their generated accounts at one bank."""
    consent_id = request.form.get("consent_id", "")
    try:
        bank = synthbank_client.bank_for(consent_id)
        if request.form.get("decision") != "approve":
            log.info("consent.refused", extra={"event": "consent.refused", "bank": bank,
                                               "data": "generated", "user_id": current_user.id})
            flash("Consent refused. Nothing was connected.", "info")
            return redirect(url_for("index"))
        cust = _demo_customer()
        if cust is None or bank is None or cust.customer_id != synthbank_client.customer_id_for(consent_id):
            flash("That consent belongs to a different customer.", "error")
            return redirect(url_for("index"))
        _upsert_connection(synthbank_store.GEN_PREFIX + bank, consent_id=consent_id)
        flash(f"{_bank_name(bank)} connected. Generated test accounts fetched.", "success")
        return redirect(url_for("dashboard"))
    except synthbank_client.SynthBankError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


# ── Money questions assistant ────────────────────────────────────────────────

def _recurring_summary() -> dict:
    """Recurring payments and wasted-spend signals for the assistant's tool."""
    expenses, income, all_txns = _detect_recurring()
    fixed = [r for r in expenses if r["is_fixed"]]
    signals = _detect_waste(fixed, expenses, income, all_txns)

    def slim(r):
        return {"merchant": r["merchant"], "bank": r["bank"], "category": r["category"],
                "avg_amount": r["avg_amount"], "currency": r["currency"], "avg_eur": r["avg_eur"],
                "months_seen": r["months"],
                "last_date": str(r["last_date"]) if r["last_date"] else None,
                "next_expected": str(r["next_date"]) if r["next_date"] else None}

    return {"fixed_subscriptions": [slim(r) for r in fixed][:25],
            "variable_recurring": [slim(r) for r in expenses if not r["is_fixed"]][:25],
            "recurring_income": [slim(r) for r in income][:10],
            "signals": [{k: v for k, v in s.items() if k in ("type", "merchant", "message", "early_avg",
                                                              "recent_avg", "pct_increase", "total_monthly",
                                                              "monthly_fixed", "monthly_income", "pct")}
                        for s in signals],
            "note": "avg_amount is in the currency named on each row; avg_eur is the EUR equivalent"}


@app.route("/ask", methods=["GET", "POST"])
@login_required
def ask():
    """Ask a question about money across every connected bank."""
    result = None
    question = ""
    if request.method == "POST":
        question = request.form.get("question", "").strip()
        asked_at = time.time()
        result = assistant.answer(question, current_user.id, _recurring_summary)
        log.info("assistant.answered", extra={
            "event": "assistant.refused" if result.get("refused") else "assistant.answered",
            "user_id": current_user.id, "question": question[:200], "refused": bool(result.get("refused")),
            "tool_calls": len(result.get("tools") or []),
            "tools": ",".join(t["name"] for t in (result.get("tools") or [])),
            "model": result.get("model"), "trace_id": result.get("trace_id"),
            "latency_ms": int((time.time() - asked_at) * 1000),
        })
        history = session.get("ask_history", [])
        history.insert(0, {"q": question[:200], "a": (result.get("answer") or "")[:600]})
        session["ask_history"] = history[:4]
    examples = [
        "How much did I spend on groceries last month across all my banks?",
        "Why did my spending change last month compared with the month before?",
        "Which subscriptions went up in price?",
        "What were my top 5 merchants in the last 3 months?",
        "How much income came in each month this year?",
    ]
    return render_template("ask.html", result=result, question=question, examples=examples,
                           history=session.get("ask_history", [])[1 if result else 0:],
                           model=llm.MODEL, usage=llm.usage())


# ── Operations ───────────────────────────────────────────────────────────────

_EVENT_WINDOWS = {"15m": 900, "1h": 3600, "24h": 86400, "7d": 604800}
_SPL_EXAMPLES = [
    ("Bank API calls", "event=bank.api.call | table time, bank, method, host, path, status_code, latency_ms | sort -time"),
    ("Calls per bank", "event=bank.api.call | stats count, avg(latency_ms) as avg_ms by bank, host"),
    ("Bank syncs", "event=sync.complete | table time, bank, data, account_count, latency_ms | sort -time"),
    ("Sign-ins", "event=auth.* | table time, event, email, switched_from"),
    ("Consents", "event=consent.* OR event=bank.signin.* | table time, event, bank, customer"),
    ("Assistant", "event=assistant.* | table time, event, question, tool_calls, latency_ms"),
    ("Assistant tools", "sourcetype=fintnet:tool | stats count by name"),
    ("Slow requests", "event=http.request | table time, method, path, status_code, latency_ms | sort -latency_ms | head 20"),
    ("Traffic by page", "event=http.request view=product | stats count by path"),
    ("Job runs", "sourcetype=fintnet:cron | stats count by name, status"),
    ("Failures", "status=error"),
]
_OPS_TABS = (("overview", "Overview"), ("search", "Search"), ("jobs", "Jobs"), ("trust", "Trust chain"))
# Fields worth summarising beside search results, in the order they are shown.
_FACET_FIELDS = ("event", "sourcetype", "bank", "status", "status_code", "path", "name", "email", "data", "view")


def _histogram(rows: list[dict], window: str) -> list[dict]:
    """Event counts per time bucket, for the bar chart above the results."""
    if not rows:
        return []
    seconds = _EVENT_WINDOWS.get(window, 86400)
    buckets = 24 if window != "15m" else 15
    size = max(60, seconds // buckets)
    now = time.time()
    counts: dict[int, int] = {i: 0 for i in range(buckets)}
    for r in rows:
        raw = (r.get("_raw") or {}).get("time")
        if raw is None:
            continue
        index = int((now - float(raw)) // size)
        if 0 <= index < buckets:
            counts[buckets - 1 - index] += 1
    top = max(counts.values()) or 1
    return [{"count": c, "height": round(c / top * 100)} for _, c in sorted(counts.items())]


def _facets(rows: list[dict], limit: int = 5) -> list[dict]:
    """Top values per field, the way a log tool lists interesting fields."""
    out = []
    for field in _FACET_FIELDS:
        values: dict[str, int] = defaultdict(int)
        for r in rows:
            value = r.get(field)
            if value not in (None, ""):
                values[str(value)] += 1
        if values:
            top = sorted(values.items(), key=lambda kv: -kv[1])[:limit]
            out.append({"field": field, "distinct": len(values),
                        "top": [{"value": v, "count": c} for v, c in top]})
    return out


@app.route("/ops")
@login_required
def ops():
    """The internal console: job runs, the categoriser evaluation, the trust
    chain and an event log searched with an SPL subset.

    Only an admin account can open it, and on the operations host (OPS_HOST)
    it is the only page served. Model traces and evaluation detail stay in
    Langfuse; this page links there rather than copying its numbers.
    """
    if current_user.role != "tpp_admin":
        abort(404)
    tab = request.args.get("tab", "overview")
    if tab not in dict(_OPS_TABS):
        tab = "overview"
    runs = JobRun.query.order_by(JobRun.run_date.desc(), JobRun.job).limit(40).all()
    latest = {job: JobRun.query.filter_by(job=job).order_by(JobRun.run_date.desc()).first()
              for job in ("feed", "categorise", "evaluate", "health")}
    evals = (JobRun.query.filter_by(job="evaluate").filter(JobRun.status.in_(["ok", "warn"]))
             .order_by(JobRun.run_date.desc()).limit(14).all())

    query = request.args.get("q", "")
    window = request.args.get("range", "24h")
    since = time.time() - _EVENT_WINDOWS.get(window, 86400) if window != "all" else 0
    records = [r for r in eventlog.read(int(os.getenv("EVENT_SEARCH_LIMIT", "2000")))
               if float(r.get("time") or 0) >= since]
    try:
        found, error = spl.run(query, records), None
    except spl.SplError as exc:
        found, error = spl.run("", records), str(exc)
    matched = found["rows"] if not found["stats"] else spl.run(query.split("|")[0], records)["rows"]

    counts = {"events": len(records),
              "errors": len(spl.run("status=error", records)["rows"]),
              "bank_calls": len(spl.run("event=bank.api.call", records)["rows"]),
              "requests": len(spl.run("event=http.request", records)["rows"])}
    return render_template("ops.html", tab=tab, tabs=_OPS_TABS, runs=runs, latest=latest, evals=evals,
                           provider=cat.provider(), model=llm.MODEL,
                           sign_ins=spl.run("event=auth.* | table time, event, email, switched_from | head 12",
                                            records)["rows"],
                           query=query, window=window, windows=list(_EVENT_WINDOWS) + ["all"],
                           result=found, search_error=error, counts=counts,
                           histogram=_histogram(matched, window), facets=_facets(matched),
                           event_store=eventlog.backend(), examples=_SPL_EXAMPLES)


# ── Account detail views (live API, DB-backed credentials) ───────────────────

def _owned_account(resource_id: str) -> Account:
    """The current user's account with this bank resource id, or 404."""
    return Account.query.filter_by(user_id=current_user.id, resource_id=resource_id).first_or_404()


@app.route("/accounts/<account_id>/balances")
@login_required
def balances(account_id):
    """Live balance lookup (NOT cached): we hit the bank API on every
    request so the figure shown is always the freshest the bank has.
    Requires an active BankConnection for the parent account's bank —
    if the user disconnected, redirect home with a warning.
    """
    acc  = _owned_account(account_id)
    generated = acc.resource_id.startswith("SB-")
    conn = _get_connection(synthbank_store.GEN_PREFIX + acc.bank if generated else acc.bank)
    if not conn:
        flash(f"No active {_bank_name(acc.bank)} connection.", "warning")
        return redirect(url_for("index"))
    try:
        if generated:
            balance_list = synthbank_client.get_balances(conn.consent_id, account_id)
        elif acc.bank == "commerzbank":
            balance_list = commerzbank_client.get_balances(
                commerzbank_client.get_oauth_token(), conn.consent_id, account_id)
        elif acc.bank == "nordea":
            balance_list = nordea_client.get_balances(conn.access_token, account_id)
        elif acc.bank == "ing":
            balance_list = ing_client.get_balances(conn.access_token, account_id)
        else:
            balance_list = psd2_client.get_balances(
                app.config["SANDBOX_BASE_URL"], conn.consent_id, account_id)
        return render_template("balances.html", balances=balance_list,
                               account_id=account_id, bank=acc.bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError,
            nordea_client.NordeaApiError, ing_client.INGApiError, synthbank_client.SynthBankError) as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/accounts/<account_id>/transactions")
@login_required
def transactions(account_id):
    """Live transaction list for one account. Side effect: we also
    upsert the freshly-fetched txns into the local DB so the analytics
    pages (dashboard, spending, recurring) see them next time.
    """
    acc  = _owned_account(account_id)
    generated = acc.resource_id.startswith("SB-")
    conn = _get_connection(synthbank_store.GEN_PREFIX + acc.bank if generated else acc.bank)
    if not conn:
        flash(f"No active {_bank_name(acc.bank)} connection.", "warning")
        return redirect(url_for("index"))
    try:
        if generated:
            txn_data = synthbank_client.get_transactions(conn.consent_id, account_id)
        elif acc.bank == "commerzbank":
            txn_data = commerzbank_client.get_transactions(
                commerzbank_client.get_oauth_token(), conn.consent_id, account_id)
        elif acc.bank == "nordea":
            txn_data = nordea_client.get_transactions(conn.access_token, account_id)
        elif acc.bank == "ing":
            txn_data = ing_client.get_transactions(conn.access_token, account_id)
        else:
            txn_data = psd2_client.get_transactions(
                app.config["SANDBOX_BASE_URL"], conn.consent_id, account_id)
        db_utils.upsert_transactions(acc.bank, account_id, txn_data, user_id=current_user.id)
        return render_template("transactions.html", transactions=txn_data,
                               account_id=account_id, bank=acc.bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError,
            nordea_client.NordeaApiError, ing_client.INGApiError, synthbank_client.SynthBankError) as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


# Hook used by the feed cron to re-sync synthetic bank connections.
app.config["SYNTHBANK_SYNC"] = lambda conn: _fetch_and_store(conn.bank, conn)


# Empty DB? Create the 5 demo logins and their synthetic bank history so a
# fresh checkout or a new database has something to show. This runs at the end
# of the module because seed_data imports names defined above. The evaluation
# population (200 customers) is seeded separately: `python seed_data.py`.
with app.app_context():
    if User.query.count() == 0:
        try:
            import seed_data
            seed_data.main(population=0)
        except Exception as exc:  # never let seeding crash app startup
            db.session.rollback()
            log.warning("seed.failed", extra={"event": "seed.failed", "error": str(exc)[:300]})


if __name__ == "__main__":
    # `debug=True` enables the auto-reloader and the in-browser debugger.
    # NEVER set this in production — the debugger lets anyone with HTTP
    # access execute Python on the server.
    # `ssl_context='adhoc'` serves over HTTPS with a self-signed cert,
    # which UniCredit's redirect_uri (https://localhost:5000/callback)
    # requires. Browser will warn once — accept the cert and proceed.
    app.run(debug=True, ssl_context="adhoc")
