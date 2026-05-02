from datetime import date

from categorize import categorize
from models import Account, Transaction, db


def upsert_accounts(bank: str, account_list: list) -> list:
    """Insert or update accounts. Returns the saved Account ORM objects."""
    saved = []
    for a in account_list:
        resource_id = a.get("resourceId", "")
        if not resource_id:
            continue
        acc = Account.query.filter_by(bank=bank, resource_id=resource_id).first()
        if acc is None:
            acc = Account(bank=bank, resource_id=resource_id)
            db.session.add(acc)
        acc.iban       = a.get("iban", "")
        acc.currency   = a.get("currency", "")
        acc.name       = a.get("name", "")
        acc.owner_name = a.get("ownerName", "")
        saved.append(acc)
    db.session.commit()
    return saved


def upsert_transactions(bank: str, resource_id: str, txn_data: dict):
    """Insert booked and pending transactions for an account (no duplicates)."""
    acc = Account.query.filter_by(bank=bank, resource_id=resource_id).first()
    if acc is None:
        return

    def _parse_date(s):
        try:
            return date.fromisoformat(s) if s else None
        except ValueError:
            return None

    def _parse_amount(s):
        try:
            return float(s) if s else None
        except (ValueError, TypeError):
            return None

    # Collect existing (booking_date, amount, creditor_name) to avoid dupes
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
