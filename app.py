import os

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

import auth
import categorize as cat
import commerzbank_client
import currency_utils
import db_utils
import nordea_client
import psd2_client
from models import Account, BankConnection, Transaction, User, db

BANK_COLORS = {
    "commerzbank": "#e67e22",
    "nordea":      "#3498db",
    "hdfc":        "#e74c3c",
    "sbi":         "#9b59b6",
    "unicredit":   "#c0392b",
}


def _parse_date_range(req, default="month"):
    from datetime import date, timedelta
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


def _mom_delta(current: float, prev: float):
    """(pct_change, 'up'|'down'|'new')  positive pct = increased."""
    if prev == 0:
        return None, "new"
    pct = round((current - prev) / abs(prev) * 100, 1)
    return pct, "up" if pct > 0 else "down"


def _detect_recurring():
    from collections import defaultdict
    from datetime import date

    all_txns = (
        db.session.query(Transaction, Account)
        .join(Account, Transaction.account_id == Account.id)
        .filter(Transaction.status == "booked",
                Account.user_id == current_user.id)
        .all()
    )

    def _process(sign):
        by_merchant = defaultdict(list)
        for t, a in all_txns:
            if (float(t.amount) < 0) == (sign < 0):
                merchant = t.creditor_name or t.debtor_name or ""
                if merchant:
                    by_merchant[(merchant, a.bank)].append((t, a))
        results = []
        for (merchant, bank), txns in by_merchant.items():
            months = {(t.booking_date.year, t.booking_date.month)
                      for t, _ in txns if t.booking_date}
            if len(months) < 2:
                continue
            amounts = [abs(float(t.amount)) for t, _ in txns]
            avg = sum(amounts) / len(amounts)
            cv = (sum((a - avg) ** 2 for a in amounts) / len(amounts)) ** .5 / avg \
                if avg > 0 else 0
            last_t = max((t for t, _ in txns if t.booking_date),
                         key=lambda t: t.booking_date)
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
                "merchant": merchant, "bank": bank,
                "color": BANK_COLORS.get(bank, "#95a5a6"),
                "category": last_t.category or "Other",
                "avg_amount": round(avg, 2),
                "occurrences": len(txns), "months": len(months),
                "is_fixed": cv < 0.08,
                "last_date": last_t.booking_date, "next_date": next_date,
            })
        return sorted(results, key=lambda x: (-x["months"], -x["avg_amount"]))

    return _process(-1), _process(+1), all_txns


