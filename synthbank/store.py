"""
Database operations for the synthetic bank: seed, daily feed, pruning and
sync into FintNet users who connected the synthetic bank.

History window: rolling 13 months (HISTORY_DAYS). The feed books one day at a
time and is idempotent per date: rerunning a date changes nothing.
"""
from __future__ import annotations

import random
import time
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, insert, select

from models import (Account, BankConnection, SbAccount, SbCustomer, SbLabel, SbTransaction, Transaction, db)

from . import catalog as C
from . import generator as G

HISTORY_DAYS = 395          # rolling 13 months
DEFAULT_POPULATION = 200    # evaluation customers beside the 5 demo logins
BANK = "synthbank"


def _customer_dict(c: SbCustomer) -> dict:
    return {"customer_id": c.customer_id, "name": c.name, "country": c.country,
            "persona": c.persona, "email": c.demo_email}


def ensure_customers(population: int, history_start: date) -> int:
    """Insert demo and population customers plus their accounts if missing. Returns rows added."""
    existing = set(db.session.scalars(select(SbCustomer.customer_id)))
    new = [c for c in G.all_customers(population) if c["customer_id"] not in existing]
    for cust in new:
        db.session.add(SbCustomer(customer_id=cust["customer_id"], name=cust["name"], country=cust["country"],
                                  persona=cust["persona"], demo_email=cust["email"], history_start=history_start))
    db.session.flush()  # customers first: Postgres enforces the accounts' foreign key
    added = 0
    for cust in new:
        rng = random.Random("balance|" + cust["customer_id"])
        for i, acct in enumerate(G.accounts_for(cust)):
            opening = rng.uniform(800, 6000) if i == 0 else rng.uniform(2000, 15000)
            db.session.add(SbAccount(customer_id=cust["customer_id"],
                                     balance=Decimal(str(round(opening * C.COUNTRIES[cust["country"]]["fx"], 2))),
                                     **acct))
        added += 1
    db.session.commit()
    return added


def _book(days: list[date], novel: float, hard: float, run_label: str,
          customers: list[SbCustomer] | None = None) -> dict:
    """Generate and insert transactions for every customer on every day, updating balances."""
    customers = customers or list(db.session.scalars(select(SbCustomer)))
    accounts: dict[str, list[SbAccount]] = {}
    for a in db.session.scalars(select(SbAccount).order_by(SbAccount.resource_id)):
        accounts.setdefault(a.customer_id, []).append(a)

    tx_rows, label_rows = [], []
    slices = {"seen": 0, "novel": 0, "hard": 0}
    for cust in customers:
        cdict = _customer_dict(cust)
        accts = sorted(accounts.get(cust.customer_id, []), key=lambda a: a.cash_account_type != "CACC")
        if not accts:
            continue
        acct_dicts = [{"resource_id": a.resource_id, "currency": a.currency} for a in accts]
        balances = {a.resource_id: Decimal(a.balance) for a in accts}
        plan = G.plan_for(cdict, cust.history_start)
        for day in days:
            for tx, label in G.transactions_for_day(cdict, acct_dicts, plan, day, novel, hard, run_label):
                balances[tx["resource_id"]] += Decimal(str(tx["amount"]))
                tx["balance_after"] = balances[tx["resource_id"]]
                tx_rows.append(tx)
                label_rows.append(label)
                slices[label["slice"]] += 1
        for a in accts:
            a.balance = balances[a.resource_id]

    for i in range(0, len(tx_rows), 5000):
        db.session.execute(insert(SbTransaction.__table__), tx_rows[i:i + 5000])
        db.session.execute(insert(SbLabel.__table__), label_rows[i:i + 5000])
    db.session.commit()
    return {"transactions": len(tx_rows), "customers": len(customers), "slices": slices}


