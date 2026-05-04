"""
Seed script — populates the local SQLite DB with three test users
across multiple banks, each with 6 months of synthetic transactions.

Re-runnable: every existing row for the seeded users is deleted first
so you always end up with a clean snapshot. The RNG is seeded with a
fixed value so the output is identical run-to-run, which keeps the
dashboard screenshots stable for the README.

Usage:
    python3 seed_data.py

After running:
    Login at http://localhost:5000/login with any of the seeded
    emails and password `TestPass123`. See the USERS list below for
    the available emails.
"""
import random
from datetime import date, timedelta

# Deterministic synthetic data — change this to vary the dataset, but
# the README screenshots assume seed=42.
random.seed(42)

from werkzeug.security import generate_password_hash

from app import app
from models import Account, BankConnection, Transaction, User, db


# "Today" for the seeded data. Hard-coded so the relative dates in
# screenshots don't shift as wall-clock time advances.
TODAY    = date(2026, 5, 2)
PASSWORD = "TestPass123"


# ── User + bank connection definitions ───────────────────────────────────────
# Each entry describes one test user and which banks they have. Bank
# blocks accept either `access_token` (Nordea/ING) or `consent_id`
# (Commerzbank/UniCredit/DB) — whichever the bank actually uses. The
# values are placeholder strings; real auth happens on the live bank
# flow, not here.
#
# Optional bank-level fields:
#   currency: ISO 4217 — used for foreign-currency accounts (Arjun's SEK).
#   fx:       multiplier applied to the seeded EUR amounts to produce
#             native-currency figures, so 100 EUR rent at fx=11.2 =>
#             1120 SEK. Salary is exempt — see `generate_current`.

USERS = [
    {
        "email":      "priya.sharma@testbank.eu",
        "owner_name": "Priya Sharma",
        "salary":     ("Siemens AG",        3900, 4500),
        "banks": {
            "nordea": {
                "access_token": "seeded-nordea-priya",
                "accounts": [
                    {"resource_id": "N-PRIYA-CUR", "iban": "FI4350001520660004",
                     "name": "Current Account", "currency": "EUR"},
                    {"resource_id": "N-PRIYA-SAV", "iban": "FI2112345600000785",
                     "name": "Savings Account",  "currency": "EUR"},
                ],
            },
            "commerzbank": {
                "consent_id": "seeded-cb-priya",
                "accounts": [
                    {"resource_id": "CB-PRIYA-CUR", "iban": "DE89370400440532013000",
                     "name": "Girokonto", "currency": "EUR"},
                    {"resource_id": "CB-PRIYA-SAV", "iban": "DE27100777770209299700",
                     "name": "Sparkonto", "currency": "EUR"},
                ],
            },
            "ing": {
                "access_token": "seeded-ing-priya",
                "accounts": [
                    {"resource_id": "ING-PRIYA-CUR", "iban": "NL91INGB0002445588",
                     "name": "Betaalrekening", "currency": "EUR"},
                    {"resource_id": "ING-PRIYA-SAV", "iban": "NL18INGB0009876541",
                     "name": "Oranje Spaarrekening", "currency": "EUR"},
                ],
            },
        },
    },
    {
        "email":      "arjun.mehta@testbank.eu",
        "owner_name": "Arjun Mehta",
        "salary":     ("SAP SE",            38000, 44000),   # Arjun is paid in SEK.
        "banks": {
            "nordea": {
                "access_token": "seeded-nordea-arjun",
                "currency": "SEK",
                "fx": 11.2,   # rough EUR -> SEK rate at seed time
                "accounts": [
                    {"resource_id": "N-ARJUN-CUR", "iban": "SE3550000000054910000003",
                     "name": "Lönekonto", "currency": "SEK"},
                    {"resource_id": "N-ARJUN-SAV", "iban": "SE6780000810340967640001",
                     "name": "Sparkonto",  "currency": "SEK"},
                ],
            },
        },
    },
    {
        "email":      "kavya.reddy@testbank.eu",
        "owner_name": "Kavya Reddy",
        "salary":     ("Deutsche Bank AG",  4100, 4700),
        "banks": {
            "commerzbank": {
                "consent_id": "seeded-cb-kavya",
                "accounts": [
                    {"resource_id": "CB-KAVYA-CUR", "iban": "DE75512108001245126199",
                     "name": "Girokonto", "currency": "EUR"},
                    {"resource_id": "CB-KAVYA-SAV", "iban": "DE02300606010002474689",
                     "name": "Sparkonto", "currency": "EUR"},
                ],
            },
        },
    },
]


# ── Synthetic transaction templates ──────────────────────────────────────────

