"""
HTTP face of the synthetic bank: a small Berlin Group AIS API plus the
simulated SCA page.

  POST /synthetic-bank/v1/consents?bank=<bank>          create a consent (logged-in demo user)
  GET  /synthetic-bank/v1/accounts                      Consent-ID header
  GET  /synthetic-bank/v1/accounts/<id>/balances        Consent-ID header
  GET  /synthetic-bank/v1/accounts/<id>/transactions    Consent-ID header, dateFrom / dateTo
  GET  /synthetic-bank/authorise/<consent_id>           simulated sign-in and SCA screen

Ground-truth labels are never served (label firewall).
"""
from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from fintnet.synthbank import client
from fintnet.telemetry.logging_config import log
from fintnet.models import SbCustomer, db
from fintnet.synthbank import catalog, store

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
    bank = request.args.get("bank") or (request.get_json(silent=True) or {}).get("bank")
    return jsonify(client.create_consent(cust.customer_id, bank)), 201


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
    """Simulated sign-in and SCA for generated accounts at one bank.

    Lists the accounts the consent covers; a customer with no accounts at that
    bank gets a failed sign-in. Approval posts to /synthbank/callback."""
    try:
        accts = client.get_accounts(consent_id)
        bank = client.bank_for(consent_id)
        cust = db.session.get(SbCustomer, client.customer_id_for(consent_id))
    except client.SynthBankError as exc:
        return _error(exc)
    if cust is None or cust.demo_email != (current_user.email or "").lower():
        return _error(client.SynthBankError("Consent belongs to a different customer", status_code=403))
    log.info("bank.signin", extra={
        "event": "bank.signin.failed" if not accts else "bank.signin.ok", "bank": bank, "data": "generated",
        "customer_id": cust.customer_id, "customer": cust.name, "accounts": len(accts)})
    return render_template("synthbank_sca.html", consent_id=consent_id, accounts=accts, bank=bank,
                           bank_label=catalog.BANK_LABEL.get(bank, "Synthetic Bank"), customer=cust.name)