def _detect_waste(fixed, all_recurring, income, all_txns):
    from collections import defaultdict
    from datetime import date, timedelta
    from statistics import mean

    signals = []
    today = date.today()
    cutoff_90 = today - timedelta(days=90)

    # ── Currency helpers (built once, used by all signals) ────────────────────
    merchant_charges  = defaultdict(list)
    merchant_currency = defaultdict(lambda: defaultdict(int))
    for t, a in all_txns:
        if float(t.amount) < 0 and t.booking_date:
            key = t.creditor_name or t.debtor_name or ""
            if key:
                merchant_charges[key].append((t.booking_date, abs(float(t.amount))))
                merchant_currency[key][t.currency or a.currency or "EUR"] += 1

    def _currency(merchant):
        counts = merchant_currency.get(merchant, {})
        return max(counts, key=counts.get) if counts else "EUR"

    def _fmt(amount, merchant):
        cur = _currency(merchant)
        symbol = "€" if cur == "EUR" else cur + " "
        return f"{symbol}{amount:.2f}"

    # ── 1. Redundant category ─────────────────────────────────────────────────
    # Deduplicate by merchant name first — same service on two bank accounts is one service
    REDUNDANCY_CATEGORIES = {"Entertainment", "Health & Fitness"}
    cat_groups = defaultdict(dict)   # category → {merchant_name: best_r}
    for r in fixed:
        if r["category"] in REDUNDANCY_CATEGORIES:
            name = r["merchant"]
            existing = cat_groups[r["category"]].get(name)
            if existing is None or r["avg_amount"] > existing["avg_amount"]:
                cat_groups[r["category"]][name] = r
    for cat, by_name in cat_groups.items():
        items = list(by_name.values())
        if len(items) >= 2:
            total = round(sum(i["avg_amount"] for i in items), 2)
            signals.append({
                "type": "redundant", "severity": "warning",
                "category": cat,
                "services": [{"merchant": i["merchant"], "avg_amount": i["avg_amount"],
                               "fmt": _fmt(i["avg_amount"], i["merchant"])} for i in items],
                "total_monthly": total,
                "total_fmt": _fmt(total, items[0]["merchant"]),
                "message": f"{len(items)} {cat} subscriptions — {_fmt(total, items[0]['merchant'])}/mo combined",
            })

    # ── 2. Price creep ────────────────────────────────────────────────────────
    PRICE_CREEP_CATEGORIES = {"Entertainment", "Utilities", "Health", "Healthcare", "Health & Fitness"}
    for r in fixed:
        if r["category"] not in PRICE_CREEP_CATEGORIES:
            continue
        charges = sorted(merchant_charges.get(r["merchant"], []), key=lambda x: x[0])
        if len(charges) < 4:
            continue
        early_avg  = mean(amt for _, amt in charges[:2])
        recent_avg = mean(amt for _, amt in charges[-2:])
        if early_avg > 0:
            pct = (recent_avg - early_avg) / early_avg * 100
            if pct > 5.0:
                signals.append({
                    "type": "price_creep", "severity": "warning",
                    "merchant": r["merchant"],
                    "early_avg": round(early_avg, 2),
                    "recent_avg": round(recent_avg, 2),
                    "early_fmt": _fmt(early_avg, r["merchant"]),
                    "recent_fmt": _fmt(recent_avg, r["merchant"]),
                    "pct_increase": round(pct, 1),
                    "message": f"{r['merchant']} price went up {pct:.0f}% ({_fmt(early_avg, r['merchant'])} → {_fmt(recent_avg, r['merchant'])})",
                })

    # ── 3. Correlation-based lapse ────────────────────────────────────────────
    TRANSIT_KWS   = ["bvg", "hvv", "mvv", "vbb", "rnv", "vgn", "transit",
                     "monatsticket", "deutschlandticket"]
    RIDESHARE_KWS = ["uber", "taxi", "bolt", "free now", "freenow", "mytaxi"]
    GYM_KWS       = ["gym", "fitness", "sport", "mcfit", "planet fitness",
                     "urban sports", "holmes place"]
    INSURANCE_KWS = ["krankenkasse", "insurance", "versicherung", "tk ", "aok", "barmer"]

    transit_subs = [r for r in all_recurring
                    if any(kw in r["merchant"].lower() for kw in TRANSIT_KWS)
                    and r["months"] >= 3]
    if transit_subs:
        rideshare_txns = [(t, a) for t, a in all_txns
                         if t.booking_date and t.booking_date >= cutoff_90
                         and float(t.amount) < 0
                         and any(kw in (t.creditor_name or t.debtor_name or "").lower()
                                 for kw in RIDESHARE_KWS)]
        if len(rideshare_txns) >= 3:
            rideshare_total = round(sum(abs(float(t.amount)) for t, _ in rideshare_txns), 2)
            sub = transit_subs[0]
            signals.append({
                "type": "lapse", "severity": "info",
                "merchant": sub["merchant"],
                "reason": "transit_rideshare_overlap",
                "subscription_monthly": sub["avg_amount"],
                "conflicting_count": len(rideshare_txns),
                "conflicting_amount": rideshare_total,
                "window_days": 90,
                "message": (f"{sub['merchant']} monthly pass + {len(rideshare_txns)} rideshare"
                            f" rides in 90 days (€{rideshare_total}) — are you using transit?"),
            })

    gym_subs = [r for r in fixed
                if r["category"] == "Health & Fitness"
                or any(kw in r["merchant"].lower() for kw in GYM_KWS)]
    if gym_subs:
        health_recent = [t for t, a in all_txns
                         if t.booking_date and t.booking_date >= cutoff_90
                         and float(t.amount) < 0
                         and t.category in ("Health & Fitness", "Healthcare")
                         and not any(kw in (t.creditor_name or t.debtor_name or "").lower()
                                     for kw in INSURANCE_KWS)]
        if len(health_recent) == 0:
            sub = gym_subs[0]
            signals.append({
                "type": "lapse", "severity": "info",
                "merchant": sub["merchant"],
                "reason": "gym_no_adjacent_spend",
                "subscription_monthly": sub["avg_amount"],
                "window_days": 90,
                "message": f"{sub['merchant']} is active but no health/fitness purchases in 90 days",
            })

    # ── 4. Subscription burden ────────────────────────────────────────────────
    monthly_fixed_total = round(sum(r["avg_amount"] for r in fixed), 2)
    monthly_income_avg  = round(sum(r["avg_amount"] for r in income), 2)
    if monthly_income_avg > 0:
        pct = round(monthly_fixed_total / monthly_income_avg * 100, 1)
        # Determine dominant currency from income transactions
        income_cur_counts: dict = defaultdict(int)
        for t, a in all_txns:
            if float(t.amount) > 0:
                income_cur_counts[t.currency or a.currency or "EUR"] += 1
        inc_cur = max(income_cur_counts, key=income_cur_counts.get) if income_cur_counts else "EUR"
        inc_sym = "€" if inc_cur == "EUR" else inc_cur + " "
        signals.append({
            "type": "burden",
            "severity": "warning" if pct > 20 else "info",
            "monthly_fixed": monthly_fixed_total,
            "monthly_income": monthly_income_avg,
            "monthly_fixed_fmt": f"{inc_sym}{monthly_fixed_total:.2f}",
            "monthly_income_fmt": f"{inc_sym}{monthly_income_avg:.2f}",
            "pct": pct,
            "flagged": pct > 20,
            "message": f"Fixed subscriptions are {pct}% of your monthly income",
        })

    return signals


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")
app.config["SANDBOX_BASE_URL"] = os.getenv("SANDBOX_BASE_URL", "https://developer.unicredit.eu")
app.config["REDIRECT_URI"] = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")
app.config["CB_REDIRECT_URI"] = os.getenv("CB_REDIRECT_URI", "http://localhost:5000/commerzbank/callback")
app.config["NORDEA_REDIRECT_URI"] = os.getenv("NORDEA_REDIRECT_URI", "http://localhost:5000/nordea/callback")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///ais.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


