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

from datetime import date, datetime, timezone
from typing import Optional

from fintnet.ai.categorize import categorize_many
from fintnet.models import Account, Transaction, db


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


def normalise_iban(iban: Optional[str]) -> str:
    """IBAN without spaces, upper case; empty string for none."""
    return "".join((iban or "").split()).upper()


def own_ibans(user_id: int) -> set[str]:
    """Normalised IBANs of every account this user has in FintNet."""
    return {normalise_iban(a.iban) for a in Account.query.filter_by(user_id=user_id).all() if normalise_iban(a.iban)}


def is_internal(txn: Transaction, own: set[str]) -> bool:
    """True for a transfer between two of the user's own accounts: it moves
    money, so it counts in balances but not in income or spending."""
    return bool(txn.counterparty_iban) and txn.counterparty_iban in own


def upsert_accounts(bank: str, account_list: list[dict], user_id: Optional[int] = None) -> list[Account]:
    """Insert new accounts or refresh existing ones, then return the ORM rows.

    Identity is `(user_id, bank, resource_id)` — `resource_id` is whatever
    the bank's API uses to refer to the account internally. Scoping by user
    lets two demo users connect the same sandbox identity. We store every
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

        acc = Account.query.filter_by(user_id=user_id, bank=bank, resource_id=resource_id).first()
        if acc is None:
            acc = Account(bank=bank, resource_id=resource_id, user_id=user_id)
            db.session.add(acc)

        acc.fetched_at = datetime.now(timezone.utc)   # every sync, so "last synced" is true
        acc.iban       = a.get("iban", "")
        acc.currency   = a.get("currency", "")
        acc.name       = a.get("name", "")
        acc.owner_name = a.get("ownerName", "")
        saved.append(acc)

    db.session.commit()
    return saved


def upsert_transactions(bank: str, resource_id: str, txn_data: dict, user_id: Optional[int] = None) -> int:
    """Insert booked + pending transactions for one account, skipping duplicates.

    `txn_data` is the normalised payload from a bank client and has the
    shape `{"booked": [...], "pending": [...]}`. The parent Account is looked
    up by `(user_id, bank, resource_id)`; if it doesn't exist yet we no-op
    rather than creating an orphan transaction.

    Dedup key: the bank's `transactionId` when it sends one (stored as
    `external_id`), else `(booking_date, amount, creditor_name, status)`.
    The fallback can coalesce two genuine identical payments on the same day,
    which some PSD2 sandboxes force because their IDs change between fetches.

    Categories come from overrides, cache or rules only; rule answers are
    marked provisional and upgraded by the model in the categorise cron job.
    Returns the number of rows inserted.
    """
    q = Account.query.filter_by(bank=bank, resource_id=resource_id)
    if user_id is not None:
        q = q.filter_by(user_id=user_id)
    acc = q.first()
    if acc is None:
        return 0

    existing_ids = {t.external_id for t in acc.transactions if t.external_id}
    existing_keys = {
        (t.booking_date, float(t.amount) if t.amount is not None else None, t.creditor_name, t.status)
        for t in acc.transactions
    }

    decided = categorize_many([t.get("creditorName") or t.get("debtorName") or ""
                               for group in ("booked", "pending") for t in txn_data.get(group, [])])

    inserted = 0
    for status, txns in (("booked", txn_data.get("booked", [])),
                         ("pending", txn_data.get("pending", []))):
        for t in txns:
            booking_date  = _parse_date(t.get("bookingDate"))
            amount        = _parse_amount(t.get("transactionAmount", {}).get("amount"))
            creditor_name = t.get("creditorName", "")
            external_id   = (t.get("transactionId") or "")[:64] or None

            if external_id:
                if external_id in existing_ids:
                    continue
                existing_ids.add(external_id)
            else:
                key = (booking_date, amount, creditor_name, status)
                if key in existing_keys:
                    continue
                existing_keys.add(key)

            # For inbound transactions creditor is empty and the
            # counterparty is the debtor — pick whichever is non-empty
            # so the categorizer has something to work with.
            merchant = creditor_name or t.get("debtorName", "")
            other = t.get("creditorAccount") if (amount or 0) < 0 else t.get("debtorAccount")
            counterparty_iban = normalise_iban((other or {}).get("iban")) or None
            category, source = decided.get(merchant or "", ("Transfers / Other", "rule"))

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
                counterparty_iban=counterparty_iban,
                status=status,
                category=category,
                category_source=source,
                external_id=external_id,
            ))
            inserted += 1
    db.session.commit()
    return inserted
