# FintNet — Product Requirements Document

**Status:** Built (working sandbox/demo, pre-production)
**Owner:** Ajay Krishna
**Last updated:** 2026-05-12

---

## 1. Summary

FintNet is a self-hosted personal-finance aggregator for European bank
customers. A user signs up with email + password, connects one or more
PSD2-licensed banks (Nordea, Commerzbank, UniCredit, Deutsche Bank, ING),
and gets a unified, multi-currency view of their balances, transactions,
spending categories, and recurring charges — including an automatic
"wasted spend" radar that surfaces redundant subscriptions, silent price
hikes, and likely-lapsed memberships.

Every transaction is auto-categorised into one of 14 buckets by a local
LLM (Ollama running Qwen 2.5 3B), so merchant strings and bank data
never leave the device. The same data is also exposed to a hand-built
tool-using agent that can answer natural-language questions like
*"how much did I spend on groceries in the last 30 days?"* by planning
SQL-backed tool calls.

The product doubles as a technical reference for two domains the
author wanted to learn end-to-end: (a) real PSD2 / Berlin Group bank
connectivity including the strict crypto requirements (mTLS, HTTP
request signatures, eIDAS QWAC certs, OCSP/CRL hosting) and (b) the
production patterns for shipping a local-LLM feature (cache, overrides,
structured output, evaluation, agent loop).

---

## 2. Goals & non-goals

### 2.1 Goals

- **G1** — Give a single user a consolidated view of their European
  bank accounts across countries and currencies, refreshed by direct
  PSD2 API calls (no screen-scraping, no Plaid/Tink reseller).
- **G2** — Run all transaction categorisation on-device with a local
  LLM. Bank data must never be sent to a third-party cloud LLM.
- **G3** — Surface actionable money-leak signals (redundant subs,
  price creep, lapsed memberships, fixed-cost burden) so the dashboard
  is more than a passive ledger.
- **G4** — Demonstrate a real-world TPP-grade integration stack:
  Berlin Group consent flow, OAuth2 + SCA redirect, mTLS, HTTP request
  signatures, self-hosted OCSP responder + CRL distribution.
- **G5** — Be Splunk-ready out of the box: every domain event is a
  structured JSON log line with stable field names.

### 2.2 Non-goals

- **NG1** — Not a multi-tenant SaaS. There is no billing, no admin
  console, no multi-region deployment. The `role="tpp_admin"` column
  exists as a forward hook but isn't wired up.
- **NG2** — Not a payment initiation service (PIS). Read-only AIS
  (Account Information Service) only.
- **NG3** — Not a fully automated background sync. Data is fetched on
  consent grant; periodic re-sync is on the roadmap, not built.
- **NG4** — Not a budgeting / goals product. We surface signals but
  don't let users set monthly caps or savings targets.
- **NG5** — No mobile app. Browser-only, Flask-rendered HTML.

---

## 3. Target user

**Primary persona:** an EU-resident knowledge worker with accounts at
2–4 banks across at least one currency boundary (e.g. a freelancer
banking in DE and SE, or a salaryman with a Finnish daily account and
a German savings account). They are technical enough to run a Python
app locally, care about not handing transaction data to third parties,
and want a unified picture without paying a SaaS subscription.

**Secondary persona (forward-looking):** a developer learning PSD2 /
Berlin Group integration or local-LLM patterns who wants a working
reference implementation rather than a tutorial.

The seeded demo personas — one per connected bank, named with each
country's banking-sandbox placeholder (Max Mustermann, Anna Korhonen,
Sven Andersson, Jan Jansen, Mario Rossi) — demonstrate the primary
use-cases:
- Max Mustermann — Commerzbank + Deutsche Bank, EUR — multi-bank consolidation within one country.
- Anna Korhonen — Nordea Finland, EUR — single-bank baseline.
- Sven Andersson — Nordea Sweden in SEK — best demo of FX conversion.
- Jan Jansen — ING Netherlands, EUR.
- Mario Rossi — UniCredit Italy, EUR.

