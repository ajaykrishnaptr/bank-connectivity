# FintNet — Financial Institutions Integration Network

Connect European bank accounts in one place. FintNet fetches accounts, balances, and transactions via secure bank APIs and presents a unified view — including cross-border, multi-currency consolidation.

---

## Architecture

**Multi-tenant auth**
- Flask-Login with email + password
- Every user sees only their own data
- `role` column in DB for future TPP admin panel (unused for now)

**Bank connections**
- `BankConnection` table stores token/consent per user per bank — persistent across sessions
- After consent is granted, accounts + transactions are fetched and stored immediately
- Users can connect multiple banks simultaneously
- Disconnect marks the connection as revoked; historical data is kept

**Analytics** — all scoped to the logged-in user
- Dashboard — spending/income overview, MoM deltas, top merchants, 6-month trend
- Spending — category breakdown with per-bank splits
- Balances — multi-bank aggregation with live currency conversion to EUR
- Recurring — auto-detected fixed and variable recurring payments

**Cross-border currency support** (`currency_utils.py`)
- Fetches live exchange rates from `frankfurter.app` (European Central Bank data)
- Rates cached in-memory for 1 hour; falls back to hardcoded ECB approximates if API is down
- All non-EUR account balances converted to EUR on the Balances page
- Currency breakdown card shows each currency's share of total net worth

---

## Banks integrated

| Bank | Country | Status |
|------|---------|--------|
| Nordea | Finland, Sweden, Norway, Denmark | Ready |
| Commerzbank | Germany | Ready — no redirect needed |
| UniCredit | Italy | Requires sandbox onboarding |

---

## Running the app

```bash
python3 app.py
```

Visit: http://127.0.0.1:5000

---

## Seeding test data

Populates the DB with 3 test users, bank connections, accounts, and ~6 months of transactions. Safe to re-run — clears and recreates test data each time.

```bash
python3 seed_data.py
```

---

## Test accounts

All accounts use password: **`TestPass123`**

| Name | Email | Banks | Currency |
|------|-------|-------|----------|
| Priya Sharma | `priya.sharma@testbank.eu` | Nordea FI + Commerzbank DE | EUR |
| Arjun Mehta | `arjun.mehta@testbank.eu` | Nordea SE | SEK |
| Kavya Reddy | `kavya.reddy@testbank.eu` | Commerzbank DE | EUR |

**Priya Sharma** — 4 accounts (2 Nordea Finland, 2 Commerzbank Germany), salary from Siemens AG ~€4,200/mo. Good demo for multi-bank, single-currency.

**Arjun Mehta** — 2 Swedish Nordea accounts (`Lönekonto` + `Sparkonto`) in SEK, salary from SAP SE ~38,000–44,000 SEK/mo. Best demo for cross-border currency conversion.

**Kavya Reddy** — 2 Commerzbank Germany accounts, salary from Deutsche Bank AG ~€4,400/mo.

Each current account includes:
- Monthly salary credit (1st–5th of month)
- Fixed recurring: Netflix, Spotify, Disney+, Deutsche Telekom, Vattenfall, TK Krankenkasse, rent
- Variable expenses: Lidl, REWE, McDonald's, Starbucks, Deutsche Bahn, H&M, Zalando, and more
- Occasional freelance / transfer income

---

## What's next

- Expat view — side-by-side comparison of accounts across two countries for users who live cross-border
- Subscription intelligence — detect unused subscriptions and alert the user
- Tax category tagging — flag deductible expenses per country for freelancers
- TPP admin panel (`role` column already in DB)
- Token refresh / expiry handling — mark connection as `expired`, show Reconnect button
- Background data sync — periodic re-fetch of transactions per active connection