# Fixed recurring: same amount on the same day each month. Detected as
# "fixed" by `_detect_recurring` because the CV is zero.
RECURRING = [
    # (merchant, amount, day, category)
    ("Netflix",                15.99,  28, "Entertainment"),
    ("Spotify",                 9.99,  15, "Entertainment"),
    ("Disney+",                 8.99,  20, "Entertainment"),
    ("Deutsche Telekom",       39.99,   1, "Utilities"),
    ("Vattenfall",             85.00,   3, "Utilities"),
    ("TK Krankenkasse",       349.00,  10, "Health"),
]

# Rent is special: roughly fixed but with ±30 EUR jitter to avoid
# being too clean (also makes the "fixed vs variable" classifier
# borderline-interesting).
RENT = ("Immobilien GmbH", 880.00, 1, "Housing")

# Variable: random amount each month inside [min, max]. Detected as
# "variable recurring" — same merchant repeats, but CV is high enough
# to flag the charge as not-a-subscription.
VARIABLE = [
    # (merchant, category, min, max)
    ("Lidl",           "Groceries",    15,  85),
    ("REWE",           "Groceries",    20,  95),
    ("Aldi",           "Groceries",    10,  60),
    ("Carrefour",      "Groceries",    18,  70),
    ("McDonald's",     "Food & Drink",  5,  22),
    ("Starbucks",      "Food & Drink",  4,  16),
    ("Lieferando",     "Food & Drink", 12,  48),
    ("Burger King",    "Food & Drink",  6,  20),
    ("Vapiano",        "Food & Drink", 14,  38),
    ("L'Osteria",      "Food & Drink", 18,  45),
    ("Deutsche Bahn",  "Transport",    18, 130),
    ("BVG",            "Transport",     3,  32),
    ("Flixbus",        "Transport",    12,  49),
    ("Lufthansa",      "Transport",    89, 340),
    ("H&M",            "Shopping",     22, 120),
    ("Zalando",        "Shopping",     35, 185),
    ("MediaMarkt",     "Shopping",     28, 320),
    ("IKEA",           "Shopping",     35, 260),
    ("Zara",           "Shopping",     30, 140),
    ("DocMorris",      "Health",       10,  65),
    ("dm Drogerie",    "Health",        8,  48),
    ("Rossmann",       "Health",        6,  40),
]