---

## 4. User journeys

### 4.1 First-time connect

1. User visits `/`, signs up with email + password (`/signup`).
2. Lands on the home page, which lists 5 bank cards (each with country
   flag, status, and a "Connect" CTA).
3. Picks a bank → goes through that bank's specific OAuth / consent
   flow (see §6).
4. On successful consent, accounts and transactions are fetched
   immediately (synchronously, inside the consent callback) and stored.
5. Redirected to `/dashboard`, which now has data.

### 4.2 Returning user

1. Logs in at `/login`.
2. Lands on `/` (home), sees their existing connections and any active
   "wasted spend" alert cards.
3. Navigates between `/dashboard`, `/spending`, `/aggregation` (balances),
   `/recurring`.
4. Optional: dismiss alerts (`/dismiss-alert`), disconnect a bank
   (`/disconnect/<bank>`), or drill into a specific account's
   transactions (`/accounts/<id>/transactions`) or balances
   (`/accounts/<id>/balances`).

### 4.3 Disconnect / data retention

- Disconnecting a bank flips its `BankConnection.status` to `revoked`
  but **does not delete** historical accounts or transactions. The
  rationale (documented in code): historical analytics should keep
  working after consent expires.

---

## 5. Functional requirements

### 5.1 Authentication

- Email + password (Flask-Login + `werkzeug.security` hashing).
- Per-user data scoping: every analytics query joins through
  `Account.user_id == current_user.id`.
- `User.role` column reserved for a future TPP admin view.
- Login failures do not disclose which half (email vs password) was
  wrong.
- Structured log events: `auth.login.success`, `auth.login.failed`,
  `auth.logout`, `auth.signup`.

### 5.2 Bank connections

For each of the 5 banks (§6), the system must:

- Initiate the bank-specific consent / OAuth flow from `/<bank>/connect`.
- Handle the callback, validate consent status (`valid` vs anything
  else), and persist a `BankConnection` row with either an
  `access_token` (Nordea, ING) or a `consent_id` (Commerzbank,
  UniCredit, Deutsche Bank).
- Upon successful consent, synchronously fetch all accounts and ~6
  months of transactions and persist them.
- Support multiple banks per user (UNIQUE on `(user_id, bank)`).
- On per-account `403 Forbidden` (ING sandbox), skip that account and
  emit `sync.account.skipped` rather than failing the whole connection.
- Disconnect: mark `status="revoked"`, keep data, emit
  `connection.disconnect`.

### 5.3 Analytics views

All views are scoped to `current_user`.

**5.3.1 Home (`/`)**
- Lists all 5 bank cards with connection status.
- Shows hero stats (`active_conns`, `account_count`, `txn_count`).
- Renders un-dismissed "waste" alerts (see §5.4).

**5.3.2 Dashboard (`/dashboard`)**
- Default window: current month, override via `?range=...`.
- KPIs: total spent, total income, net, each with month-over-month
  delta vs a same-length preceding window.
- Per-bank breakdown for the period.
- Category breakdown (donut) for the period.
- 6-month stacked-bar trend, one stack per bank.
- Top 10 merchants by spend in the period.
- 15 most recent transactions.

**5.3.3 Spending (`/spending`)**
- Outflows only, default window: last 90 days.
- Category table with per-category MoM delta and per-bank split.

**5.3.4 Balances / aggregation (`/aggregation`)**
- Lists every account across every bank.
- Native-currency balance + EUR-converted balance side by side.
- "Money by bank" donut.
- "Currency mix" card with percentage share of total EUR-equivalent
  net worth.
- Live FX rates from frankfurter.app, cached in-memory for 1 hour,
  falls back to hardcoded ECB approximates on outage (logged as
  `currency.fetch_failed`).

**5.3.5 Recurring (`/recurring`)**
- Auto-detected recurring expenses split into **fixed** (low coefficient
  of variation, ≈subscriptions) and **variable** (utilities-style).
