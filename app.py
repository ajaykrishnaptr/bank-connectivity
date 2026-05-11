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
     (aggregation/dashboard/spending/recurring), one section per bank
     (UniCredit, Commerzbank, Nordea, Deutsche Bank, ING), and
     per-account detail views.

Things this file deliberately does NOT do:
  * Talk to bank APIs directly — each bank lives in its own *_client.py.
  * Categorise transactions — categorize.py owns that.
  * Persist account/transaction rows — db_utils.py owns that.

Keep route handlers small. Anything more than ~30 lines of logic
belongs in a helper above so the routes stay readable.
"""
import os
import uuid
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

# .env must be loaded before any module that reads env vars at import time.
load_dotenv()

import auth
import categorize as cat
import commerzbank_client
import currency_utils
import db_utils
import deutschebank_client
import ing_client
import nordea_client
import psd2_client
from logging_config import log
from models import Account, BankConnection, DismissedAlert, Transaction, User, db

# Brand colour per bank. Used for chart bars, donut slices, and the
# coloured pill next to merchant names on the dashboard. Adding a new
# bank? Add it here too — `BANK_COLORS.get(bank, "#95a5a6")` falls back
# to grey, which looks fine but lazy.
BANK_COLORS = {
    "commerzbank":  "#e67e22",
    "nordea":       "#3498db",
    "unicredit":    "#c0392b",
    "deutschebank": "#0018a8",
    "ing":          "#FF6200",
}

# Heuristic thresholds used by the analytics helpers. Tuning these
# changes which alerts the dashboard surfaces — keep them named so
# nobody has to guess what 0.08 means.
_FIXED_AMOUNT_CV_THRESHOLD = 0.08   # coeff. of variation below which we call a charge "fixed"
_PRICE_CREEP_PCT           = 5.0    # % rise from early to recent average that triggers a "price creep" alert
_BURDEN_PCT_FLAGGED        = 20.0   # % of income going to fixed costs that flips burden from info → warning
_LAPSE_WINDOW_DAYS         = 90     # window for adjacent-spend / lapse heuristics


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
    all_txns = (
        db.session.query(Transaction, Account)
        .join(Account, Transaction.account_id == Account.id)
        .filter(Transaction.status == "booked",
                Account.user_id == current_user.id)
        .all()
    )

    def _grouped_by_sign(sign: int) -> list[dict]:
        # sign = -1 => only outflows (expenses)
        # sign = +1 => only inflows  (income)
        by_merchant: dict[tuple[str, str], list] = defaultdict(list)
        for t, a in all_txns:
            if (float(t.amount) < 0) == (sign < 0):
                merchant = t.creditor_name or t.debtor_name or ""
                if merchant:
                    by_merchant[(merchant, a.bank)].append((t, a))

        results = []
        for (merchant, bank), txns in by_merchant.items():
            # A transaction is "recurring" only if it shows up in at
            # least two distinct calendar months — otherwise it's a
            # one-off that happened twice in the same month.
            months = {(t.booking_date.year, t.booking_date.month)
                      for t, _ in txns if t.booking_date}
            if len(months) < 2:
                continue

            amounts = [abs(float(t.amount)) for t, _ in txns]
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
                "occurrences": len(txns),
                "months":      len(months),
                "is_fixed":    cv < _FIXED_AMOUNT_CV_THRESHOLD,
                "last_date":   last_t.booking_date,
                "next_date":   next_date,
            })

        # Most months seen first; ties broken by largest average.
        return sorted(results, key=lambda x: (-x["months"], -x["avg_amount"]))

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
    for cat, by_name in cat_groups.items():
        items = list(by_name.values())
        if len(items) >= 2:
            total = round(sum(i["avg_amount"] for i in items), 2)
            sorted_names = sorted(i["merchant"] for i in items)
            signals.append({
                "type": "redundant", "severity": "warning",
                "key": f"redundant:{cat}:{','.join(sorted_names)}",
                "category": cat,
                "services": [{"merchant": i["merchant"], "avg_amount": i["avg_amount"],
                               "fmt": _fmt(i["avg_amount"], i["merchant"])} for i in items],
                "total_monthly": total,
                "total_fmt": _fmt(total, items[0]["merchant"]),
                "message": f"Are you actually using all {len(items)}? You're paying for {', '.join(i['merchant'] for i in items)} every month.",
            })

    # ── 2. Price creep ───────────────────────────────────────────────────────
    # Compare the average of the first two charges vs. the last two; flag
    # if it has crept up by more than _PRICE_CREEP_PCT. Need at least 4
    # charges for early/recent to be meaningfully different windows.
    for r in fixed:
        if r["category"] not in _PRICE_CREEP_CATEGORIES:
            continue
        charges = sorted(merchant_charges.get(r["merchant"], []), key=lambda x: x[0])
        if len(charges) < 4:
            continue
        early_avg  = mean(amt for _, amt in charges[:2])
        recent_avg = mean(amt for _, amt in charges[-2:])
        if early_avg > 0:
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
                "window_days": _LAPSE_WINDOW_DAYS,
                "message": f"Your {sub['merchant']} membership is still charging. When did you last go?",
            })

    # ── 4. Subscription burden ───────────────────────────────────────────────
    # Same dedup trick as the redundancy signal: a sub appearing on two
    # bank accounts counts once.
    unique_fixed  = {r["merchant"]: r["avg_amount"] for r in fixed}
    unique_income = {r["merchant"]: r["avg_amount"] for r in income}
    monthly_fixed_total = round(sum(unique_fixed.values()), 2)
    monthly_income_avg  = round(sum(unique_income.values()), 2)
    if monthly_income_avg > 0:
        pct = round(monthly_fixed_total / monthly_income_avg * 100, 1)
        # Pick the currency the user actually earns in for the message.
        income_cur_counts: dict = defaultdict(int)
        for t, a in all_txns:
            if float(t.amount) > 0:
                income_cur_counts[t.currency or a.currency or "EUR"] += 1
        inc_cur = max(income_cur_counts, key=income_cur_counts.get) if income_cur_counts else "EUR"
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


# ── Flask app + DB + login setup ─────────────────────────────────────────────
# Each per-bank redirect URI is a distinct route because the OAuth /
# consent flows can't all reuse the same endpoint — they post different
# query parameters and we want one handler per bank to keep things
# clear. Defaults are localhost so a fresh checkout runs without env vars.
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")
app.config["SANDBOX_BASE_URL"]        = os.getenv("SANDBOX_BASE_URL",     "https://api-sandbox.unicredit.it")
app.config["REDIRECT_URI"]            = os.getenv("REDIRECT_URI",        "http://localhost:5000/callback")
app.config["CB_REDIRECT_URI"]         = os.getenv("CB_REDIRECT_URI",     "http://localhost:5000/commerzbank/callback")
app.config["NORDEA_REDIRECT_URI"]     = os.getenv("NORDEA_REDIRECT_URI", "http://localhost:5000/nordea/callback")
app.config["DB_REDIRECT_URI"]         = os.getenv("DB_REDIRECT_URI",     "http://localhost:5000/deutschebank/callback")
app.config["ING_REDIRECT_URI"]        = os.getenv("ING_REDIRECT_URI",    "http://localhost:5000/ing/callback")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL",        "sqlite:///ais.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ── One-shot schema init + migration (runs at import time) ───────────────────
# This deliberately runs at import time so a fresh checkout's first
# request has a working DB. The ALTER TABLE is a hand-rolled migration
# from before BankConnection.user_id existed; SQLite has no native
# migration framework, so the cheapest fix is "is the column there? if
# not, add it". If you migrate to Alembic later, this whole block
# should move into the migration scripts.
with app.app_context():
    db.create_all()

    # SQLite: add user_id column to accounts if missing.
    with db.engine.connect() as conn:
        cols = [r[1] for r in conn.execute(db.text("PRAGMA table_info(accounts)")).fetchall()]
        if "user_id" not in cols:
            conn.execute(db.text("ALTER TABLE accounts ADD COLUMN user_id INTEGER REFERENCES users(id)"))
            conn.commit()

    # Backfill category for any rows that predate the column. Cheap
    # because the cache + override layer handle most lookups instantly.
    uncategorized = Transaction.query.filter(Transaction.category.is_(None)).all()
    for t in uncategorized:
        t.category = cat.categorize(t.creditor_name or t.debtor_name or "")
    if uncategorized:
        db.session.commit()


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
    if bank == "nordea":
        account_list = nordea_client.get_accounts(conn.access_token)
    elif bank == "commerzbank":
        token = commerzbank_client.get_oauth_token()
        account_list = commerzbank_client.get_accounts(token, conn.consent_id)
    elif bank == "deutschebank":
        token = deutschebank_client.get_oauth_token()
        account_list = deutschebank_client.get_accounts(token, conn.consent_id)
    elif bank == "ing":
        account_list = ing_client.get_accounts(conn.access_token)
    else:  # unicredit
        account_list = psd2_client.get_accounts(app.config["SANDBOX_BASE_URL"], conn.consent_id)

    saved = db_utils.upsert_accounts(bank, account_list, user_id=conn.user_id)

    for acc in saved:
        if bank == "nordea":
            txn_data = nordea_client.get_transactions(conn.access_token, acc.resource_id)
        elif bank == "commerzbank":
            token = commerzbank_client.get_oauth_token()
            txn_data = commerzbank_client.get_transactions(token, conn.consent_id, acc.resource_id)
        elif bank == "deutschebank":
            token = deutschebank_client.get_oauth_token()
            txn_data = deutschebank_client.get_transactions(token, conn.consent_id, acc.resource_id)
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
        db_utils.upsert_transactions(bank, acc.resource_id, txn_data)

    log.info("sync.complete", extra={
        "event": "sync.complete", "user_id": conn.user_id, "bank": bank,
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
    if active_conns > 0 and txn_count > 0:
        expenses, income, all_txns = _detect_recurring()
        fixed       = [r for r in expenses if r["is_fixed"]]
        all_signals = _detect_waste(fixed, expenses, income, all_txns)
        dismissed   = {d.alert_key for d in
                       DismissedAlert.query.filter_by(user_id=current_user.id).all()}
        waste = [s for s in all_signals if s.get("key") not in dismissed]

    return render_template("index.html",
        connections=connections,
        active_conns=active_conns,
        account_count=account_count,
        txn_count=txn_count,
        waste=waste,
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
    conn = BankConnection.query.filter_by(user_id=current_user.id, bank=bank).first()
    if conn:
        conn.status = "revoked"
        db.session.commit()
        log.info("connection.disconnect", extra={
            "event": "connection.disconnect", "user_id": current_user.id, "bank": bank,
        })
    flash(f"Disconnected from {bank.capitalize()}.", "info")
    return redirect(url_for("index"))


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    """Email + password sign-in. Honours `?next=` so flask-login can
    bounce users back to wherever they were trying to go."""
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            log.info("auth.login.success", extra={"event": "auth.login.success",
                                                  "user_id": user.id, "email": email})
            return redirect(request.args.get("next") or url_for("index"))
        # Don't tell the attacker which half was wrong.
        log.warning("auth.login.failed", extra={"event": "auth.login.failed", "email": email})
        flash("Invalid email or password.", "error")
    return render_template("login.html")


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
    all_rows  = _txn_acct_query().filter(Transaction.status == "booked").all()
    all_banks = sorted(set(a.bank for _, a in all_rows))

    def _in(t, d0, d1) -> bool:
        return t.booking_date and d0 <= t.booking_date <= d1

    period_rows = [(t, a) for t, a in all_rows if _in(t, date_from, date_to)]
    prev_rows   = [(t, a) for t, a in all_rows if _in(t, prev_from, prev_to)]

    def _totals(rows) -> tuple[float, float]:
        """(spent, income) — both returned as positive floats."""
        spent  = abs(sum(float(t.amount) for t, _ in rows if t.amount < 0))
        income = sum(float(t.amount) for t, _ in rows if t.amount > 0)
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
            bank_period[a.bank]["spent"] += abs(float(t.amount))
        else:
            bank_period[a.bank]["income"] += float(t.amount)
    bank_month_summary = [
        {"bank": b, "spent": round(v["spent"], 2), "income": round(v["income"], 2),
         "color": BANK_COLORS.get(b, "#95a5a6")}
        for b, v in sorted(bank_period.items(), key=lambda x: -x[1]["spent"])
    ]

    cat_totals = defaultdict(float)
    for t, _ in period_rows:
        if t.amount < 0:
            cat_totals[t.category or "Other"] += abs(float(t.amount))
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
        "label": bank.capitalize(),
        "data": [round(abs(sum(
            float(t.amount) for t, a in all_rows
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
            merchant_key[k]["total"] += abs(float(t.amount))
            merchant_key[k]["bank"] = a.bank
    top_merchants = sorted(
        [{"name": k[0], "bank": k[1], "color": BANK_COLORS.get(k[1], "#95a5a6"),
          "total": round(v["total"], 2)} for k, v in merchant_key.items()],
        key=lambda x: -x["total"]
    )[:10]

    recent = sorted(
        [(t, a) for t, a in all_rows if t.booking_date],
        key=lambda x: x[0].booking_date, reverse=True
    )[:15]

    return render_template("dashboard.html",
        date_from=date_from, date_to=date_to,
        period_label=f"{date_from.strftime('%d %b')} – {date_to.strftime('%d %b %Y')}",
        total_spent=round(total_spent, 2), total_income=round(total_income, 2), net=round(net, 2),
        delta_spent=delta_spent, delta_income=delta_income, delta_net=delta_net,
        account_count=_acct_query().count(),
        bank_month_summary=bank_month_summary,
        cat_labels=[c for c, _ in cat_sorted], cat_values=[round(v, 2) for _, v in cat_sorted],
        bank_donut_labels=[b for b, _ in bank_spent_period],
        bank_donut_values=[v for _, v in bank_spent_period],
        bank_donut_colors=[BANK_COLORS.get(b, "#95a5a6") for b, _ in bank_spent_period],
        month_labels=month_labels, monthly_by_bank=monthly_by_bank,
        top_merchants=top_merchants, recent=recent,
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

    def _fetch(d0, d1):
        """Outflow transactions in [d0, d1], joined with their account."""
        return (
            _txn_acct_query()
            .filter(Transaction.amount < 0,
                    Transaction.booking_date >= d0,
                    Transaction.booking_date <= d1)
            .order_by(Transaction.booking_date.desc())
            .all()
        )

    rows      = _fetch(date_from, date_to)
    prev_rows = _fetch(prev_from, prev_to)
    all_banks = sorted(set(a.bank for _, a in rows))

    totals      = defaultdict(float)
    by_category = defaultdict(list)
    cat_by_bank = defaultdict(lambda: defaultdict(float))
    prev_totals = defaultdict(float)

    for txn, acc in rows:
        totals[txn.category]                += float(txn.amount)
        by_category[txn.category].append((txn, acc))
        cat_by_bank[txn.category][acc.bank] += abs(float(txn.amount))
    for txn, _ in prev_rows:
        prev_totals[txn.category] += abs(float(txn.amount))

    sorted_totals = sorted(totals.items(), key=lambda x: x[1])
    categories    = [c for c, _ in sorted_totals]

    grouped_datasets = [{
        "label": b.capitalize(),
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
        monthly_fixed=round(sum(r["avg_amount"] for r in fixed), 2),
        monthly_all=round(sum(r["avg_amount"] for r in expenses), 2),
        monthly_income=round(sum(r["avg_amount"] for r in income), 2),
        bank_colors=BANK_COLORS,
    )


# ── UniCredit (mTLS + consent SCA) ───────────────────────────────────────────

@app.route("/unicredit/connect")
@login_required
def unicredit_connect():
    """Step 1 of the UniCredit flow: ask the bank to create a consent
    and redirect the user to its SCA (Strong Customer Authentication)
    page. Comes back to /callback when the user finishes."""
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
    return render_template("nordea_consent.html", country=nordea_client.COUNTRY)


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
    try:
        redirect_uri = app.config["NORDEA_REDIRECT_URI"]
        location, state = nordea_client.initiate_authorize(redirect_uri)
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


# ── Deutsche Bank (Berlin Group NextGenPSD2, OAuth2 + SCA redirect) ──────────

@app.route("/deutschebank/connect")
@login_required
def deutschebank_connect():
    """Create a Berlin-Group-style consent at Deutsche Bank and send
    the user to its SCA redirect URL.

    Two pre-conditions can fail and produce a useful error:
      * the OAuth client credentials are wrong (caught by `get_oauth_token`)
      * the consent response is missing `consentId` or `scaRedirect`
        — usually a misconfigured DB_CLIENT_ID / DB_BASE_URL.
    """
    try:
        token        = deutschebank_client.get_oauth_token()
        redirect_uri = app.config["DB_REDIRECT_URI"]
        consent      = deutschebank_client.create_consent(token, redirect_uri)
        consent_id   = consent.get("consentId")
        sca_url      = consent.get("_links", {}).get("scaRedirect", {}).get("href")
        if not consent_id or not sca_url:
            flash("Deutsche Bank consent creation failed — check DB_CLIENT_ID / DB_BASE_URL.", "error")
            return redirect(url_for("index"))
        session["db_consent_id"] = consent_id
        return redirect(sca_url)
    except deutschebank_client.DeutscheBankApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/deutschebank/callback")
@login_required
def deutschebank_callback():
    """User came back from DB's SCA page. We picked up `consent_id`
    from the session (set in /deutschebank/connect); if it's missing
    the session expired and the user has to start over."""
    consent_id = session.pop("db_consent_id", None)
    if not consent_id:
        flash("Session expired. Please try connecting Deutsche Bank again.", "error")
        return redirect(url_for("index"))
    try:
        token  = deutschebank_client.get_oauth_token()
        status = deutschebank_client.get_consent_status(token, consent_id)
        if status != "valid":
            flash(f"Deutsche Bank consent not valid (status: {status}).", "warning")
            return render_template("consent_pending.html", status=status)
        _upsert_connection("deutschebank", consent_id=consent_id)
        flash("Deutsche Bank connected. Accounts fetched.", "success")
        return redirect(url_for("dashboard"))
    except deutschebank_client.DeutscheBankApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


# ── ING (mTLS + HTTP Signatures + OAuth2 authorization_code) ─────────────────

@app.route("/ing/connect")
@login_required
def ing_connect():
    """Start ING's OAuth dance. Validates the app-level token early so
    a misconfigured ING_CLIENT_ID surfaces here rather than after the
    user is bounced to ING's auth URL."""
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


