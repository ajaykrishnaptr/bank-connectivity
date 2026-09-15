"""
Deterministic generator for the synthetic bank.

Pure functions, no database. Given a customer and a calendar day it returns
Berlin Group shaped transactions plus a separate label per transaction.

Determinism: every random draw comes from random.Random seeded with the
customer ID and the ISO date, so generating the same day twice gives the same
rows (idempotent feed) and different days differ.

"Random category" is handled so ground truth stays valid: the category is
drawn first (weighted by persona), then a merchant that truly belongs to that
category is chosen or created. A random label is never attached to an
existing merchant.
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import date, timedelta

from . import catalog as C

# Merchant mix for the daily feed (plan: about 60 / 30 / 10).
FEED_NOVEL_SHARE = 0.30
FEED_HARD_SHARE = 0.10
# History uses mostly known merchants so the seed does not create tens of
# thousands of never-seen merchants nobody will categorise.
SEED_NOVEL_SHARE = 0.05
SEED_HARD_SHARE = 0.05

_LEGAL = {"DE": ["GmbH", "e.K.", "UG"], "FI": ["Oy", "Ky"], "SE": ["AB", "HB"],
          "NL": ["BV", "VOF"], "IT": ["Srl", "Snc", "SpA"]}


# ── IBANs ────────────────────────────────────────────────────────────────────

def _mod97_check(country: str, bban: str) -> str:
    rearranged = bban + country + "00"
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return f"{98 - int(digits) % 97:02d}"


def make_iban(country: str, n: int) -> str:
    code = C.COUNTRIES[country]["bank_code"]
    if country == "DE":
        bban = code + f"{n:010d}"
    elif country == "FI":
        bban = code + f"{n:08d}"
    elif country == "SE":
        bban = code + f"{n:017d}"
    elif country == "NL":
        bban = code + f"{n:010d}"
    else:  # IT: CIN + ABI + CAB + account
        bban = "X" + code + "01600" + f"{n:012d}"
    return country + _mod97_check(country, bban) + bban


# ── Customers ────────────────────────────────────────────────────────────────

def _num(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest()[:10], 16)


def build_population(size: int) -> list[dict]:
    """Deterministic evaluation population spread over the 5 countries."""
    rng = random.Random(42)
    countries = list(C.COUNTRIES)
    personas = ["salaried", "salaried", "family", "student", "pensioner", "freelancer"]
    out = []
    for i in range(size):
        country = countries[i % len(countries)]
        out.append({
            "customer_id": f"POP-{country}-{i:04d}",
            "name": f"{rng.choice(C.FIRST[country])} {rng.choice(C.LAST[country])}",
            "country": country,
            "persona": rng.choice(personas),
            "email": None,
        })
    return out


def all_customers(population: int) -> list[dict]:
    demo = [{k: d[k] for k in ("customer_id", "name", "country", "persona", "email")} for d in C.DEMO_CUSTOMERS]
    return demo + build_population(population)


def accounts_for(customer: dict) -> list[dict]:
    country = customer["country"]
    cur = C.COUNTRIES[country]["currency"]
    base = _num(customer["customer_id"]) % 10**9
    accts = [{"resource_id": f"SB-{customer['customer_id']}-CUR", "iban": make_iban(country, base),
              "currency": cur, "name": "Current account", "product": "Synthetic Current Account",
              "cash_account_type": "CACC"}]
    if customer.get("email"):  # demo customers also hold a savings account
        accts.append({"resource_id": f"SB-{customer['customer_id']}-SAV", "iban": make_iban(country, base + 1),
                      "currency": cur, "name": "Savings account", "product": "Synthetic Savings Account",
                      "cash_account_type": "SVGS"})
    return accts


def plan_for(customer: dict, history_start: date) -> dict:
    """The customer's recurring life, fixed for their whole history."""
    rng = random.Random("plan|" + customer["customer_id"])
    country, persona = customer["country"], customer["persona"]
    known = C.KNOWN[country]
    demo = next((d for d in C.DEMO_CUSTOMERS if d["customer_id"] == customer["customer_id"]), None)

    if persona == "pensioner":
        income = {"payer": C.PENSION_PAYERS[country], "amount": rng.uniform(1500, 2600), "day": 1,
                  "purpose": "PENS", "label": "Pension"}
    elif persona == "student":
        income = {"payer": rng.choice(C.EMPLOYERS[country]), "amount": rng.uniform(700, 1200), "day": 28,
                  "purpose": "SALA", "label": "Salary"}
    elif persona == "freelancer":
        income = None
    else:
        income = {"payer": demo["employer"] if demo else rng.choice(C.EMPLOYERS[country]),
                  "amount": rng.uniform(3200, 5400) * (1.15 if persona == "family" else 1.0),
                  "day": 25 if country == "SE" else 27, "purpose": "SALA", "label": "Salary"}

    subs = [s for s in C.SUBSCRIPTIONS if s in known["Entertainment"] or s == "Apple.com/bill"]
    chosen_subs = rng.sample(subs, k=min(len(subs), rng.randint(1, 3)))
    sub_prices = {"Netflix": 13.99, "Spotify": 11.99, "Disney+": 9.99, "DAZN": 34.99, "Audible": 9.95,
                  "Viaplay": 14.99, "Videoland": 7.99, "Apple.com/bill": 2.99}
    creep_after = history_start + timedelta(days=200) if rng.random() < 0.35 else None

    recurring = [
        {"merchant": rng.choice(known["Housing"]), "category": "Housing", "day": 1, "kind": "standing_order",
         "amount": rng.uniform(420, 700) if persona == "student" else rng.uniform(650, 1500)},
        {"merchant": known["Utilities"][rng.randrange(0, 2)], "category": "Utilities", "day": 3, "kind": "dd",
         "amount": rng.uniform(45, 120), "jitter": 0.08},
        {"merchant": known["Utilities"][rng.randrange(2, len(known["Utilities"]))], "category": "Utilities",
         "day": rng.choice([5, 10, 15]), "kind": "dd", "amount": rng.uniform(20, 60)},
    ]
    for i, s in enumerate(chosen_subs):
        recurring.append({"merchant": s, "category": "Entertainment", "day": rng.randint(2, 27), "kind": "card",
                          "amount": sub_prices.get(s, 9.99),
                          "creep_after": creep_after if i == 0 else None})
    if rng.random() < 0.5:
        recurring.append({"merchant": rng.choice(known["Health & Fitness"]), "category": "Health & Fitness",
                          "day": 1, "kind": "dd", "amount": rng.uniform(20, 55)})
    if rng.random() < 0.25:
        recurring.append({"merchant": rng.choice(known["Charity"]), "category": "Charity", "day": 15,
                          "kind": "dd", "amount": rng.choice([5, 10, 15, 20, 25])})
    if country == "NL":
        recurring.append({"merchant": "Zilveren Kruis", "category": "Healthcare", "day": 1, "kind": "dd",
                          "amount": rng.uniform(135, 155)})
    if customer.get("email"):
        recurring.append({"merchant": "Savings transfer", "category": "Transfers / Other", "day": 28,
                          "kind": "savings", "amount": rng.choice([100, 150, 200, 300])})
    return {"income": income, "recurring": recurring}