- Auto-detected recurring income.
- "Wasted spend" alert cards (see §5.4).
- Definition of recurring: same merchant+bank pair seen in ≥2 distinct
  calendar months.

**5.3.6 Account detail**
- `/accounts/<id>/balances` — balance snapshot.
- `/accounts/<id>/transactions` — paginated transaction list.

### 5.4 "Wasted spend" detector

Generates dismissable alerts in four families:

1. **Redundant** — two fixed-cost subscriptions in the same
   redundancy category (`Entertainment`, `Health & Fitness`).
2. **Price creep** — fixed sub whose average of the first two charges
   vs. the last two has risen >5%.
3. **Lapse** — heuristic guesses that a sub isn't being used:
   - (a) transit pass held ≥3 months + ≥3 rideshare charges in window.
   - (b) gym membership + zero adjacent health spend in 90 days.
4. **Burden** — fixed costs as % of average income.

Dismissals persist per user in `DismissedAlert(user_id, alert_key)`.
Alerts re-appear if the underlying signal regenerates with a different
key (e.g. a new merchant joins the redundancy set).

### 5.5 Transaction categorisation

Four-layer waterfall in `categorize.py::categorize(merchant)`:

1. **Overrides** — short hand-curated list (case-insensitive substring).
2. **DB cache** — `MerchantCategory(merchant unique, category, source)`.
3. **Local LLM** — Ollama / Qwen 2.5 3B, `temperature=0`, few-shot
   prompted with DE/IN/Nordic examples. Result written back to cache.
4. **Keyword rules** — pure-Python fallback if AI disabled or LLM fails.

Toggle: `USE_AI_CATEGORIZER=true|false`.

Canonical category set is 14 buckets (Groceries, Utilities, Dining,
Income, etc.); legacy names (`Food & Drink`, `Health`, `Transfer`) are
re-mapped by the backfill script.

**Additional API:** `categorize_with_confidence(merchant)` returns
`{category, confidence, reasoning}` JSON for the dashboard's "explain"
UI. Bypasses the cache. Defensive parsing strips markdown code fences.

**Logged events:** `categorize.cached`, `categorize.ai.failed`,
`categorize.ai.unknown_label`, `categorize.json.parse_failed`.

**Operational tools:**
- `backfill_categories.py` — re-categorise all existing transactions,
  print before/after diff.
- `eval_categorizer.py` — compare keyword rules vs AI on seeded data.
- `genai_json_demo.py` — structured-output demo.

### 5.6 Tool-using agent (`agent.py`)

CLI: `python3 agent.py "<natural-language question>"`.

- Implements the agent loop by hand (no LangChain).
- System prompt declares two tools: `total_spent`, `top_merchants`.
- Strict JSON output schema with two shapes:
  `{"action": "tool_call", ...}` or `{"action": "answer", ...}`.
- Ollama `format="json"` forces single-pass `json.loads()`.
- Loop cap: 5 turns.
- Tools deliberately return small scalar summaries (e.g.
  `{count, inflow, outflow, net}`) rather than raw row lists — large
  contexts blow up CPU prompt eval on Qwen 3B.
- Status: seed implementation. Production-fast on M-series Macs;
  unusably slow on Intel CPU-only.

### 5.7 Cross-border currency

`currency_utils.py`:
- `get_rates(base="EUR")` fetches live rates from frankfurter.app.
- In-memory cache, 1-hour TTL.
- Fallback table of hardcoded ECB approximates on API outage.
- `to_eur(amount, currency, rates)` converts.
- Flag emoji map for currency badges in the UI.

### 5.8 Seeding (`seed_data.py`)

Idempotent (clears + recreates). Also runs automatically when the DB is
empty (fresh checkout or serverless cold start). Creates:
- 5 demo personas (one per connected bank: Max Mustermann, Anna Korhonen,
  Sven Andersson, Jan Jansen, Mario Rossi) with password `TestPass123`.