# ── Account detail views (live API, DB-backed credentials) ───────────────────

@app.route("/accounts/<account_id>/balances")
@login_required
def balances(account_id):
    """Live balance lookup (NOT cached): we hit the bank API on every
    request so the figure shown is always the freshest the bank has.
    Requires an active BankConnection for the parent account's bank —
    if the user disconnected, redirect home with a warning.
    """
    acc  = Account.query.get_or_404(account_id)
    conn = _get_connection(acc.bank)
    if not conn:
        flash(f"No active {acc.bank.capitalize()} connection.", "warning")
        return redirect(url_for("index"))
    try:
        if acc.bank == "commerzbank":
            balance_list = commerzbank_client.get_balances(
                commerzbank_client.get_oauth_token(), conn.consent_id, account_id)
        elif acc.bank == "nordea":
            balance_list = nordea_client.get_balances(conn.access_token, account_id)
        else:
            # Default catches UniCredit (and any future bank that uses
            # the generic Berlin-Group `psd2_client`).
            balance_list = psd2_client.get_balances(
                app.config["SANDBOX_BASE_URL"], conn.consent_id, account_id)
        return render_template("balances.html", balances=balance_list,
                               account_id=account_id, bank=acc.bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError,
            nordea_client.NordeaApiError) as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/accounts/<account_id>/transactions")
