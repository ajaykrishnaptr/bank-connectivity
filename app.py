import os

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, session, url_for

load_dotenv()

import auth
import commerzbank_client
import psd2_client

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")
app.config["SANDBOX_BASE_URL"] = os.getenv("SANDBOX_BASE_URL", "https://developer.unicredit.eu")
app.config["REDIRECT_URI"] = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")


# ── Home ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/disconnect")
def disconnect():
    session.clear()
    flash("Disconnected.", "info")
    return redirect(url_for("index"))


# ── UniCredit (mTLS) ─────────────────────────────────────────────────────────

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


# ── Commerzbank (OAuth sandbox) ───────────────────────────────────────────────

@app.route("/commerzbank/connect")
def commerzbank_connect():
    try:
        token = commerzbank_client.get_oauth_token()
        commerzbank_client.create_consent(token)
        session["bank"] = "commerzbank"
        session["cb_token"] = token
        return redirect(url_for("accounts"))
    except commerzbank_client.CommerzbankApiError as e:
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
            account_list = commerzbank_client.get_accounts(session["cb_token"])
        else:
            if not session.get("consent_id"):
                flash("No active consent. Please connect first.", "warning")
                return redirect(url_for("index"))
            account_list = psd2_client.get_accounts(
                app.config["SANDBOX_BASE_URL"], session["consent_id"]
            )
        return render_template("accounts.html", accounts=account_list, bank=bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError) as e:
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
            balance_list = commerzbank_client.get_balances(session["cb_token"], account_id)
        else:
            balance_list = psd2_client.get_balances(
                app.config["SANDBOX_BASE_URL"], session["consent_id"], account_id
            )
        return render_template("balances.html", balances=balance_list, account_id=account_id, bank=bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError) as e:
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
            txn_data = commerzbank_client.get_transactions(session["cb_token"], account_id)
        else:
            txn_data = psd2_client.get_transactions(
                app.config["SANDBOX_BASE_URL"], session["consent_id"], account_id
            )
        return render_template("transactions.html", transactions=txn_data, account_id=account_id, bank=bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError) as e:
        flash(str(e), "error")
        return redirect(url_for("accounts"))


if __name__ == "__main__":
    app.run(debug=True)