- Their bank connections and accounts (12 accounts total, 2 per bank).
- ~6 months of transactions including monthly salary, fixed recurring
  (Netflix, Spotify, Disney+, Telekom, Vattenfall, TK Krankenkasse,
  rent), variable expenses (Lidl, REWE, McDonald's, Starbucks, Deutsche
  Bahn, H&M, Zalando, ...), and occasional freelance income.
- Categorisation runs as part of the seed.

---

## 6. Bank connectivity matrix

| Bank | Country | Auth pattern | Persisted as | Status |
|------|---------|--------------|--------------|--------|
| Nordea | FI / SE / NO / DK | OAuth2 authorization_code + hosted SCA | `access_token` | Ready |
| Commerzbank | DE | OAuth2 client_credentials + consent (no redirect needed) | `consent_id` | Ready |
| UniCredit | IT | mTLS QWAC + Berlin Group consent + SCA redirect | `consent_id` | Ready (sandbox) |
| Deutsche Bank | DE | OAuth2 + Berlin Group consent + SCA redirect | `consent_id` | Client built, awaiting credentials |
| ING | NL / BE / DE | mTLS + HTTP Request Signatures + OAuth2 authorization_code | `access_token` | Working w/ sandbox example client |

### 6.1 ING specifics

- **Two key pairs**: TLS keypair for mTLS, separate signing keypair
  for HTTP Request Signatures.
- **Two keyId formats**:
  - App token (`client_credentials`): `keyId="SN=<cert-serial-hex>"`,
    signature in `Authorization` header, requires
    `TPP-Signature-Certificate` header.
  - Bearer-token calls (code exchange, AIS): `keyId="<client_id>"`,
    signature in `Signature` header, no TPP cert header.
- Sandbox uses pre-registered redirect URI `https://www.example.com/`.
  User pastes the `?code=...` back into `/ing/enter-code`.
- `_fetch_and_store` tolerates per-account `403`s and emits
  `sync.account.skipped`.

### 6.2 UniCredit specifics — self-hosted PKI

UniCredit's F5 SSL gateway is strict about cert revocation, which
forces FintNet to host its own OCSP responder and CRL DP:

- It **hard-fails** if the AIA OCSP host is unreachable. NXDOMAIN
  counts as a hard fail, not "OCSP unavailable".
- It **refuses `https://` CRL DPs** — most F5 SSL profiles only follow
  `http://` (or `ldap://`) for revocation lookups.

Components:
- `generate_psd2_cert.py` — mints the QWAC chain with ETSI PSD2
  `qcStatements` (PSP_AI role, BaFin authority, ID
  PSDDE-BAFIN-19337). Leaf AIA/CRLDP point at fintnet.ai endpoints.
- `generate_ocsp_signer.py` — mints a delegate OCSP signer (EKU
  `id-kp-OCSPSigning`, `id-pkix-ocsp-nocheck` to break recursion).
  The intermediate's private key never leaves the laptop; the VM only
  holds the delegate key.
- Always-Free Oracle VM (140.245.223.102) running:
  - `openssl ocsp -port 8888` as signing backend.
  - ~50-line stdlib Python proxy on port 80 serving `/crl.crl` +
    `/inter.crt` static, forwarding everything else to OCSP backend.
  - Both as systemd services.
- DNS: `ocsp.fintnet.ai`, `crl.fintnet.ai` A-records at GoDaddy.
- After each leaf re-issue, `generate_psd2_cert.py` writes
  `certs/ocsp_index.txt` with the new serial as `V` (valid); pushed
  to the responder.
- `refresh_crl.py` — re-signs an empty CRL monthly (nextUpdate 30 days
  out).
- `deploy_crl.sh` — scps the fresh CRL to the VM and restarts the proxy.
- One-time bank action: import `chain.crt` into their trust store.
  Subsequent leaf re-issues under the same intermediate need no
  re-trust.

---

## 7. Data model