@login_required
def transactions(account_id):
    """Live transaction list for one account. Side effect: we also
    upsert the freshly-fetched txns into the local DB so the analytics
    pages (dashboard, spending, recurring) see them next time.
    """
    acc  = Account.query.get_or_404(account_id)
    conn = _get_connection(acc.bank)
    if not conn:
        flash(f"No active {acc.bank.capitalize()} connection.", "warning")
        return redirect(url_for("index"))
    try:
        if acc.bank == "commerzbank":
            txn_data = commerzbank_client.get_transactions(
                commerzbank_client.get_oauth_token(), conn.consent_id, account_id)
        elif acc.bank == "nordea":
            txn_data = nordea_client.get_transactions(conn.access_token, account_id)
        else:
            txn_data = psd2_client.get_transactions(
                app.config["SANDBOX_BASE_URL"], conn.consent_id, account_id)
        db_utils.upsert_transactions(acc.bank, account_id, txn_data)
        return render_template("transactions.html", transactions=txn_data,
                               account_id=account_id, bank=acc.bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError,
            nordea_client.NordeaApiError) as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


if __name__ == "__main__":
    # `debug=True` enables the auto-reloader and the in-browser debugger.
    # NEVER set this in production — the debugger lets anyone with HTTP
    # access execute Python on the server.
    # `ssl_context='adhoc'` serves over HTTPS with a self-signed cert,
    # which UniCredit's redirect_uri (https://localhost:5000/callback)
    # requires. Browser will warn once — accept the cert and proceed.
    app.run(debug=True, ssl_context="adhoc")
