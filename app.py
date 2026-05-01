import os

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for

load_dotenv()

import auth
import commerzbank_client
import nordea_client
import psd2_client

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")
app.config["SANDBOX_BASE_URL"] = os.getenv("SANDBOX_BASE_URL", "https://developer.unicredit.eu")
app.config["REDIRECT_URI"] = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")
app.config["CB_REDIRECT_URI"] = os.getenv("CB_REDIRECT_URI", "http://localhost:5000/commerzbank/callback")
app.config["NORDEA_REDIRECT_URI"] = os.getenv("NORDEA_REDIRECT_URI", "http://localhost:5000/nordea/callback")


# ── Home ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/disconnect")
def disconnect():
    session.clear()
    flash("Disconnected.", "info")
    return redirect(url_for("index"))


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
    try:
        redirect_uri = app.config["NORDEA_REDIRECT_URI"]
        location, state = nordea_client.initiate_authorize(redirect_uri)
        session["bank"] = "nordea"
        session["nordea_state"] = state
        # Sandbox auto-approves: Location goes straight to redirect_uri with code
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(location)
        params = parse_qs(parsed.query)
        if "code" in params:
            code = params["code"][0]
            token = nordea_client.exchange_code(code, redirect_uri)
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
        return render_template("transactions.html", transactions=txn_data, account_id=account_id, bank=bank)
    except (psd2_client.PSD2ApiError, commerzbank_client.CommerzbankApiError, nordea_client.NordeaApiError) as e:
        flash(str(e), "error")
        return redirect(url_for("accounts"))


if __name__ == "__main__":
    app.run(debug=True)