(See `models.py` for the SQLAlchemy ORM definitions; SQLite in
`instance/` by default.)

- `User(id, email, password_hash, role, created_at)` — 1:N to
  `Account`, `BankConnection`, `DismissedAlert`.
- `BankConnection(id, user_id, bank, access_token?, consent_id?,
  status, connected_at)` — UNIQUE `(user_id, bank)`.
- `Account(id, bank, resource_id, iban, currency, name, owner_name,
  user_id, fetched_at)` — UNIQUE `(bank, resource_id)`. 1:N to
  `Transaction`. Cascades on delete.
- `Transaction(id, bank, account_id, booking_date, value_date,
  amount[Numeric(18,4)], currency, creditor_name, debtor_name,
  remittance_info, status, category, fetched_at)`. Money is `Numeric`,
  not `Float`. `status` ∈ {`booked`, `pending`}.
- `MerchantCategory(merchant unique, category, source ∈ {ai|rule},
  created_at)` — LLM-result cache, no FK.
- `DismissedAlert(user_id, alert_key)` — UNIQUE `(user_id, alert_key)`.

Dedup of transactions is handled in `db_utils.upsert_transactions`,
not at the DB level (legitimate near-duplicates with different
remittance text exist).

---

## 8. Logging & observability

Structured JSON to `logs/fintnet.json` via `RotatingFileHandler`
(10 MB, 5 backups). Every line is a single JSON object with `ts`,
`level`, `logger`, `event`, plus event-specific fields.

**Event catalogue (today):**
- Auth: `auth.login.success`, `auth.login.failed`, `auth.logout`,
  `auth.signup`.
- Connections: `connection.upsert`, `connection.disconnect`.
- Sync: `sync.complete` (`latency_ms`, `account_count`, `bank`),
  `sync.account.skipped`.
- Categorisation: `categorize.cached`, `categorize.ai.failed`,
  `categorize.ai.unknown_label`, `categorize.json.parse_failed`.
- Currency: `currency.fetch_failed`.
- ING: `ing.customer_token` (scope, expires_in).

**Splunk ingest path:** Universal Forwarder against
`logs/fintnet.json` (preferred — resilient to Splunk downtime), or
swap `RotatingFileHandler` for an HEC handler in `logging_config.py`.

---

## 9. Privacy & security requirements

- **R1** — Transaction data (merchant strings, amounts, IBANs) MUST
  NOT be sent to a third-party cloud LLM. Categorisation runs locally
  via Ollama.
- **R2** — Bank credentials are NEVER captured by FintNet. All
  authentication happens on the bank's hosted SCA page via OAuth2 /
  PSD2 redirect.
- **R3** — Passwords stored as `pbkdf2:sha256` via
  `generate_password_hash`. No plaintext.
- **R4** — TLS material:
  - mTLS QWAC keypairs (UniCredit, ING) stored in `certs/`, gitignored.
  - OCSP intermediate private key never leaves the dev machine; only
    the delegate signer's key sits on the public OCSP VM.
- **R5** — Per-user data isolation enforced at the query layer
  (`Account.user_id == current_user.id` in every analytics query).
- **R6** — Login failures must not disclose which credential half was
  wrong.
- **R7** — Disconnect MUST mark the connection revoked but MUST NOT
  delete history (so the user can still review past spend).

---

## 10. Non-functional requirements

- **Platform**: Python 3.11.6, Flask, SQLAlchemy, SQLite for dev. Runs
  on macOS (primary), Linux. Local-LLM path needs Ollama on the host;
  M-series performance assumed for the agent loop.
- **Currency conversion**: ≥1 hr stale FX rates are acceptable; the
  app must not fail closed when frankfurter.app is unreachable.
- **Resilience**: per-account 403s during sync must not fail the whole
  bank connection.
- **Operational**: app must continue running if Splunk / log shipper
  is down; logs spool to disk and catch up later.
- **Reproducibility**: categorisation at `temperature=0` + cache must
  produce identical labels for the same merchant string across runs.