# Occasional inflows that aren't salary. Probability is set per-month
# in `generate_current`.
EXTRA_INCOME = [
    ("Freelance Transfer",  200,  800),
    ("PayPal Transfer",      50,  300),
    ("Kleinanzeigen Sale",   30,  150),
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _rand_date(year: int, month: int) -> date:
    """Return a uniformly-random date inside the given calendar month."""
    first = date(year, month, 1)
    last  = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return first + timedelta(days=random.randint(0, (last - first).days))


def _last_6_months() -> list[tuple[int, int]]:
    """The six (year, month) tuples ending with `TODAY`'s month, in
    chronological order. Same trick the dashboard uses."""
    months: list[tuple[int, int]] = []
    y, m = TODAY.year, TODAY.month
    for _ in range(6):
        months.insert(0, (y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return months


def _txn(bank: str, account_id: int, d: date, amount: float,
         creditor: str, debtor: str, info: str, category: str,
         currency: str = "EUR", fx: float = 1.0) -> Transaction:
    """Build (but don't add) one Transaction row. Amounts are rounded
    to two decimals and multiplied by `fx` so EUR templates can be
    reused for SEK accounts."""
    return Transaction(
        bank=bank, account_id=account_id,
        booking_date=d, value_date=d,
        amount=round(amount * fx, 2), currency=currency,
        creditor_name=creditor, debtor_name=debtor,
        remittance_info=info, status="booked", category=category,
    )


def generate_current(bank: str, account_id: int, salary: tuple,
                     currency: str = "EUR", fx: float = 1.0) -> list[Transaction]:
    """6 months of realistic transactions for a current account.

    Per month: salary (1st-5th), rent (1st), every fixed recurring,
    14-22 variable expenses, and a 30% chance of one extra income.
    Salary is excluded from `fx` because the seeded salary range is
    already in the user's native currency (see Arjun's SEK row).
    """
    sal_name, sal_min, sal_max = salary
    txns: list[Transaction] = []

    for year, month in _last_6_months():

        # Salary lands in the first 5 days of the month.
        d = date(year, month, random.randint(1, 5))
        if d <= TODAY:
            txns.append(_txn(bank, account_id, d,
                             random.uniform(sal_min, sal_max),
                             "", sal_name, "Monthly salary", "Income",
                             currency=currency, fx=1.0))

        # Rent on the 1st.
        d = date(year, month, 1)
        if d <= TODAY:
            merchant, base, _, category = RENT
            info = (f"Hyra {year}/{month:02d}" if currency != "EUR"
                    else f"Miete {year}/{month:02d}")
            txns.append(_txn(bank, account_id, d,
                             -random.uniform(base - 30, base + 30),
                             merchant, "", info, category,
                             currency=currency, fx=fx))

        # Fixed recurring (subscriptions, fixed utilities, insurance).
        for merchant, amount, day, category in RECURRING:
            d = date(year, month, min(day, 28))   # clip to 28 for Feb
            if d <= TODAY:
                txns.append(_txn(bank, account_id, d, -amount,
                                 merchant, "", "", category,
                                 currency=currency, fx=fx))

        # Variable expenses scattered through the month.
        for _ in range(random.randint(14, 22)):
            merchant, category, mn, mx = random.choice(VARIABLE)
            d = _rand_date(year, month)
            if d <= TODAY:
                txns.append(_txn(bank, account_id, d,
                                 -random.uniform(mn, mx),
                                 merchant, "", "", category,
                                 currency=currency, fx=fx))

        # Occasional extra income (~30% of months).
        if random.random() < 0.30:
            name, mn, mx = random.choice(EXTRA_INCOME)
            d = _rand_date(year, month)
            if d <= TODAY:
                txns.append(_txn(bank, account_id, d,
                                 random.uniform(mn, mx),
                                 "", name, "", "Income",
                                 currency=currency, fx=fx))

    return txns


def generate_savings(bank: str, account_id: int,
                     currency: str = "EUR", fx: float = 1.0) -> list[Transaction]:
    """Monthly deposits into a savings account. Amount is jittered
    between 200-600 EUR (scaled by `fx` for non-EUR accounts)."""
    txns: list[Transaction] = []
    for year, month in _last_6_months():
        d = date(year, month, min(random.randint(5, 12), 28))
        if d <= TODAY:
            txns.append(_txn(bank, account_id, d,
                             random.uniform(200, 600),
                             "", "Own Transfer", "Monthly savings deposit", "Transfer",
                             currency=currency, fx=fx))
    return txns


# ── Seed entry point ─────────────────────────────────────────────────────────

def main() -> None:
    """Wipe seeded users + their data, then re-create everything."""
    with app.app_context():
        # Step 1: clean up any previous run for these users. We don't
        # use raw `delete()` because we want SQLAlchemy's cascade to
        # take care of accounts → transactions.
        for u_data in USERS:
            user = User.query.filter_by(email=u_data["email"]).first()
            if user:
                for acc in Account.query.filter_by(user_id=user.id).all():
                    db.session.delete(acc)   # cascades to transactions
                for conn in BankConnection.query.filter_by(user_id=user.id).all():
                    db.session.delete(conn)
                db.session.delete(user)
        db.session.commit()
        print("Cleared existing test data.\n")

        total_txns = 0

        for u_data in USERS:
            user = User(
                email=u_data["email"],
                password_hash=generate_password_hash(PASSWORD),
                role="user",
            )
            db.session.add(user)
            # `flush` so we get user.id back without committing yet.
            db.session.flush()
            print(f"User: {u_data['owner_name']} ({u_data['email']})")

            for bank, bank_data in u_data["banks"].items():
                conn = BankConnection(
                    user_id=user.id,
                    bank=bank,
                    access_token=bank_data.get("access_token"),
                    consent_id=bank_data.get("consent_id"),
                    status="active",
                )
                db.session.add(conn)

                bank_currency = bank_data.get("currency", "EUR")
                bank_fx       = bank_data.get("fx", 1.0)

                # By convention the first account in each bank's list is the
                # current account (gets salary + variable spend); the rest
                # are savings accounts (just monthly deposits).
                for i, acc_data in enumerate(bank_data["accounts"]):
                    acc = Account(
                        bank=bank,
                        resource_id=acc_data["resource_id"],
                        iban=acc_data["iban"],
                        currency=acc_data["currency"],
                        name=acc_data["name"],
                        owner_name=u_data["owner_name"],
                        user_id=user.id,
                    )
                    db.session.add(acc)
                    db.session.flush()

                    is_current = i == 0
                    if is_current:
                        txns = generate_current(bank, acc.id, u_data["salary"],
                                                currency=bank_currency, fx=bank_fx)
                    else:
                        txns = generate_savings(bank, acc.id,
                                                currency=bank_currency, fx=bank_fx)

                    db.session.add_all(txns)
                    total_txns += len(txns)
                    print(f"  [{bank.capitalize()}] {acc_data['name']}: {len(txns)} transactions")

            print()

        db.session.commit()
        print(f"Done. {total_txns} transactions seeded across {len(USERS)} users.")


if __name__ == "__main__":
    main()