with app.app_context():
    db.create_all()
    # SQLite: add user_id column to accounts if missing
    with db.engine.connect() as conn:
        cols = [r[1] for r in conn.execute(db.text("PRAGMA table_info(accounts)")).fetchall()]
        if "user_id" not in cols:
            conn.execute(db.text("ALTER TABLE accounts ADD COLUMN user_id INTEGER REFERENCES users(id)"))
            conn.commit()
    # Backfill category for any rows that predate the column
    uncategorized = Transaction.query.filter(Transaction.category.is_(None)).all()
    for t in uncategorized:
        t.category = cat.categorize(t.creditor_name or t.debtor_name or "")
    if uncategorized:
        db.session.commit()


def _acct_query():
    return Account.query.filter(Account.user_id == current_user.id)


def _txn_acct_query():
    return (
        db.session.query(Transaction, Account)
        .join(Account, Transaction.account_id == Account.id)
        .filter(Account.user_id == current_user.id)
    )


def _get_connection(bank):
    """Return the active BankConnection for current_user + bank, or None."""
    return BankConnection.query.filter_by(
        user_id=current_user.id, bank=bank, status="active"
    ).first()


def _upsert_connection(bank, access_token=None, consent_id=None):
    """Save or update a BankConnection for current_user, then fetch and store data."""
    conn = BankConnection.query.filter_by(user_id=current_user.id, bank=bank).first()
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
    _fetch_and_store(bank, conn)
    return conn


def _fetch_and_store(bank, conn):
    """Fetch all accounts + transactions from the bank API and upsert into DB."""
    if bank == "nordea":
        account_list = nordea_client.get_accounts(conn.access_token)
    elif bank == "commerzbank":
        token = commerzbank_client.get_oauth_token()
        account_list = commerzbank_client.get_accounts(token, conn.consent_id)
    else:  # unicredit
        account_list = psd2_client.get_accounts(app.config["SANDBOX_BASE_URL"], conn.consent_id)

    saved = db_utils.upsert_accounts(bank, account_list, user_id=conn.user_id)

    for acc in saved:
        if bank == "nordea":
            txn_data = nordea_client.get_transactions(conn.access_token, acc.resource_id)
        elif bank == "commerzbank":
            token = commerzbank_client.get_oauth_token()
            txn_data = commerzbank_client.get_transactions(token, conn.consent_id, acc.resource_id)
        else:
            txn_data = psd2_client.get_transactions(
                app.config["SANDBOX_BASE_URL"], conn.consent_id, acc.resource_id)
        db_utils.upsert_transactions(bank, acc.resource_id, txn_data)


