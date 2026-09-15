"""
Seed script: the 5 demo logins, the synthetic bank and its history.

Each demo login is the test user that exists in one bank's PSD2 sandbox
(Commerzbank, Nordea FI, Nordea SE, ING NL, UniCredit IT), so the live sandbox
connect flow returns the same owner name as the login. Each login is also a
synthetic bank customer with 13 months of transactions, connected on seed so
the dashboards and the assistant have data before any live connection.

Beside them, an evaluation population (default 200 customers) gets the same
13 months. Only the evaluation job reads its labels.

Usage:
    python seed_data.py                     # demo logins + population of 200
    python seed_data.py --population 0      # demo logins only (what a cold start runs)
    python seed_data.py --population 50

Re-runnable: existing users, customers and history are kept; only what is
missing is created. Login password for every demo user: TestPass123.
"""
from __future__ import annotations

import argparse
import time

from werkzeug.security import generate_password_hash

PASSWORD = "TestPass123"


def main(population: int = 200) -> dict:
    # Imported here because app imports this module lazily during startup.
    from app import _fetch_and_store, app
    import synthbank_client
    from models import BankConnection, User, db
    from synthbank import catalog, store

    t0 = time.time()
    with app.app_context():
        created = 0
        for demo in catalog.DEMO_CUSTOMERS:
            if not User.query.filter_by(email=demo["email"]).first():
                db.session.add(User(email=demo["email"], password_hash=generate_password_hash(PASSWORD), role="user"))
                created += 1
        db.session.commit()

        seeded = store.seed(population=population)

        connected = 0
        for demo in catalog.DEMO_CUSTOMERS:
            user = User.query.filter_by(email=demo["email"]).first()
            if BankConnection.query.filter_by(user_id=user.id, bank=store.BANK).first():
                continue
            consent = synthbank_client.create_consent(demo["customer_id"])
            conn = BankConnection(user_id=user.id, bank=store.BANK, consent_id=consent["consentId"], status="active")
            db.session.add(conn)
            db.session.commit()
            _fetch_and_store(store.BANK, conn)
            connected += 1

    result = {"users_created": created, "synthbank": seeded, "synthbank_connections": connected,
              "seconds": round(time.time() - t0, 1)}
    print(result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--population", type=int, default=200)
    args = parser.parse_args()
    main(population=args.population)
