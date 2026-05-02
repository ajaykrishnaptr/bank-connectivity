# Bank Connectivity

A multi-tenant PSD2 Open Banking app. Users connect their European bank accounts via OAuth2 consent flows. The TPP fetches and stores accounts and transactions, and provides spending analytics, balance aggregation, and recurring payment detection.

---

## Architecture

**Multi-tenant auth**
- Flask-Login with email + password
- Every user sees only their own data
- `role` column exists in the DB for future TPP admin panel (unused for now)

**Bank connections**
- `BankConnection` table stores token/consent per user per bank — persistent across sessions
- After consent is granted, accounts + transactions are fetched and stored immediately
- Users can connect multiple banks simultaneously
- Disconnect marks the connection as revoked; historical data is kept

**Analytics** — all scoped to the logged-in user
- Dashboard — spending/income overview, MoM deltas, top merchants, 6-month trend
- Spending — category breakdown with per-bank splits
- Balances — account aggregation across all connected banks
- Recurring — auto-detected fixed and variable recurring payments

---

## Banks integrated

| Bank | Auth method | Status |
|------|-------------|--------|
| Nordea | OAuth2 authorization_code — user redirected to bank, sandbox auto-approves | Ready |
| Commerzbank | OAuth2 client_credentials + static sandbox consent | Ready — no redirect needed |
| UniCredit | mTLS + API consent + SCA redirect | Requires sandbox onboarding |

**How the consent flow works (PSD2 model):**
The user picks their bank in the TPP app and is redirected to the bank's own login page. They authenticate at the bank — the TPP never sees their bank password. The bank issues an authorization code, the TPP exchanges it for an access token, and fetches the user's accounts and transactions. Commerzbank uses a TPP-level OAuth token + a pre-approved sandbox consent instead of a per-user redirect.

---

## Running the app

```bash
python3 app.py
```

Visit: http://127.0.0.1:5000

---

## Seeding test data

Populates the DB with 3 test users, bank connections, accounts, and 6 months of transactions. Safe to re-run — clears and recreates test data each time.

```bash
python3 seed_data.py
```

---

## Test accounts

All accounts use password: **`TestPass123`**

| Name | Email | Banks | Transactions |
|------|-------|-------|-------------|
| Priya Sharma | `priya.sharma@testbank.eu` | Nordea + Commerzbank | ~278 |
| Arjun Mehta | `arjun.mehta@testbank.eu` | Nordea only | ~140 |
| Kavya Reddy | `kavya.reddy@testbank.eu` | Commerzbank only | ~144 |

**Priya Sharma** — 4 accounts (2 Nordea FI, 2 Commerzbank DE), salary from Siemens AG ~€4,200/mo
**Arjun Mehta** — 2 accounts (2 Nordea FI), salary from SAP SE ~€3,750/mo
**Kavya Reddy** — 2 accounts (2 Commerzbank DE), salary from Deutsche Bank AG ~€4,400/mo

Each current account includes:
- Monthly salary credit (1st–5th of month)
- Fixed recurring: Netflix, Spotify, Disney+, Deutsche Telekom, Vattenfall, TK Krankenkasse, rent
- Variable expenses: Lidl, REWE, McDonald's, Starbucks, Deutsche Bahn, H&M, Zalando, and more
- Occasional freelance / transfer income

---

## What's next

- TPP admin panel (`role` column already in DB)
- Commerzbank proper per-user `create_consent()` redirect flow for production
- Token refresh / expiry handling — mark connection as `expired`, show Reconnect button
- Background data sync — periodic re-fetch of transactions per active connection

---

## Sandbox limitations

Bank data from sandbox APIs is synthetic — not tied to real user accounts. Seeded users bypass the consent flow entirely; data is inserted directly into the DB. Real bank connections (via the Connect page) work on top of seeded data.