# ── Merchant choice ──────────────────────────────────────────────────────────

def _typo(name: str, rng: random.Random) -> str:
    letters = [i for i, ch in enumerate(name[:-1]) if ch.isalpha() and name[i + 1].isalpha()]
    if not letters:
        return name
    i = rng.choice(letters)
    return name[:i] + name[i + 1] + name[i] + name[i + 2:]


def _novel(country: str, category: str, rng: random.Random) -> str:
    template = rng.choice(C.NOVEL[country][category])
    name = template.format(last=rng.choice(C.LAST[country]), first=rng.choice(C.FIRST[country]),
                           place=rng.choice(C.PLACE[country]), city=rng.choice(C.COUNTRIES[country]["cities"]))
    if rng.random() < 0.3:
        name += " " + rng.choice(_LEGAL[country])
    return name


def pick_merchant(country: str, category: str, rng: random.Random,
                  novel_share: float, hard_share: float) -> tuple[str, str, str]:
    """Return (display_name, true_category, slice) for a discretionary purchase."""
    if category == "Transfers / Other":
        return f"{rng.choice(C.FIRST[country])} {rng.choice(C.LAST[country])}", category, "seen"
    if category == "ATM / Cash":
        city = rng.choice(C.COUNTRIES[country]["cities"])
        label = {"DE": "Bargeldauszahlung", "FI": "Nosto", "SE": "Uttag Bankomat",
                 "NL": "GEA Geldautomaat", "IT": "Prelievo Bancomat"}[country]
        return f"{label} {city}", category, "seen"

    r = rng.random()
    if r < hard_share:
        mode = rng.random()
        if mode < 0.35:
            name, true_cat = rng.choice(C.HARD_FIXED)
            return name, true_cat, "hard"
        base = rng.choice(C.KNOWN[country][category])
        if mode < 0.65:
            return rng.choice(C.FACILITATOR_PREFIXES) + base.upper()[:18], category, "hard"
        if mode < 0.85:
            return base.upper()[: rng.randint(8, 14)], category, "hard"
        return _typo(base, rng), category, "hard"
    if r < hard_share + novel_share and category in C.NOVEL[country]:
        return _novel(country, category, rng), category, "novel"
    return rng.choice(C.KNOWN[country][category]), category, "seen"


