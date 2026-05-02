"""Generate and insert realistic multi-month transaction data for HDFC and SBI sandbox accounts."""
import random
from datetime import date, timedelta

random.seed(42)

from app import app
from categorize import categorize
from models import Account, Transaction, db

ACCOUNTS_DATA = [
    dict(bank="hdfc", resource_id="HDFC001", iban="HDFC0001234567890",
         currency="EUR", name="Savings Account", owner_name="Arjun Mehta"),
    dict(bank="hdfc", resource_id="HDFC002", iban="HDFC0009876543210",
         currency="EUR", name="Current Account", owner_name="Priya Sharma"),
    dict(bank="sbi",  resource_id="SBI001",  iban="SBI00001122334455",
         currency="EUR", name="Salary Account", owner_name="Rohan Verma"),
]

# (merchant, min_amount, max_amount)
EXPENSES = [
    ("DMart",                  30,  120),
    ("Big Bazaar",             20,   80),
    ("Reliance Fresh",         10,   50),
    ("Swiggy",                  8,   40),
    ("Zomato",                 10,   35),
    ("Ola Cabs",                5,   30),
    ("Uber India",              8,   45),
    ("IRCTC",                  25,  150),
    ("IndiGo Airlines",        80,  300),
    ("Flipkart",               20,  200),
    ("Amazon India",           15,  180),
    ("Myntra",                 30,  120),
    ("BookMyShow",             15,   60),
    ("Netflix India",          15,   15),
    ("Hotstar Premium",        10,   10),
    ("Jio Fiber",              25,   25),
    ("BSES Delhi",             30,   80),
    ("Apollo Pharmacy",        10,   80),
    ("Medplus Pharmacy",       10,   60),
    ("Café Coffee Day",         5,   20),
    ("McDonald's India",        8,   25),
    ("KFC India",              10,   30),
    ("Starbucks India",         6,   18),
    ("Domino's India",         12,   35),
    ("Cross Sports Club",      30,   30),
    ("Rahul Gupta",            20,  200),
    ("Sneha Patel",            15,  150),
    ("Vikram Singh",           50,  300),
    ("Neha Kapoor",            10,  100),
    ("Ananya Iyer",            25,  120),
    ("Deepak Nair",            30,  200),
    ("Kavitha Krishnan",       15,   80),
    ("Ravi Subramanian",       20,  100),
    ("Pooja Agarwal",          10,   90),
    ("Sunita Rao",             15,  130),
]

SALARY_BY_ACCOUNT = {
    "HDFC001": ("Tata Consultancy Services", 2800, 3200),
    "HDFC002": ("Infosys Ltd",               2400, 2700),
    "SBI001":  ("Wipro Ltd",                 2100, 2500),
}

EXTRA_INCOME = [
    ("Rahul Gupta",         50,  200),
    ("Ananya Iyer",         30,  150),
    ("Freelance - Web Dev", 150, 500),
    ("Deepak Nair",         40,  180),
]


def _rand_date(year, month):
    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    delta = (last - first).days
    return first + timedelta(days=random.randint(0, delta))


def _last_n_months(n, today=date(2026, 5, 2)):
    months = []
    y, m = today.year, today.month
    for _ in range(n):
        months.insert(0, (y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return months


def generate(account_id, resource_id, bank, today=date(2026, 5, 2)):
    txns = []
    salary_name, sal_min, sal_max = SALARY_BY_ACCOUNT[resource_id]

    for year, month in _last_n_months(6, today):
        # Salary (1st–5th of month)
        sal_date = date(year, month, random.randint(1, 5))
        if sal_date <= today:
            txns.append(Transaction(
                bank=bank, account_id=account_id,
                booking_date=sal_date, value_date=sal_date,
                amount=round(random.uniform(sal_min, sal_max), 2),
                currency="EUR", creditor_name="", debtor_name=salary_name,
                remittance_info="Monthly salary", status="booked", category="Income",
            ))

        # 18–28 expenses spread across the month
        n_exp = random.randint(18, 28)
        for _ in range(n_exp):
            merchant, mn, mx = random.choice(EXPENSES)
            d = _rand_date(year, month)
            if d > today:
                continue
            txns.append(Transaction(
                bank=bank, account_id=account_id,
                booking_date=d, value_date=d,
                amount=-round(random.uniform(mn, mx), 2),
                currency="EUR", creditor_name=merchant, debtor_name="",
                remittance_info="", status="booked", category=categorize(merchant),
            ))

        # ~30% chance of extra income (freelance / transfer)
        if random.random() < 0.35:
            name, mn, mx = random.choice(EXTRA_INCOME)
            d = _rand_date(year, month)
            if d <= today:
                txns.append(Transaction(
                    bank=bank, account_id=account_id,
                    booking_date=d, value_date=d,
                    amount=round(random.uniform(mn, mx), 2),
                    currency="EUR", creditor_name="", debtor_name=name,
                    remittance_info="", status="booked", category="Income",
                ))

    return txns


with app.app_context():
    total_txns = 0
    for adata in ACCOUNTS_DATA:
        acc = Account.query.filter_by(bank=adata["bank"], resource_id=adata["resource_id"]).first()
        if acc is None:
            acc = Account(**adata)
            db.session.add(acc)
            db.session.flush()
            print(f"  Created: {adata['owner_name']} ({adata['bank']})")
        else:
            # Wipe existing so re-runs are idempotent
            Transaction.query.filter_by(account_id=acc.id).delete()

        txns = generate(acc.id, adata["resource_id"], adata["bank"])
        db.session.add_all(txns)
        total_txns += len(txns)
        print(f"  {adata['owner_name']}: {len(txns)} transactions")

    db.session.commit()
    print(f"\nSeeded {total_txns} transactions across {len(ACCOUNTS_DATA)} accounts.")
