"""
Seed script: the 5 demo logins, the synthetic bank and its history.

Each demo login is the test user that exists in one bank's PSD2 sandbox
(Commerzbank, Nordea FI, Nordea SE, ING NL, UniCredit IT), so the live sandbox
connect flow returns the same owner name as the login. Each login also holds
generated accounts with 13 months of transactions at one or more of the banks
FintNet connects (synthbank/catalog.py, DEMO_CUSTOMERS). Nothing is connected
on seed: the user picks which banks to connect.

Beside them, an evaluation population (default 200 customers) gets the same
13 months. Only the evaluation job reads its labels.

Usage:
    python seed_data.py                     # demo logins + population of 200
    python seed_data.py --population 0      # demo logins only (what a cold start runs)
    python seed_data.py --population 50
    python seed_data.py --reset-demo        # rebuild the demo logins' generated accounts and drop
                                            # their FintNet accounts and connections
    ADMIN_PASSWORD=... python seed_data.py --admin-email you@example.com   # operations admin account

Re-runnable: existing users, customers and history are kept; only what is
missing is created. Login password for every demo user: TestPass123.
"""
from __future__ import annotations

import argparse
import time

from werkzeug.security import generate_password_hash

PASSWORD = "TestPass123"


def create_admin(email: str, password: str) -> str:
    """Create or update the admin account that may open the operations view."""
    from app import app
    from models import User, db

    with app.app_context():
        user = User.query.filter_by(email=email.lower()).first()
        if user is None:
            user = User(email=email.lower(), password_hash=generate_password_hash(password), role="tpp_admin")
            db.session.add(user)
            action = "created"
        else:
            user.password_hash, user.role = generate_password_hash(password), "tpp_admin"
            action = "updated"
        db.session.commit()
    print({"admin": email.lower(), "action": action})
    return action


def main(population: int = 200, reset_demo: bool = False) -> dict:
    # Imported here because app imports this module lazily during startup.
    from app import app
    from models import User, db
    from synthbank import catalog, store

    t0 = time.time()
    with app.app_context():
        reset = store.reset_demo() if reset_demo else None
        created = 0
        for demo in catalog.DEMO_CUSTOMERS:
            if not User.query.filter_by(email=demo["email"]).first():
                db.session.add(User(email=demo["email"], password_hash=generate_password_hash(PASSWORD), role="user"))
                created += 1
        db.session.commit()
        seeded = store.seed(population=population)

    result = {"reset_demo": reset, "users_created": created, "synthbank": seeded,
              "seconds": round(time.time() - t0, 1)}
    print(result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--population", type=int, default=200)
    parser.add_argument("--reset-demo", action="store_true")
    parser.add_argument("--admin-email", help="create or update the operations admin; password from ADMIN_PASSWORD")
    args = parser.parse_args()
    if args.admin_email:
        import os
        create_admin(args.admin_email, os.environ["ADMIN_PASSWORD"])
    else:
        main(population=args.population, reset_demo=args.reset_demo)