# ── Statement text per country ───────────────────────────────────────────────

def _ref(rng: random.Random, n: int = 12) -> str:
    return "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(n))


def remittance(country: str, kind: str, merchant: str, day: date, rng: random.Random,
               amount: float, mandate: str | None = None) -> str:
    city = rng.choice(C.COUNTRIES[country]["cities"]).upper()
    if kind == "salary":
        word = {"DE": "LOHN/GEHALT", "FI": "PALKKA", "SE": "LON", "NL": "SALARIS", "IT": "STIPENDIO"}[country]
        return f"{word} {day:%m/%Y}"
    if kind == "pension":
        return {"DE": "RENTE", "FI": "ELAKE", "SE": "PENSION", "NL": "AOW", "IT": "PENSIONE"}[country] + f" {day:%m/%Y}"
    if kind == "dd":
        return {
            "DE": f"SEPA-BASISLASTSCHRIFT EREF+{_ref(rng)} MREF+{mandate} SVWZ+{merchant} Kd-Nr {rng.randint(10**6, 10**7)}",
            "FI": f"E-LASKU {merchant} VIITE {rng.randint(10**8, 10**9)}",
            "SE": f"AUTOGIRO {merchant.upper()}",
            "NL": f"SEPA Incasso algemeen doorlopend Naam: {merchant} Machtiging: {mandate}",
            "IT": f"ADDEBITO DIRETTO SDD {merchant.upper()} MANDATO {mandate}",
        }[country]
    if kind in ("transfer", "standing_order", "savings"):
        return {
            "DE": f"SEPA-UEBERWEISUNG SVWZ+{merchant}", "FI": f"TILISIIRTO {merchant}",
            "SE": f"OVERFORING {merchant}", "NL": f"SEPA Overboeking Naam: {merchant}",
            "IT": f"BONIFICO A FAVORE DI {merchant.upper()}",
        }[country]
    if kind == "atm":
        return merchant.upper()
    return {  # card
        "DE": f"{merchant.upper()}//{city}/DE {day:%d.%m} Kartenzahlung girocard",
        "FI": f"KORTTIOSTO {day:%d.%m} {merchant.upper()} {city}",
        "SE": f"KORTKOP {day:%y%m%d} {merchant.upper()} {city}",
        "NL": f"BEA, Betaalpas {merchant},PAS{rng.randint(100, 999)} NR:{_ref(rng, 8)}, {day:%d.%m.%y} {city}",
        "IT": f"PAGAMENTO POS {abs(amount):.2f} EUR DEL {day:%d.%m.%y} A ({city}) {merchant.upper()}",
    }[country]


_BTC = {"card": "PMNT-MCRD-POSD", "dd": "PMNT-RDDT-ESDD", "transfer": "PMNT-ICDT-ESCT",
        "standing_order": "PMNT-ICDT-STDO", "savings": "PMNT-ICDT-ESCT", "salary": "PMNT-RCDT-ESCT",
        "pension": "PMNT-RCDT-ESCT", "atm": "PMNT-CWDL-ATML", "invoice": "PMNT-RCDT-ESCT"}


# ── One day ──────────────────────────────────────────────────────────────────

