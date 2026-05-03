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

| Bank | Country | Auth | Status |
|------|---------|------|--------|
| Nordea | Finland, Sweden, Norway, Denmark | OAuth2 authorization_code + SCA | Ready |
| Commerzbank | Germany | OAuth2 client_credentials + consent | Ready — no redirect needed |
| UniCredit | Italy | mTLS + PSD2 consent SCA | Requires sandbox onboarding |
| Deutsche Bank | Germany | OAuth2 + Berlin Group consent + SCA redirect | Client built, awaiting credentials |
| ING | Netherlands, Belgium, Germany | mTLS + HTTP Signatures + OAuth2 authorization_code | Working with sandbox example client |

### ING flow specifics

ING is the most complex integration:
- Two **separate key pairs** required: TLS for mTLS, signing for HTTP Request Signatures
- Two **different keyId formats** depending on endpoint:
  - App token (`client_credentials`): `keyId="SN=<cert-serial-hex>"`, signature in `Authorization` header, requires `TPP-Signature-Certificate` header
  - All Bearer-token calls (code exchange, AIS): `keyId="<client_id>"`, signature in `Signature` header, no TPP cert
- Sandbox example client uses pre-registered redirect URI `https://www.example.com/`. After authorization the user lands there with `?code=...` in the URL bar — paste it at `/ing/enter-code` to complete connection.
- Per-account grants vary by sandbox test profile — `_fetch_and_store` catches `INGApiError` 403s and skips accounts without grant, so the connection still saves.

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
| Priya Sharma | `priya.sharma@testbank.eu` | Nordea FI + Commerzbank DE + ING NL | EUR |
| Arjun Mehta | `arjun.mehta@testbank.eu` | Nordea SE | SEK |
| Kavya Reddy | `kavya.reddy@testbank.eu` | Commerzbank DE | EUR |

**Priya Sharma** — 6 accounts across three banks (2 Nordea Finland, 2 Commerzbank Germany, 2 ING Netherlands), salary from Siemens AG ~€4,200/mo. Best demo for multi-bank, multi-country aggregation.

**Arjun Mehta** — 2 Swedish Nordea accounts (`Lönekonto` + `Sparkonto`) in SEK, salary from SAP SE ~38,000–44,000 SEK/mo. Best demo for cross-border currency conversion.

**Kavya Reddy** — 2 Commerzbank Germany accounts, salary from Deutsche Bank AG ~€4,400/mo.

Each current account includes:
- Monthly salary credit (1st–5th of month)
- Fixed recurring: Netflix, Spotify, Disney+, Deutsche Telekom, Vattenfall, TK Krankenkasse, rent
- Variable expenses: Lidl, REWE, McDonald's, Starbucks, Deutsche Bahn, H&M, Zalando, and more
- Occasional freelance / transfer income

---

## Roadmap

### Growing the TPP (becoming the aggregator)

FintNet is a licensed-ready TPP. The goal is to expand direct PSD2 integrations bank by bank — no third-party aggregator, full data ownership, no per-connection fees.

**Pending setup**
- [ ] Deutsche Bank sandbox credentials — register at [developer.db.com](https://developer.db.com), then add to `.env`:
  - `DB_CLIENT_ID`, `DB_CLIENT_SECRET`
  - `DB_SANDBOX_PSU_ID` (from Dashboard → My Test Users)
  - `DB_BASE_URL`, `DB_TOKEN_URL` (from your app's API docs page after registration)

**Next banks to integrate**
- Santander (Spain/Portugal) — Berlin Group, good sandbox
- BNP Paribas (France) — Berlin Group, large retail footprint
- HSBC (UK/Europe) — post-Brexit but active PSD2 API
- BBVA (Spain) — Berlin Group, strong open API ecosystem

**Infrastructure needed to scale**
- Bank registry — config-driven bank catalogue (name, country, spec, base URL, auth method) so adding a new bank doesn't require a new client file
- Unified PSD2 adapter — single client that handles Berlin Group NextGenPSD2 spec (covers ~80% of EU banks); keep bespoke clients only for non-standard banks (Nordea, UniCredit)
- Token refresh / expiry handling — mark connection as `expired`, show Reconnect button
- Background data sync — periodic re-fetch of transactions per active connection
- Consent renewal — auto-prompt users before 90-day consent windows expire

**Platform features**
- TPP admin panel — manage users, connections, consent status (`role` column already in DB)
- Expat view — side-by-side accounts across two countries for cross-border users
- Tax category tagging — flag deductible expenses per country for freelancers
