# Bank Connectivity

A multi-tenant PSD2 Open Banking app. Users connect their European bank accounts via OAuth2 consent flows. The TPP fetches and stores accounts and transactions, and provides spending analytics, balance aggregation, and recurring payment detection.

---

## Running the app

```bash
python3 app.py
```

Visit: http://127.0.0.1:5000

---

## Seeding test data

Populates the database with 3 test users, their bank connections, accounts, and 6 months of transactions. Safe to re-run — clears and recreates test data each time.

```bash
python3 seed_data.py
```

---

## Test accounts

All accounts use password: **`TestPass123`**

| Name | Email | Banks connected |
|------|-------|-----------------|
| Priya Sharma | `priya.sharma@testbank.eu` | Nordea + Commerzbank |
| Arjun Mehta | `arjun.mehta@testbank.eu` | Nordea only |
| Kavya Reddy | `kavya.reddy@testbank.eu` | Commerzbank only |

### What's seeded per user

**Priya Sharma** — 4 accounts (2 Nordea FI, 2 Commerzbank DE), salary from Siemens AG ~€4,200/mo
**Arjun Mehta** — 2 accounts (2 Nordea FI), salary from SAP SE ~€3,750/mo
**Kavya Reddy** — 2 accounts (2 Commerzbank DE), salary from Deutsche Bank AG ~€4,400/mo

Each current account has:
- Monthly salary credit
- Fixed recurring charges (Netflix, Spotify, rent, Deutsche Telekom, Vattenfall)
- Variable expenses across groceries, dining, transport, shopping, health
- Occasional freelance/transfer income

---

## Connected sandbox banks

| Bank | Auth method | Status |
|------|-------------|--------|
| Nordea | OAuth2 authorization_code | Ready — sandbox auto-approves |
| Commerzbank | OAuth2 client_credentials + static consent | Ready — no redirect needed |
| UniCredit | mTLS + API consent + SCA redirect | Requires sandbox onboarding |

---

## Sandbox limitations

Bank data from sandbox APIs is synthetic test data — not real user accounts. Seeded users bypass the consent flow entirely; data is inserted directly into the DB. Real bank connections (via the Connect page) work on top of seeded data.