def _poisson(lam: float, rng: random.Random) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _last_day(d: date) -> int:
    nxt = (d.replace(day=28) + timedelta(days=4))
    return (nxt - timedelta(days=nxt.day)).day


def transactions_for_day(customer: dict, accounts: list[dict], plan: dict, day: date,
                         novel_share: float, hard_share: float, run_label: str) -> list[tuple[dict, dict]]:
    """All transactions a customer books on `day`, as (transaction, label) pairs."""
    rng = random.Random(f"{customer['customer_id']}|{day.isoformat()}")
    country, persona = customer["country"], customer["persona"]
    fx = C.COUNTRIES[country]["fx"]
    cur_acct = accounts[0]
    out: list[tuple[dict, dict]] = []
    seq = 0

    def add(account: dict, amount_eur: float, category: str, merchant: str, slice_: str, kind: str,
            counterparty_is_creditor: bool, recurring: bool = False, purpose: str | None = None) -> None:
        nonlocal seq
        amount = round(amount_eur * fx, 2)
        mandate = f"MNDT{_num(customer['customer_id'] + merchant) % 10**8:08d}" if kind == "dd" else None
        tx = {
            "transaction_id": hashlib.sha1(f"{account['resource_id']}|{day}|{seq}".encode()).hexdigest()[:24],
            "resource_id": account["resource_id"], "customer_id": customer["customer_id"],
            "booking_date": day, "value_date": day, "amount": amount, "currency": account["currency"],
            "creditor_name": merchant if counterparty_is_creditor else None,
            "debtor_name": None if counterparty_is_creditor else merchant,
            "remittance": remittance(country, kind, merchant, day, rng, amount, mandate)[:300],
            "bank_transaction_code": _BTC.get(kind, "PMNT-MCRD-POSD"), "purpose_code": purpose,
            "end_to_end_id": None if kind in ("card", "atm") else f"E2E{_ref(rng, 16)}",
            "mandate_id": mandate,
            "creditor_id": f"{country}98ZZZ{_num(merchant) % 10**11:011d}" if kind == "dd" else None,
            "feed_run": run_label,
        }
        label = {"transaction_id": tx["transaction_id"], "category": category,
                 "merchant": merchant, "slice": slice_, "is_recurring": recurring}
        out.append((tx, label))
        seq += 1

    last = _last_day(day)
    income = plan["income"]
    if income and day.day == min(income["day"], last):
        kind = "pension" if income["purpose"] == "PENS" else "salary"
        add(cur_acct, income["amount"], "Income", income["payer"], "seen", kind, False, True, income["purpose"])
    if persona == "freelancer" and rng.random() < 3 / 30.4:
        client = f"{rng.choice(C.LAST[country])} {rng.choice(_LEGAL[country])}"
        add(cur_acct, rng.uniform(800, 3500), "Income", client, "seen", "invoice", False, False, "SUPP")

    for r in plan["recurring"]:
        if day.day != min(r["day"], last):
            continue
        amount = r["amount"] * (1 + rng.uniform(-r.get("jitter", 0), r.get("jitter", 0)))
        if r.get("creep_after") and day >= r["creep_after"]:
            amount *= 1.15
        if r["kind"] == "savings" and len(accounts) > 1:
            add(cur_acct, -amount, "Transfers / Other", "Savings transfer", "seen", "savings", True, True)
            add(accounts[1], amount, "Transfers / Other", "Savings transfer", "seen", "savings", False, True)
            continue
        add(cur_acct, -amount, r["category"], r["merchant"], "seen", r["kind"], True, True)

    for category, per_month in C.MONTHLY_RATE[persona].items():
        for _ in range(_poisson(per_month / 30.4, rng)):
            merchant, true_cat, slice_ = pick_merchant(country, category, rng, novel_share, hard_share)
            lo, hi = C.AMOUNTS[true_cat]
            amount = rng.uniform(lo, hi)
            if true_cat == "ATM / Cash":
                amount = round(amount / 10) * 10
                kind = "atm"
            elif true_cat == "Transfers / Other":
                kind = "transfer"
            else:
                kind = "card"
            if true_cat == "Transfers / Other" and rng.random() < 0.35:
                add(cur_acct, amount, true_cat, merchant, slice_, kind, False)  # money received from a friend
            else:
                add(cur_acct, -amount, true_cat, merchant, slice_, kind, True)
    return out
