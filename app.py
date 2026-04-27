import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, session, url_for

load_dotenv()

import auth
import psd2_client

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")
app.config["SANDBOX_BASE_URL"] = os.getenv("SANDBOX_BASE_URL", "https://developer.unicredit.eu")
app.config["REDIRECT_URI"] = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/connect")
def connect():
    try:
        sca_url = auth.initiate_consent_flow()
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
        flash(f"Consent not yet valid (status: {status}). Complete the SCA step and try again.", "warning")
        return render_template("consent_pending.html", status=status)
    except psd2_client.PSD2ApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/accounts")
def accounts():
    if not session.get("consent_id"):
        flash("No active consent. Please connect first.", "warning")
        return redirect(url_for("index"))
    try:
        account_list = psd2_client.get_accounts(
            app.config["SANDBOX_BASE_URL"], session["consent_id"]
        )
        return render_template("accounts.html", accounts=account_list)
    except psd2_client.PSD2ApiError as e:
        flash(str(e), "error")
        return redirect(url_for("index"))


@app.route("/accounts/<account_id>/balances")
def balances(account_id):
    if not session.get("consent_id"):
        flash("No active consent. Please connect first.", "warning")
        return redirect(url_for("index"))
    try:
        balance_list = psd2_client.get_balances(
            app.config["SANDBOX_BASE_URL"], session["consent_id"], account_id
        )
        return render_template("balances.html", balances=balance_list, account_id=account_id)
    except psd2_client.PSD2ApiError as e:
        flash(str(e), "error")
        return redirect(url_for("accounts"))


@app.route("/accounts/<account_id>/transactions")
def transactions(account_id):
    if not session.get("consent_id"):
        flash("No active consent. Please connect first.", "warning")
        return redirect(url_for("index"))
    try:
        txn_data = psd2_client.get_transactions(
            app.config["SANDBOX_BASE_URL"], session["consent_id"], account_id
        )
        return render_template("transactions.html", transactions=txn_data, account_id=account_id)
    except psd2_client.PSD2ApiError as e:
        flash(str(e), "error")
        return redirect(url_for("accounts"))


@app.route("/disconnect")
def disconnect():
    session.clear()
    flash("Disconnected.", "info")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
