import os

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for

load_dotenv()

import auth
import categorize as cat
import commerzbank_client
import db_utils
import nordea_client
import psd2_client
from models import Transaction, db

BANK_COLORS = {
    "commerzbank": "#e67e22",
    "nordea":      "#3498db",
    "hdfc":        "#e74c3c",
    "sbi":         "#9b59b6",
    "unicredit":   "#c0392b",
}

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")
app.config["SANDBOX_BASE_URL"] = os.getenv("SANDBOX_BASE_URL", "https://developer.unicredit.eu")
app.config["REDIRECT_URI"] = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")
app.config["CB_REDIRECT_URI"] = os.getenv("CB_REDIRECT_URI", "http://localhost:5000/commerzbank/callback")
app.config["NORDEA_REDIRECT_URI"] = os.getenv("NORDEA_REDIRECT_URI", "http://localhost:5000/nordea/callback")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "postgresql://localhost/ais_db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

with app.app_context():
    db.create_all()
    # Backfill category for any rows that predate the column
    uncategorized = Transaction.query.filter(Transaction.category.is_(None)).all()
    for t in uncategorized:
        t.category = cat.categorize(t.creditor_name or t.debtor_name or "")
    if uncategorized:
        db.session.commit()


# ── Home ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/disconnect")
def disconnect():
    session.clear()
    flash("Disconnected.", "info")
    return redirect(url_for("index"))


# ── Analytics ────────────────────────────────────────────────────────────────

@app.route("/aggregation")
def aggregation():
    from collections import defaultdict
    from models import Account

    accounts = (
        db.session.query(Account)
        .order_by(Account.bank, Account.id)
        .all()
    )

    BANK_COLORS = {
        "commerzbank": "#e67e22",
        "nordea":      "#3498db",
        "hdfc":        "#e74c3c",
        "sbi":         "#9b59b6",
        "unicredit":   "#c0392b",
    }

    rows = []
    bank_totals = defaultdict(float)
    for acc in accounts:
        balance = sum(float(t.amount) for t in acc.transactions)
        last_txn = max((t.booking_date for t in acc.transactions if t.booking_date), default=None)
        rows.append({
            "account":    acc,
            "balance":    round(balance, 2),
            "txn_count":  len(acc.transactions),
            "last_txn":   last_txn,
            "color":      BANK_COLORS.get(acc.bank, "#95a5a6"),
        })
        bank_totals[acc.bank] += balance

    total_balance = round(sum(r["balance"] for r in rows), 2)
    bank_summary = [
        {"bank": b, "total": round(t, 2), "color": BANK_COLORS.get(b, "#95a5a6")}
        for b, t in sorted(bank_totals.items(), key=lambda x: -x[1])
    ]

    chart_labels  = [f"{r['account'].owner_name or r['account'].iban} ({r['account'].bank.upper()})" for r in rows]
    chart_values  = [r["balance"] for r in rows]
    chart_colors  = [r["color"] for r in rows]

    return render_template("aggregation.html",
        rows=rows,
        bank_summary=bank_summary,
        total_balance=total_balance,
        chart_labels=chart_labels,
        chart_values=chart_values,
        chart_colors=chart_colors,
        account_count=len(rows),
    )