def seed(population: int = DEFAULT_POPULATION, today: date | None = None) -> dict:
    """Create customers and 13 months of history ending yesterday for every customer without history.

    Safe to rerun: customers that already have transactions are skipped, so the
    5 demo customers can be seeded on a cold start and the evaluation
    population added later from the command line.
    """
    today = today or date.today()
    start = today - timedelta(days=HISTORY_DAYS)
    t0 = time.time()
    ensure_customers(population, start)
    with_history = set(db.session.scalars(select(SbTransaction.customer_id).distinct()))
    wanted = {c["customer_id"] for c in G.all_customers(population)}
    todo = [c for c in db.session.scalars(select(SbCustomer))
            if c.customer_id in wanted and c.customer_id not in with_history]
    if not todo:
        return {"skipped": "every customer already has history"}
    days = [start + timedelta(days=i) for i in range((today - start).days)]
    result = _book(days, G.SEED_NOVEL_SHARE, G.SEED_HARD_SHARE, "seed", customers=todo)
    result["seconds"] = round(time.time() - t0, 1)
    return result


def feed_day(day: date) -> dict:
    """Book one day for every customer. Idempotent per date."""
    label = day.isoformat()
    already = db.session.scalar(select(func.count()).select_from(SbTransaction).where(SbTransaction.feed_run == label))
    if already:
        return {"skipped": True, "transactions": already}
    covered = db.session.scalar(select(func.count()).select_from(SbTransaction)
                                .where(SbTransaction.booking_date == day))
    if covered:  # the seed already booked this date
        return {"skipped": True, "transactions": covered, "reason": "booked by seed"}
    return _book([day], G.FEED_NOVEL_SHARE, G.FEED_HARD_SHARE, label)


def feed_until(day: date, max_days: int = 7) -> dict:
    """Book every missing day after the newest booked date up to `day`, at most `max_days` per run.

    Vercel Hobby crons can be skipped; this catches up without gaps.
    """
    newest = db.session.scalar(select(func.max(SbTransaction.booking_date)))
    if newest is None:
        return {"skipped": "no history; run the seed first"}
    days = []
    d = newest + timedelta(days=1)
    while d <= day and len(days) < max_days:
        days.append(d)
        d += timedelta(days=1)
    booked = {d.isoformat(): feed_day(d) for d in days}
    return {"booked_days": list(booked), "results": booked,
            "remaining_days": max(0, (day - (days[-1] if days else newest)).days)}


def prune(today: date | None = None) -> dict:
    """Delete synthetic history older than the rolling window, in the bank and in FintNet."""
    cutoff = (today or date.today()) - timedelta(days=HISTORY_DAYS)
    old_ids = select(SbTransaction.transaction_id).where(SbTransaction.booking_date < cutoff)
    labels = db.session.execute(delete(SbLabel).where(SbLabel.transaction_id.in_(old_ids))).rowcount
    txns = db.session.execute(delete(SbTransaction).where(SbTransaction.booking_date < cutoff)).rowcount
    acct_ids = select(Account.id).where(Account.bank == BANK)
    fintnet = db.session.execute(delete(Transaction).where(Transaction.account_id.in_(acct_ids),
                                                           Transaction.booking_date < cutoff)).rowcount
    db.session.commit()
    return {"cutoff": cutoff.isoformat(), "labels": labels, "transactions": txns, "fintnet_transactions": fintnet}


def customer_for_email(email: str) -> SbCustomer | None:
    return db.session.scalar(select(SbCustomer).where(SbCustomer.demo_email == (email or "").lower()))


def sync_all_connections(sync_fn) -> dict:
    """Re-sync every active synthetic bank connection through `sync_fn(conn)`."""
    conns = db.session.scalars(select(BankConnection).where(BankConnection.bank == BANK,
                                                            BankConnection.status == "active")).all()
    import synthbank_client

    from models import User

    synced, reissued = 0, 0
    for conn in conns:
        try:
            synthbank_client.customer_id_for(conn.consent_id)
        except synthbank_client.SynthBankError:
            # Consent signed with a different secret (for example seeded locally):
            # re-issue it for the demo customer that belongs to this login.
            user = db.session.get(User, conn.user_id)
            cust = customer_for_email(user.email if user else "")
            if cust is None:
                continue
            conn.consent_id = synthbank_client.create_consent(cust.customer_id)["consentId"]
            db.session.commit()
            reissued += 1
        sync_fn(conn)
        synced += 1
    return {"connections": synced, "consents_reissued": reissued}
