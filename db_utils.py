"""
Persistence helpers for the data we get back from bank PSD2 APIs.

Both functions are idempotent: re-running fetch-all on the same day
should not create duplicate rows. Each bank client returns a
normalised dict shape; these helpers turn that shape into ORM rows.

Why two functions instead of one? Accounts are fetched first (we need
the row IDs before transactions can reference them via account_id),
and they appear once per consent. Transactions are fetched per
account and can be many thousands per call, so they get their own
dedup pass.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from categorize import categorize
from models import Account, Transaction, db


def _parse_date(s: Optional[str]) -> Optional[date]:
    """Banks send ISO-8601 date strings; tolerate empty/missing values."""
    try:
        return date.fromisoformat(s) if s else None
    except (ValueError, TypeError):
        return None


def _parse_amount(s: Optional[str]) -> Optional[float]:
    """Bank API amounts arrive as strings ("12.34"). Returns None on
    anything we can't parse so the caller can decide what to do."""
    try:
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def upsert_accounts(bank: str, account_list: list[dict], user_id: Optional[int] = None) -> list[Account]:
    """Insert new accounts or refresh existing ones, then return the ORM rows.

    Identity is `(bank, resource_id)` — `resource_id` is whatever the
    bank's API uses to refer to the account internally. We store every
    other field verbatim and overwrite on each call so renames at the
    bank propagate without manual intervention.

    Accounts without a `resourceId` are silently skipped: there's no
    way to look them up later so persisting them would be useless.
    """
    saved: list[Account] = []
    for a in account_list:
        resource_id = a.get("resourceId", "")
        if not resource_id:
            continue

        acc = Account.query.filter_by(bank=bank, resource_id=resource_id).first()
        if acc is None:
            acc = Account(bank=bank, resource_id=resource_id, user_id=user_id)
            db.session.add(acc)

        acc.iban       = a.get("iban", "")
        acc.currency   = a.get("currency", "")
        acc.name       = a.get("name", "")
        acc.owner_name = a.get("ownerName", "")
        saved.append(acc)

    db.session.commit()
    return saved


def upsert_transactions(bank: str, resource_id: str, txn_data: dict) -> None:
    """Insert booked + pending transactions for one account, skipping duplicates.

    `txn_data` is the normalised payload from a bank client and has the
    shape `{"booked": [...], "pending": [...]}`. We look up the parent
    Account by `(bank, resource_id)`; if it doesn't exist yet (caller
    didn't run upsert_accounts first), we silently no-op rather than
    creating an orphan transaction.

    The dedup key is `(booking_date, amount, creditor_name, status)`.
    That can theoretically collide — two genuine identical-amount
    payments to the same merchant on the same day get coalesced into
    one row — but in practice it's rare enough, and the alternative
    (using the bank's transaction ID) is unreliable across re-fetches
    for some PSD2 sandboxes.
    """
    acc = Account.query.filter_by(bank=bank, resource_id=resource_id).first()
    if acc is None:
        return

    existing = {
        (t.booking_date, t.amount, t.creditor_name, t.status)
        for t in acc.transactions
    }

    for status, txns in (("booked", txn_data.get("booked", [])),
                         ("pending", txn_data.get("pending", []))):
        for t in txns:
            booking_date  = _parse_date(t.get("bookingDate"))
            amount        = _parse_amount(t.get("transactionAmount", {}).get("amount"))
            creditor_name = t.get("creditorName", "")

            key = (booking_date, amount, creditor_name, status)
            if key in existing:
                continue
            existing.add(key)

            # For inbound transactions creditor is empty and the
            # counterparty is the debtor — pick whichever is non-empty
            # so the categorizer has something to work with.
            merchant = creditor_name or t.get("debtorName", "")

            db.session.add(Transaction(
                bank=bank,
                account_id=acc.id,
                booking_date=booking_date,
                value_date=_parse_date(t.get("valueDate")),
                amount=amount,
                currency=t.get("transactionAmount", {}).get("currency", ""),
                creditor_name=creditor_name,
                debtor_name=t.get("debtorName", ""),
                remittance_info=t.get("remittanceInformationUnstructured", ""),
                status=status,
                category=categorize(merchant),
            ))
    db.session.commit()