# ── Home ─────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    connections    = {c.bank: c for c in BankConnection.query.filter_by(user_id=current_user.id).all()}
    active_conns   = sum(1 for c in connections.values() if c.status == "active")
    account_count  = _acct_query().count()
    account_ids    = [a.id for a in _acct_query().with_entities(Account.id).all()]
    txn_count      = Transaction.query.filter(Transaction.account_id.in_(account_ids)).count() if account_ids else 0
    return render_template("index.html",
        connections=connections,
        active_conns=active_conns,
        account_count=account_count,
        txn_count=txn_count,
    )


@app.route("/disconnect/<bank>")
@login_required
def disconnect(bank):
    conn = BankConnection.query.filter_by(user_id=current_user.id, bank=bank).first()
    if conn:
        conn.status = "revoked"
        db.session.commit()
    flash(f"Disconnected from {bank.capitalize()}.", "info")
    return redirect(url_for("index"))


# ── Auth ─────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(request.args.get("next") or url_for("index"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
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
            user = User(
                email=email,
                password_hash=generate_password_hash(password),
                role="user",
            )
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash("Account created. Welcome!", "success")
            return redirect(url_for("index"))
    return render_template("signup.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    logout_user()
    return redirect(url_for("login"))


# ── Analytics ────────────────────────────────────────────────────────────────

@app.route("/aggregation")
@login_required
def aggregation():
    from collections import defaultdict

    accounts = _acct_query().order_by(Account.bank, Account.id).all()
    rates    = currency_utils.get_rates("EUR")

    rows = []
    bank_totals_eur = defaultdict(float)
    currency_totals = defaultdict(float)   # original-currency totals per currency

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
    from collections import defaultdict
    from datetime import date, timedelta

    date_from, date_to = _parse_date_range(request, default="month")
    period_days = (date_to - date_from).days + 1
    prev_to   = date_from - timedelta(days=1)
    prev_from = prev_to   - timedelta(days=period_days - 1)

    all_rows  = _txn_acct_query().filter(Transaction.status == "booked").all()
    all_banks = sorted(set(a.bank for _, a in all_rows))

    def _in(t, d0, d1):
        return t.booking_date and d0 <= t.booking_date <= d1

    period_rows = [(t, a) for t, a in all_rows if _in(t, date_from, date_to)]
    prev_rows   = [(t, a) for t, a in all_rows if _in(t, prev_from, prev_to)]

    def _totals(rows):
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

    today = date.today()
    months, y, m = [], today.year, today.month
    for _ in range(6):
        months.insert(0, (y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    month_labels = [date(y, m, 1).strftime("%b %Y") for y, m in months]
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
    from collections import defaultdict
    from datetime import timedelta

    date_from, date_to = _parse_date_range(request, default="3m")
    period_days = (date_to - date_from).days + 1
    prev_to   = date_from - timedelta(days=1)
    prev_from = prev_to   - timedelta(days=period_days - 1)

    def _fetch(d0, d1):
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
    try:
        sca_url = auth.initiate_consent_flow()
        return redirect(sca_url)
    except psd2_client.PSD2ApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/callback")
@login_required
def callback():
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
    try:
        commerzbank_client.get_oauth_token()  # validate credentials early
        return render_template("cb_consent.html", consent_id=commerzbank_client.SANDBOX_CONSENT)
    except commerzbank_client.CommerzbankApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/commerzbank/authorize", methods=["POST"])
@login_required
def commerzbank_authorize():
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
    return render_template("nordea_consent.html", country=nordea_client.COUNTRY)


@app.route("/nordea/authorize", methods=["POST"])
@login_required
def nordea_authorize():
    try:
        from urllib.parse import parse_qs, urlparse
        redirect_uri = app.config["NORDEA_REDIRECT_URI"]
        location, state = nordea_client.initiate_authorize(redirect_uri)
        session["nordea_state"] = state
        parsed = urlparse(location)
        params = parse_qs(parsed.query)
        if "code" in params:
            # Sandbox auto-approves — code is already in the redirect Location
            token = nordea_client.exchange_code(params["code"][0], redirect_uri)
            _upsert_connection("nordea", access_token=token)
            flash("Nordea connected. Accounts fetched.", "success")
            return redirect(url_for("dashboard"))
        # Production: send user to Nordea SCA page
        return redirect(location)
    except nordea_client.NordeaApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/nordea/callback")
@login_required
def nordea_callback():
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


# ── Account detail views (live API, DB-backed credentials) ───────────────────

@app.route("/accounts/<account_id>/balances")
@login_required
def balances(account_id):
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
    app.run(debug=True)