@app.route("/dashboard")
def dashboard():
    from collections import defaultdict
    from datetime import date
    from models import Account

    today = date.today()
    cy, cm = today.year, today.month

    all_rows = (
        db.session.query(Transaction, Account)
        .join(Account, Transaction.account_id == Account.id)
        .filter(Transaction.status == "booked")
        .all()
    )
    all_banks = sorted(set(a.bank for _, a in all_rows))

    this_month = [(t, a) for t, a in all_rows
                  if t.booking_date and t.booking_date.year == cy and t.booking_date.month == cm]

    total_spent  = abs(sum(float(t.amount) for t, _ in this_month if t.amount < 0))
    total_income = sum(float(t.amount) for t, _ in this_month if t.amount > 0)
    net          = total_income - total_spent

    # Per-bank spend/income this month
    bank_month = defaultdict(lambda: {"spent": 0.0, "income": 0.0})
    for t, a in this_month:
        if t.amount < 0:
            bank_month[a.bank]["spent"] += abs(float(t.amount))
        else:
            bank_month[a.bank]["income"] += float(t.amount)
    bank_month_summary = [
        {"bank": b, "spent": round(v["spent"], 2), "income": round(v["income"], 2),
         "color": BANK_COLORS.get(b, "#95a5a6")}
        for b, v in sorted(bank_month.items(), key=lambda x: -x[1]["spent"])
    ]

    # Category donut (current month, all banks)
    cat_totals = defaultdict(float)
    for t, _ in this_month:
        if t.amount < 0:
            cat_totals[t.category or "Other"] += abs(float(t.amount))
    cat_sorted = sorted(cat_totals.items(), key=lambda x: -x[1])

    # Bank spending donut (current month)
    bank_spent_month = [(b, round(v["spent"], 2)) for b, v in bank_month.items() if v["spent"] > 0]
    bank_spent_month.sort(key=lambda x: -x[1])

    # Last 6 months — stacked by bank
    months = []
    y, m = cy, cm
    for _ in range(6):
        months.insert(0, (y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    month_labels = [date(y, m, 1).strftime("%b %Y") for y, m in months]

    monthly_by_bank = []
    for bank in all_banks:
        data = []
        for y, m in months:
            amt = round(abs(sum(
                float(t.amount) for t, a in all_rows
                if a.bank == bank and t.booking_date
                and t.booking_date.year == y and t.booking_date.month == m
                and t.amount < 0
            )), 2)
            data.append(amt)
        monthly_by_bank.append({
            "label": bank.capitalize(),
            "data": data,
            "backgroundColor": BANK_COLORS.get(bank, "#95a5a6") + "cc",
            "borderColor": BANK_COLORS.get(bank, "#95a5a6"),
            "borderWidth": 1,
            "stack": "expenses",
        })

    # Top 10 merchants with bank
    merchant_key = defaultdict(lambda: {"total": 0.0, "bank": ""})
    for t, a in all_rows:
        if t.amount < 0:
            name = t.creditor_name or t.debtor_name or "Unknown"
            k = (name, a.bank)
            merchant_key[k]["total"] += abs(float(t.amount))
            merchant_key[k]["bank"] = a.bank
    top_merchants = sorted(
        [{"name": k[0], "bank": k[1], "color": BANK_COLORS.get(k[1], "#95a5a6"),
          "total": round(v["total"], 2)} for k, v in merchant_key.items()],
        key=lambda x: -x["total"]
    )[:10]

    # Recent 15 transactions
    recent = sorted(
        [(t, a) for t, a in all_rows if t.booking_date],
        key=lambda x: x[0].booking_date, reverse=True
    )[:15]

    return render_template("dashboard.html",
        current_month=date(cy, cm, 1).strftime("%B %Y"),
        total_spent=round(total_spent, 2),
        total_income=round(total_income, 2),
        net=round(net, 2),
        account_count=Account.query.count(),
        bank_month_summary=bank_month_summary,
        cat_labels=[c for c, _ in cat_sorted],
        cat_values=[round(v, 2) for _, v in cat_sorted],
        bank_donut_labels=[b for b, _ in bank_spent_month],
        bank_donut_values=[v for _, v in bank_spent_month],
        bank_donut_colors=[BANK_COLORS.get(b, "#95a5a6") for b, _ in bank_spent_month],
        month_labels=month_labels,
        monthly_by_bank=monthly_by_bank,
        top_merchants=top_merchants,
        recent=recent,
    )


@app.route("/spending")
def spending():
    from collections import defaultdict
    from models import Account
    rows = (
        db.session.query(Transaction, Account)
        .join(Account, Transaction.account_id == Account.id)
        .filter(Transaction.amount < 0)
        .order_by(Transaction.booking_date.desc())
        .all()
    )
    all_banks = sorted(set(a.bank for _, a in rows))

    totals     = defaultdict(float)
    by_category = defaultdict(list)
    cat_by_bank = defaultdict(lambda: defaultdict(float))

    for txn, acc in rows:
        totals[txn.category] += float(txn.amount)
        by_category[txn.category].append((txn, acc))
        cat_by_bank[txn.category][acc.bank] += abs(float(txn.amount))

    sorted_totals = sorted(totals.items(), key=lambda x: x[1])
    categories = [c for c, _ in sorted_totals]

    # Grouped bar datasets: one per bank
    grouped_datasets = [{
        "label": b.capitalize(),
        "data": [round(cat_by_bank[c].get(b, 0), 2) for c in categories],
        "backgroundColor": BANK_COLORS.get(b, "#95a5a6") + "cc",
        "borderColor": BANK_COLORS.get(b, "#95a5a6"),
        "borderWidth": 1,
        "borderRadius": 3,
    } for b in all_banks]

    # Per-category bank breakdown for table
    cat_bank_rows = {
        c: [(b, round(cat_by_bank[c].get(b, 0), 2)) for b in all_banks]
        for c in categories
    }

    bank_totals = {
        b: round(sum(cat_by_bank[c].get(b, 0) for c in categories), 2)
        for b in all_banks
    }
    grand_total = round(sum(bank_totals.values()), 2)
    txn_count = sum(len(v) for v in by_category.values())
    return render_template("spending.html",
        totals=sorted_totals,
        by_category=by_category,
        all_banks=all_banks,
        categories=categories,
        grouped_datasets=grouped_datasets,
        cat_bank_rows=cat_bank_rows,
        bank_totals=bank_totals,
        grand_total=grand_total,
        bank_colors=BANK_COLORS,
        txn_count=txn_count,
    )


# ── UniCredit (mTLS + consent SCA) ───────────────────────────────────────────

@app.route("/unicredit/connect")
def unicredit_connect():
    try:
        sca_url = auth.initiate_consent_flow()
        session["bank"] = "unicredit"
        return redirect(sca_url)
    except psd2_client.PSD2ApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/callback")
def callback():
    try:
        status = auth.check_and_store_consent_status()
        if status == "valid":
            return redirect(url_for("accounts"))
        flash(f"Consent not yet valid (status: {status}). Complete SCA and try again.", "warning")
        return render_template("consent_pending.html", status=status)
    except psd2_client.PSD2ApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


# ── Commerzbank (OAuth + consent SCA) ────────────────────────────────────────

@app.route("/commerzbank/connect")
def commerzbank_connect():
    try:
        commerzbank_client.get_oauth_token()  # validate credentials early
        consent_id = commerzbank_client.SANDBOX_CONSENT
        session["bank"] = "commerzbank"
        # Show the SCA consent authorization screen before granting access
        return render_template("cb_consent.html", consent_id=consent_id)
    except commerzbank_client.CommerzbankApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/commerzbank/authorize", methods=["POST"])
def commerzbank_authorize():
    consent_id = request.form.get("consent_id")
    if not consent_id:
        flash("Missing consent ID.", "error")
        return redirect(url_for("index"))
    try:
        token = commerzbank_client.get_oauth_token()
        status = commerzbank_client.get_consent_status(token, consent_id)
        if status != "valid":
            flash(f"Consent not valid (status: {status}).", "warning")
            return render_template("consent_pending.html", status=status)
        session["cb_consent_id"] = consent_id
        return redirect(url_for("accounts"))
    except commerzbank_client.CommerzbankApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


# ── Nordea (OAuth authorization_code + SCA redirect) ─────────────────────────

@app.route("/nordea/connect")
def nordea_connect():
    session["bank"] = "nordea"
    return render_template("nordea_consent.html", country=nordea_client.COUNTRY)


@app.route("/nordea/authorize", methods=["POST"])
def nordea_authorize():
    try:
        from urllib.parse import urlparse, parse_qs
        redirect_uri = app.config["NORDEA_REDIRECT_URI"]
        location, state = nordea_client.initiate_authorize(redirect_uri)
        session["nordea_state"] = state
        parsed = urlparse(location)
        params = parse_qs(parsed.query)
        if "code" in params:
            # Sandbox: mock authorizer auto-approved, code already in Location
            token = nordea_client.exchange_code(params["code"][0], redirect_uri)
            session["nordea_token"] = token
            return redirect(url_for("accounts"))
        # Production: redirect user to Nordea SCA page
        return redirect(location)
    except nordea_client.NordeaApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/nordea/callback")
def nordea_callback():
    code = request.args.get("code")
    if not code:
        return render_template("nordea_code.html", sca_url=None)
    try:
        redirect_uri = app.config["NORDEA_REDIRECT_URI"]
        token = nordea_client.exchange_code(code, redirect_uri)
        session["bank"] = "nordea"
        session["nordea_token"] = token
        return redirect(url_for("accounts"))
    except nordea_client.NordeaApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


# ── Shared account views ──────────────────────────────────────────────────────

@app.route("/accounts")
def accounts():
    bank = session.get("bank")
    if not bank:
        flash("Please connect to a bank first.", "warning")
        return redirect(url_for("index"))
    try:
        if bank == "commerzbank":
            consent_id = session.get("cb_consent_id")
            if not consent_id:
                flash("No active consent. Please connect first.", "warning")
                return redirect(url_for("index"))
            account_list = commerzbank_client.get_accounts(
                commerzbank_client.get_oauth_token(), consent_id)
        elif bank == "nordea":
            token = session.get("nordea_token")
            if not token:
                flash("No active session. Please connect first.", "warning")
                return redirect(url_for("index"))
            account_list = nordea_client.get_accounts(token)
        else:
            if not session.get("consent_id"):
                flash("No active consent. Please connect first.", "warning")
                return redirect(url_for("index"))
            account_list = psd2_client.get_accounts(
                app.config["SANDBOX_BASE_URL"], session["consent_id"])
        db_utils.upsert_accounts(bank, account_list)
        return render_template("accounts.html", accounts=account_list, bank=bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError, nordea_client.NordeaApiError) as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/accounts/<account_id>/balances")
def balances(account_id):
    bank = session.get("bank")
    if not bank:
        flash("Please connect to a bank first.", "warning")
        return redirect(url_for("index"))
    try:
        if bank == "commerzbank":
            balance_list = commerzbank_client.get_balances(
                commerzbank_client.get_oauth_token(), session["cb_consent_id"], account_id)
        elif bank == "nordea":
            balance_list = nordea_client.get_balances(session["nordea_token"], account_id)
        else:
            balance_list = psd2_client.get_balances(
                app.config["SANDBOX_BASE_URL"], session["consent_id"], account_id)
        return render_template("balances.html", balances=balance_list, account_id=account_id, bank=bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError, nordea_client.NordeaApiError) as e:
        flash(str(e), "error")
        return redirect(url_for("accounts"))


@app.route("/accounts/<account_id>/transactions")
def transactions(account_id):
    bank = session.get("bank")
    if not bank:
        flash("Please connect to a bank first.", "warning")
        return redirect(url_for("index"))
    try:
        if bank == "commerzbank":
            txn_data = commerzbank_client.get_transactions(
                commerzbank_client.get_oauth_token(), session["cb_consent_id"], account_id)
        elif bank == "nordea":
            txn_data = nordea_client.get_transactions(session["nordea_token"], account_id)
        else:
            txn_data = psd2_client.get_transactions(
                app.config["SANDBOX_BASE_URL"], session["consent_id"], account_id)
        db_utils.upsert_transactions(bank, account_id, txn_data)
        return render_template("transactions.html", transactions=txn_data, account_id=account_id, bank=bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError, nordea_client.NordeaApiError) as e:
        flash(str(e), "error")
        return redirect(url_for("accounts"))


if __name__ == "__main__":
    app.run(debug=True)
