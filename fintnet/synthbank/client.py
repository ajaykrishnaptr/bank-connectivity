"""
Client for the synthetic bank, shaped like the other *_client.py modules.

The synthetic bank lives inside this app, so the client reads its tables
directly instead of calling itself over HTTP. Responses use Berlin Group
NextGenPSD2 field names, the same shapes `db_utils` already stores for the
real sandboxes. The same payloads are also served over HTTP by
`synthbank/api.py` under /synthetic-bank/v1.

Consents are signed tokens naming one customer and, for demo customers, one
bank: a consent for Commerzbank covers only that customer's Commerzbank
accounts. Ground-truth labels are never read here (label firewall).
"""
from __future__ import annotations

import os
from datetime import date

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select

from fintnet.models import SbAccount, SbCustomer, SbTransaction, db

BANK = "synthbank"


class SynthBankError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(os.getenv("FLASK_SECRET_KEY", "dev-secret"), salt="synthbank-consent")


def create_consent(customer_id: str, bank: str | None = None) -> dict:
    """A Berlin Group style consent response; SCA happens on /synthetic-bank/authorise.
    `bank` limits the consent to the customer's accounts at that bank."""
    payload = {"c": customer_id, "b": bank} if bank else {"c": customer_id}
    consent_id = _serializer().dumps(payload)
    return {"consentStatus": "received", "consentId": consent_id,
            "_links": {"scaRedirect": {"href": f"/synthetic-bank/authorise/{consent_id}"}}}


def _consent(consent_id: str) -> dict:
    try:
        data = _serializer().loads(consent_id)
    except (BadSignature, TypeError) as exc:
        raise SynthBankError("Invalid or unknown consent", status_code=401) from exc
    if not isinstance(data, dict) or "c" not in data:
        raise SynthBankError("Invalid or unknown consent", status_code=401)
    return data


def customer_id_for(consent_id: str) -> str:
    return _consent(consent_id)["c"]


def bank_for(consent_id: str) -> str | None:
    return _consent(consent_id).get("b")


def _money(amount, currency: str) -> dict:
    return {"amount": f"{float(amount):.2f}", "currency": currency}


def get_accounts(consent_id: str) -> list[dict]:
    consent = _consent(consent_id)
    cid, bank = consent["c"], consent.get("b")
    cust = db.session.get(SbCustomer, cid)
    if cust is None:
        raise SynthBankError("Customer not found", status_code=404)
    q = select(SbAccount).where(SbAccount.customer_id == cid)
    if bank:
        q = q.where(SbAccount.bank == bank)
    rows = db.session.scalars(q.order_by(SbAccount.resource_id)).all()
    return [{"resourceId": a.resource_id, "iban": a.iban, "currency": a.currency, "name": a.name,
             "product": a.product, "cashAccountType": a.cash_account_type, "ownerName": cust.name,
             "bic": "SYNB"} for a in rows]


def _owned_account(consent_id: str, resource_id: str) -> SbAccount:
    consent = _consent(consent_id)
    acct = db.session.get(SbAccount, resource_id)
    if acct is None or acct.customer_id != consent["c"] or (consent.get("b") and acct.bank != consent["b"]):
        raise SynthBankError("Account not covered by consent", status_code=403)
    return acct


def get_balances(consent_id: str, resource_id: str) -> list[dict]:
    a = _owned_account(consent_id, resource_id)
    return [{"balanceType": "closingBooked", "balanceAmount": _money(a.balance, a.currency)},
            {"balanceType": "interimAvailable", "balanceAmount": _money(a.balance, a.currency)}]


def get_transactions(consent_id: str, resource_id: str, date_from: date | None = None,
                     date_to: date | None = None) -> dict:
    a = _owned_account(consent_id, resource_id)
    q = select(SbTransaction).where(SbTransaction.resource_id == a.resource_id)
    if date_from:
        q = q.where(SbTransaction.booking_date >= date_from)
    if date_to:
        q = q.where(SbTransaction.booking_date <= date_to)
    booked = []
    for t in db.session.scalars(q.order_by(SbTransaction.booking_date.desc(), SbTransaction.transaction_id)):
        item = {
            "transactionId": t.transaction_id, "bookingDate": t.booking_date.isoformat(),
            "valueDate": (t.value_date or t.booking_date).isoformat(),
            "transactionAmount": _money(t.amount, t.currency),
            "remittanceInformationUnstructured": t.remittance or "",
            "bankTransactionCode": t.bank_transaction_code,
            "balanceAfterTransaction": {"balanceType": "closingBooked",
                                        "balanceAmount": _money(t.balance_after or 0, t.currency)},
        }
        if t.counterparty_iban:
            item["creditorAccount" if float(t.amount) < 0 else "debtorAccount"] = {"iban": t.counterparty_iban}
        for key, value in (("creditorName", t.creditor_name), ("debtorName", t.debtor_name),
                           ("purposeCode", t.purpose_code), ("endToEndId", t.end_to_end_id),
                           ("mandateId", t.mandate_id), ("creditorId", t.creditor_id)):
            if value:
                item[key] = value
        booked.append(item)
    return {"booked": booked, "pending": []}
