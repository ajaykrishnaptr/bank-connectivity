"""
HTTP face of the synthetic bank: a small Berlin Group AIS API plus the
simulated SCA page.

  POST /synthetic-bank/v1/consents                      create a consent (logged-in demo user)
  GET  /synthetic-bank/v1/accounts                      Consent-ID header
  GET  /synthetic-bank/v1/accounts/<id>/balances        Consent-ID header
  GET  /synthetic-bank/v1/accounts/<id>/transactions    Consent-ID header, dateFrom / dateTo
  GET  /synthetic-bank/authorise/<consent_id>           simulated SCA screen

Ground-truth labels are never served (label firewall).
"""
from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

import synthbank_client as client
from synthbank import store

bp = Blueprint("synthbank", __name__, url_prefix="/synthetic-bank")


def _error(exc: client.SynthBankError):
    return jsonify({"tppMessages": [{"category": "ERROR", "text": str(exc)}]}), exc.status_code or 400


def _date(arg: str) -> date | None:
    try:
        return date.fromisoformat(request.args.get(arg, ""))
    except ValueError:
        return None


@bp.post("/v1/consents")
@login_required
def create_consent():
    cust = store.customer_for_email(current_user.email)
    if cust is None:
        return jsonify({"tppMessages": [{"category": "ERROR",
                                         "text": "No synthetic bank customer for this login"}]}), 404
    return jsonify(client.create_consent(cust.customer_id)), 201


@bp.get("/v1/accounts")
def accounts():
    try:
        return jsonify({"accounts": client.get_accounts(request.headers.get("Consent-ID", ""))})
    except client.SynthBankError as exc:
        return _error(exc)


@bp.get("/v1/accounts/<resource_id>/balances")
def balances(resource_id):
    try:
        return jsonify({"balances": client.get_balances(request.headers.get("Consent-ID", ""), resource_id)})
    except client.SynthBankError as exc:
        return _error(exc)


@bp.get("/v1/accounts/<resource_id>/transactions")
def transactions(resource_id):
    try:
        data = client.get_transactions(request.headers.get("Consent-ID", ""), resource_id,
                                       _date("dateFrom"), _date("dateTo"))
        return jsonify({"transactions": data})
    except client.SynthBankError as exc:
        return _error(exc)


@bp.get("/authorise/<consent_id>")
@login_required
def authorise(consent_id):
    """Simulated SCA: shows whose accounts the consent covers; approval posts to /synthbank/callback."""
    try:
        accts = client.get_accounts(consent_id)
    except client.SynthBankError as exc:
        return _error(exc)
    return render_template("synthbank_sca.html", consent_id=consent_id, accounts=accts,
                           owner=accts[0]["ownerName"] if accts else "")