---

## 11. Out of scope / explicitly not built

- Background / scheduled re-sync of bank data.
- Token refresh & expiry handling — connections currently don't move
  to `expired` automatically.
- Consent-renewal prompts before the 90-day PSD2 window closes.
- Mobile UI.
- Email / notification channels for alerts.
- Per-user budgeting and savings targets.
- TPP admin console (`role="tpp_admin"` exists but unused).
- Payment Initiation (PIS). AIS only.

---

## 12. Roadmap

### 12.1 Pending setup (unblocks existing code)

- Deutsche Bank sandbox credentials (`DB_CLIENT_ID`,
  `DB_CLIENT_SECRET`, `DB_SANDBOX_PSU_ID`, `DB_BASE_URL`,
  `DB_TOKEN_URL`) — client is built and waiting.

### 12.2 Bank coverage expansion

- **Code-reuse wins (DE):** Norisbank, comdirect (Berlin Group; reuse
  Deutsche Bank / Commerzbank patterns).
- **Higher-effort DE:** N26, Solarisbank.
- **Skip:** 1,057 Sparkassen + Volksbanken individually — needs an
  aggregator (Tink) or a unified Berlin Group adapter.
- **EU expansion candidates:** Santander (ES/PT), BNP Paribas (FR),
  HSBC (UK/EU), BBVA (ES).

### 12.3 Infrastructure

- **Bank registry** — config-driven catalogue (name, country, spec,
  base URL, auth method) so adding a bank doesn't require a new
  client file.
- **Unified Berlin Group adapter** — single PSD2 NextGenPSD2 client
  for the ~80% of banks that conform; keep bespoke clients only for
  outliers (Nordea, UniCredit).
- **Token refresh / expiry** — mark connection `expired`, show
  Reconnect CTA.
- **Background sync** — periodic re-fetch per active connection.
- **Consent renewal** — auto-prompt before 90-day window expires.

### 12.4 AI building blocks

- **MCP server** wrapping accounts / transactions / balances /
  recurring detection as tools and resources, so Claude Desktop /
  Code can answer questions against the live SQLite. Hand-written
  JSON-RPC, not a framework. *Stretch:* a second MCP server over the
  Splunk-ready JSON log stream.
- **Anthropic-SDK agent** — swap Ollama for Claude in `agent.py`,
  grow toolset to ≈5 (`get_balance`, `get_transactions(filter)`,
  `get_recurring`, `categorize`, `web_search`). Target query:
  *"Why did my spending jump in March?"* — plan → call → observe →
  re-plan.
- **Override fixes for known LLM bias** — Indian IT firms ("Infosys
  Ltd", "Wipro") wrongly routed to `Housing` (LLM reads "Ltd" as
  property); salary-credit names ("Siemens AG", "PayPal Transfer")
  reclassified by name alone, losing the "incoming money" signal.
  Candidate fix: amount-aware prompt.

---

## 13. Acceptance / done criteria (current build)

- A new user can sign up, connect Nordea (sandbox), Commerzbank
  (sandbox), UniCredit (sandbox), or ING (sandbox example client) and
  see at least one account with ≥1 transaction within 60 seconds of
  consent.
- Seeded demo (`python3 seed_data.py`) produces 5 users, 12 accounts,
  and ~850 transactions, all categorised with no manual cleanup.
- Dashboard, Spending, Aggregation, Recurring all render for each
  seeded user with non-empty data.
- `backfill_categories.py` on 1,303 seeded transactions reclassifies
  ≥200 rows and merges legacy categories into the canonical 13.
- `agent.py "how much did I spend on groceries in the last 30 days?"`
  returns a numeric answer in ≤5 model turns on M-series hardware.
- Every domain event in §8 appears as a single-line JSON in
  `logs/fintnet.json` and is parseable by `jq`.
- Disconnecting a bank flips the row to `revoked`, historical
  analytics still work, and `connection.disconnect` is logged.
