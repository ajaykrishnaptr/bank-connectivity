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

## Logging (Splunk-ready)

Structured JSON events are written to `logs/fintnet.json` (rotated at 10 MB, keep 5 backups).

**Events emitted today:**
- `auth.login.success`, `auth.login.failed`, `auth.logout`, `auth.signup`
- `connection.upsert`, `connection.disconnect`
- `sync.complete` (with `latency_ms`, `account_count`, `bank`)
- `sync.account.skipped` (per-account 403s on ING)
- `categorize.cached`, `categorize.ai.failed`, `categorize.ai.unknown_label`, `categorize.json.parse_failed`
- `currency.fetch_failed` (frankfurter.app unreachable, falling back to hardcoded ECB rates)
- `ing.customer_token` (scope + expires_in after a successful ING code exchange)

Every event is a single-line JSON object with `ts`, `level`, `logger`, `event`, plus event-specific fields (`user_id`, `bank`, `email`, `latency_ms`, etc.) — all directly searchable in Splunk without regex parsing.

**Splunk integration paths:**
- **Universal Forwarder** (recommended) — install on the host, point at `logs/fintnet.json`. Resilient: app keeps running even if Splunk is down, events catch up when ingestion resumes.
- **HEC handler** — swap `RotatingFileHandler` in `logging_config.py` for an HTTP Event Collector handler if direct shipping is preferred.

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

---

## Learning roadmap — MCP, Agentic AI, GenAI

Concrete ways to layer modern AI capabilities onto FintNet for hands-on learning. Each topic maps cleanly onto a flagship addition with the highest learning value.

### 1. MCP — Build an MCP server that exposes FintNet's data

Wrap accounts, transactions, balances, and the recurring/waste detection as MCP tools and resources. Then Claude Desktop or Code can answer questions like *"what did Priya spend on groceries last month?"* against the live SQLite. Touches tool definitions, resource schemas, and the JSON-RPC handshake — the actual MCP protocol, not a framework abstraction.

*Stretch:* a second MCP server that wraps Splunk to let Claude query `logs/fintnet.json` events.

### 2. Agentic AI — A "financial advisor" agent over your own data

Use the Anthropic SDK directly (not LangChain — frameworks hide the agent loop you want to learn). Give it 4–5 tools: `get_balance`, `get_transactions(filter)`, `get_recurring`, `categorize`, `web_search`. Ask it open-ended questions like *"Why did my spending jump in March?"* and watch it plan → call tool → observe → re-plan until it converges. Teaches the core agent loop, error handling, and tool design.

*Stretch:* extend with a "TPP ops" agent that reads `logs/fintnet.json`, detects sync-rate regressions, and opens GitHub issues automatically (the existing scheduled-routines pattern is already half of this).

### 3. GenAI — LLM categorizer (✅ shipped)

`categorize.py` is now a three-tier hybrid: hand-curated overrides → SQLite cache → local LLM (Ollama + Qwen 2.5 3B) with few-shot examples. Toggle via `USE_AI_CATEGORIZER=true` in `.env`. New merchants get one ~10-second LLM call, then are cached forever.

**What it teaches:** classification prompts, constrained outputs, few-shot prompt engineering, cache-as-LLM-optimization, evaluation sets, the production "LLM + overrides" pattern, and structured JSON output.

**Files shipped:**
- `categorize.py` — three-tier router: overrides → cache → AI (with rules fallback). Includes `categorize_with_confidence()` returning `{category, confidence, reasoning}` JSON.
- `genai_test.py` — minimal "hello LLM" script
- `eval_categorizer.py` — compares rule-based vs AI on real seeded merchants (surfaces zero-shot vs few-shot tradeoffs)
- `genai_json_demo.py` — demo of structured JSON output with confidence + reasoning
- `backfill_categories.py` — re-categorizes every existing Transaction via AI; reports a before/after category diff
- `MerchantCategory` table in `models.py` — caches `(merchant → category, source)`

**Real-world results on the 1,303 seeded transactions (latest backfill):**
- 269 / 1,303 transactions updated, 15 distinct merchants flipped category
- Legacy category names auto-merged into the canonical 13 — `Food & Drink` (122 → 0), `Health` (95 → 0), `Transfer` (25 → 0); the rows redistributed into `Dining` (+108), `Healthcare` (+71), `Groceries` (+24), and `Utilities` (+17)
- `Food Delivery` (+14) split out from the old `Food & Drink` bucket — Lieferando recognised correctly
- Surfaced LLM bias bugs:
  - Indian IT companies ("Infosys Ltd", "Wipro") wrongly routed to `Housing` — fixed in the override table
  - Salary-credit merchant names (`Siemens AG`, `PayPal Transfer`, `Kleinanzeigen Sale`) reclassified by merchant string alone, losing the "this is incoming money" context — candidates for the override table or a future amount-aware prompt

*Stretch:* "Spending Q&A" chat — natural-language queries against the user's transactions ("restaurants over €50 in March"), which becomes a clean RAG-over-structured-data exercise.

### Suggested order for max learning compounding

1. Start with the **GenAI categorizer** (smallest scope, sharpest before/after, teaches caching + cost discipline).
2. Then the **MCP server** (a clean data layer is already in place to expose).
3. Then the **financial advisor agent** (which can reuse the MCP server as its tools — that's the elegant part).
