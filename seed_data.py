"""
Seed test users, bank connections, accounts, and 6 months of transactions.
Safe to re-run — clears and recreates test data on each execution.
"""
import random
from datetime import date, timedelta

random.seed(42)

from werkzeug.security import generate_password_hash

from app import app
from models import Account, BankConnection, Transaction, User, db

TODAY = date(2026, 5, 2)
PASSWORD = "TestPass123"

# ── User + bank connection definitions ───────────────────────────────────────

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
        },
    },
    {
        "email":      "arjun.mehta@testbank.eu",
        "owner_name": "Arjun Mehta",
        "salary":     ("SAP SE",            38000, 44000),   # SEK salary (~3500–4000 EUR)
        "banks": {
            "nordea": {
                "access_token": "seeded-nordea-arjun",
                "currency": "SEK",
                "fx": 11.2,   # approximate EUR→SEK rate used for seeding
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

# ── Transaction data ──────────────────────────────────────────────────────────

# Fixed recurring: same amount, same day each month → detected as fixed recurring
RECURRING = [
    # (merchant, amount, day, category)
    ("Netflix",                15.99,  28, "Entertainment"),
    ("Spotify",                 9.99,  15, "Entertainment"),
    ("Disney+",                 8.99,  20, "Entertainment"),
    ("Deutsche Telekom",       39.99,   1, "Utilities"),
    ("Vattenfall",             85.00,   3, "Utilities"),
    ("TK Krankenkasse",       349.00,  10, "Health"),
]

RENT = ("Immobilien GmbH", 880.00, 1, "Housing")

# Variable: random amount each month → detected as variable recurring
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

EXTRA_INCOME = [
    ("Freelance Transfer",  200,  800),
    ("PayPal Transfer",      50,  300),
    ("Kleinanzeigen Sale",   30,  150),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _rand_date(year, month):
    first = date(year, month, 1)
    last  = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return first + timedelta(days=random.randint(0, (last - first).days))


def _last_6_months():
    months, y, m = [], TODAY.year, TODAY.month
    for _ in range(6):
        months.insert(0, (y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return months


def _txn(bank, account_id, d, amount, creditor, debtor, info, category, currency="EUR", fx=1.0):
    return Transaction(
        bank=bank, account_id=account_id,
        booking_date=d, value_date=d,
        amount=round(amount * fx, 2), currency=currency,
        creditor_name=creditor, debtor_name=debtor,
        remittance_info=info, status="booked", category=category,
    )


def generate_current(bank, account_id, salary, currency="EUR", fx=1.0):
    """Generate 6 months of realistic transactions for a current account."""
    sal_name, sal_min, sal_max = salary
    txns = []

    for year, month in _last_6_months():

        # Salary (1st–5th)
        d = date(year, month, random.randint(1, 5))
        if d <= TODAY:
            txns.append(_txn(bank, account_id, d,
                             random.uniform(sal_min, sal_max),
                             "", sal_name, "Monthly salary", "Income",
                             currency=currency, fx=1.0))  # salary already in native currency

        # Rent (1st)
        d = date(year, month, 1)
        if d <= TODAY:
            merchant, base, _, category = RENT
            txns.append(_txn(bank, account_id, d,
                             -random.uniform(base - 30, base + 30),
                             merchant, "", f"Hyra {year}/{month:02d}" if currency != "EUR" else f"Miete {year}/{month:02d}",
                             category, currency=currency, fx=fx))

        # Fixed recurring
        for merchant, amount, day, category in RECURRING:
            d = date(year, month, min(day, 28))
            if d <= TODAY:
                txns.append(_txn(bank, account_id, d, -amount,
                                 merchant, "", "", category, currency=currency, fx=fx))

        # Variable expenses (14–22 per month)
        for _ in range(random.randint(14, 22)):
            merchant, category, mn, mx = random.choice(VARIABLE)
            d = _rand_date(year, month)
            if d <= TODAY:
                txns.append(_txn(bank, account_id, d,
                                 -random.uniform(mn, mx),
                                 merchant, "", "", category, currency=currency, fx=fx))

        # ~30% chance of extra income
        if random.random() < 0.30:
            name, mn, mx = random.choice(EXTRA_INCOME)
            d = _rand_date(year, month)
            if d <= TODAY:
                txns.append(_txn(bank, account_id, d,
                                 random.uniform(mn, mx),
                                 "", name, "", "Income", currency=currency, fx=fx))

    return txns


def generate_savings(bank, account_id, currency="EUR", fx=1.0):
    """Generate monthly deposits into a savings account."""
    txns = []
    for year, month in _last_6_months():
        d = date(year, month, min(random.randint(5, 12), 28))
        if d <= TODAY:
            txns.append(_txn(bank, account_id, d,
                             random.uniform(200, 600),
                             "", "Own Transfer", "Monthly savings deposit", "Transfer",
                             currency=currency, fx=fx))
    return txns


# ── Seed ─────────────────────────────────────────────────────────────────────

with app.app_context():
    # Clean up existing test users and their data
    for u_data in USERS:
        user = User.query.filter_by(email=u_data["email"]).first()
        if user:
            for acc in Account.query.filter_by(user_id=user.id).all():
                db.session.delete(acc)       # cascades to transactions
            for conn in BankConnection.query.filter_by(user_id=user.id).all():
                db.session.delete(conn)
            db.session.delete(user)
    db.session.commit()
    print("Cleared existing test data.\n")

    total_txns = 0

    for u_data in USERS:
        # Create user
        user = User(
            email=u_data["email"],
            password_hash=generate_password_hash(PASSWORD),
            role="user",
        )
        db.session.add(user)
        db.session.flush()
        print(f"User: {u_data['owner_name']} ({u_data['email']})")

        for bank, bank_data in u_data["banks"].items():
            # Create BankConnection
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
