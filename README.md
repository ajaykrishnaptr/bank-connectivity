# FintNet — Financial Institutions Integration Network

Connect European bank accounts in one place. FintNet fetches accounts, balances, and transactions via secure bank APIs and presents a unified view — including cross-border, multi-currency consolidation, and **on-device transaction categorisation powered by a local LLM (Ollama + Qwen 2.5 3B)** — so no cloud-LLM calls, no per-call AI costs, and transaction data never leaves the machine for categorisation.

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
| UniCredit | Italy | mTLS + PSD2 consent SCA | Ready (sandbox) |
| Synthetic Bank | DE, FI, SE, NL, IT | Berlin Group AIS test bank inside this app, simulated SCA | Ready (13 months of generated history, daily feed) |
| ING | Netherlands, Belgium, Germany | mTLS + HTTP Signatures + OAuth2 authorization_code | Working with sandbox example client |

### ING flow specifics

ING is the most complex integration:
- Two **separate key pairs** required: TLS for mTLS, signing for HTTP Request Signatures
- Two **different keyId formats** depending on endpoint:
  - App token (`client_credentials`): `keyId="SN=<cert-serial-hex>"`, signature in `Authorization` header, requires `TPP-Signature-Certificate` header
  - All Bearer-token calls (code exchange, AIS): `keyId="<client_id>"`, signature in `Signature` header, no TPP cert
- Sandbox example client uses pre-registered redirect URI `https://www.example.com/`. After authorization the user lands there with `?code=...` in the URL bar — paste it at `/ing/enter-code` to complete connection.
- Per-account grants vary by sandbox test profile — `_fetch_and_store` catches `INGApiError` 403s and skips accounts without grant, so the connection still saves.

### UniCredit flow specifics — self-hosted OCSP & CRL

The UniCredit gateway is strict about revocation:
- It **hard-fails** if the AIA OCSP host is unreachable (NXDOMAIN counts as a hard fail, not "OCSP unavailable").
- It **refuses `https://` CRL DPs** — most F5 SSL profiles only follow `http://` (or `ldap://`) for revocation lookups, to avoid a chicken-and-egg of "validate this cert chain by validating *that* cert chain".

So a vanilla self-signed eIDAS chain isn't enough. The leaf has to point at OCSP and CRL endpoints we actually run, over plain HTTP. Setup:

- `generate_psd2_cert.py` mints the QWAC chain (root + intermediate + leaf) with ETSI PSD2 `qcStatements` (PSP_AI role, BaFin authority, PSDDE-BAFIN-19337 ID). Leaf AIA / CRLDP point at:
  - `http://ocsp.fintnet.ai` — OCSP responder
  - `http://crl.fintnet.ai/crl.crl` — CRL distribution point
  - `http://crl.fintnet.ai/inter.crt` — AIA CA Issuers (path validation fallback)
- `generate_ocsp_signer.py` mints a delegate OCSP signer (EKU `id-kp-OCSPSigning`, plus `id-pkix-ocsp-nocheck` to break recursion). The intermediate's private key never leaves the laptop — the VM only holds the delegate's key.
- Both endpoints are served from a single Always-Free Oracle Cloud VM:
  - `openssl ocsp -port 8888` is the signing backend (loads `chain.crt`, `ocsp_signer.{crt,key}`, and an `index.txt` listing valid serials).
  - A ~50-line Python proxy (stdlib only) listens on port 80, serves `/crl.crl` + `/inter.crt` static, and forwards every other request to the openssl backend.
  - Both as systemd services. Two A records (`crl`, `ocsp`) at GoDaddy point at the VM.
- After every leaf re-issue, `generate_psd2_cert.py` also writes `certs/ocsp_index.txt` — the new serial gets a `V` (valid) line that's pushed to the responder so OCSP returns "good" for it.
- CRL housekeeping is split into two scripts: `refresh_crl.py` re-signs an empty CRL with the intermediate (the `nextUpdate` field is 30 days out, so this is run monthly); `deploy_crl.sh` scps the fresh CRL up to the Oracle VM and restarts the proxy.

The bank still has to import `chain.crt` once into its trust store; subsequent leaf re-issues under the same intermediate don't need re-trust.

---

## Transaction categorisation — local LLM, fully offline

Every transaction is auto-categorised into one of 14 buckets (Groceries, Utilities, Dining, Income, ...) by a **four-layer waterfall** designed so the cheapest layers handle the easy cases and the model is consulted as a last resort:

1. **Hand-curated overrides** — short list of merchants the LLM is reliably wrong about (e.g. "Infosys Ltd" → Income, not Housing). Beats every layer below.
2. **SQLite cache** (`MerchantCategory` table) — once we've seen "Lieferando", we never ask the LLM about it again. Subsequent transactions are a single row read.
3. **Local LLM** — Ollama running **Qwen 2.5 3B** on the developer's machine. ~10 seconds on a brand-new merchant, then cached forever. Few-shot prompted with examples spanning German + Indian + Nordic merchants for cross-border accuracy.
4. **Keyword rules** — pure-Python fallback if Ollama isn't running or returns garbage.

Toggle the AI path with `USE_AI_CATEGORIZER=true` in `.env`. `false` → deterministic rules only (useful in tests).

### Why local instead of OpenAI / Anthropic API?

- **🔒 Privacy** — bank transactions never leave the device. The whole point of a TPP is to be a trustworthy custodian of financial data; sending merchant strings to a cloud LLM would undo that.
- **💰 Cost** — categorising 1,303 seeded transactions cost **€0**. A cloud-LLM equivalent at scale (10k users × dozens of new merchants/month) would compound into real spend; the cache + local-model design proves the architecture works on a laptop and scales without a per-call line item.
- **🎯 Determinism** — `temperature=0` plus the cache means the same merchant string produces the same category every time. Reproducible analytics.
- **⚡ No rate limits, no network dependency** — works offline, no provider outage to plan around.

### Structured JSON output with confidence + reasoning

`categorize_with_confidence(merchant)` asks the model for a structured response:

```json
{
  "category": "Utilities",
  "confidence": "high",
  "reasoning": "Vattenfall is a Swedish electricity and heating provider"
}
```

Powers the dashboard's "explain" panel and the `genai_json_demo.py` demo script. Defensive parsing handles markdown code fences and trailing comments — the model occasionally wraps its answer in ```json fences``` and we strip them before `json.loads`.

### Real-world impact (latest backfill on 1,303 seeded transactions)

Running `python3 backfill_categories.py`:

- **269 / 1,303 transactions** reclassified, **15 distinct merchants** flipped bucket
- **Legacy category names auto-merged into the canonical 13:** `Food & Drink` (122 → 0), `Health` (95 → 0), `Transfer` (25 → 0). Rows redistributed into `Dining` (+108), `Healthcare` (+71), `Groceries` (+24), `Utilities` (+17), `Food Delivery` (+14)
- **Surfaced LLM bias bugs that became override entries:**
  - Indian IT companies ("Infosys Ltd", "Wipro") → wrongly routed to `Housing` (the LLM reads "Ltd" as a property firm)
  - Salary-credit merchant strings ("Siemens AG", "PayPal Transfer") get reclassified by name alone, losing the "this is incoming money" context — candidate for an amount-aware prompt

### Files

| File | Purpose |
|------|---------|
| `categorize.py` | The four-layer waterfall + `categorize_with_confidence()` for JSON output |
| `backfill_categories.py` | Re-categorise every existing Transaction; print before/after category diff |
| `eval_categorizer.py` | Compare keyword rules vs AI on real seeded merchants — surfaces zero-shot vs few-shot tradeoffs |
| `genai_json_demo.py` | Demo of structured JSON with confidence + reasoning |
| `genai_test.py` | Minimal "hello LLM" prompt for hacking on the prompt template |
| `agent.py` | Tool-using agent loop over the transactions DB (see section below) |
| `models.MerchantCategory` | DB cache so the LLM sees each unique merchant at most once |

### Setup

```bash
# 1. Install Ollama (https://ollama.com) and pull the model
ollama pull qwen2.5:3b

# 2. Enable the AI path in .env
echo "USE_AI_CATEGORIZER=true" >> .env

# 3. Seed test data — categorisation runs automatically
python3 seed_data.py

# 4. (Optional) Backfill any pre-existing transactions
python3 backfill_categories.py
```

What it teaches: classification prompts, constrained outputs, few-shot prompting, cache-as-LLM-cost-control, the production "LLM + overrides" pattern, structured JSON output, and evaluation sets.

---

## Tool-using agent (`agent.py`)

A small CLI that turns the LLM from a *classifier* into a *planner*. Run it like:

```bash
python3 agent.py "how much did I spend on groceries in the last 30 days?"
```

The script implements the canonical agent loop by hand — no LangChain, no framework — so the moving parts are visible:

1. **System prompt** declares two tools (`total_spent`, `top_merchants`) and a strict JSON output schema with two shapes: `{"action": "tool_call", ...}` or `{"action": "answer", ...}`.
2. **JSON mode** (`format="json"` on the Ollama chat API) forces every model turn to be parseable in one `json.loads()` — no regex over prose.
3. **Loop** — at most 5 turns: parse → if tool call, run the Python function and append the result as a `role: "tool"` message → if answer, print and exit.

Tools deliberately return *small scalar summaries* (`{count, inflow, outflow, net}`) instead of raw transaction lists. Real-world lesson learned the hard way: dumping 50 rows back into the prompt blew up CPU prompt evaluation on `qwen2.5:3b` and timed out the loop. Agent frameworks paginate, summarise, or use handles for the same reason — context is expensive.

**Status: seed implementation, runs end-to-end on M-series hardware.** On Intel/CPU-only Macs each turn is minutes, not seconds — the loop is correct but the model is too slow to be useful there. This is the foundation for the Anthropic-SDK agent listed under *Next AI building blocks* below.

---

## Running the app

```bash
python3 app.py
```

Visit: https://127.0.0.1:5000 (the dev server uses an adhoc self-signed cert, so accept the browser warning).

The login page lists every demo persona under **"Try a demo account"** — click one to auto-fill the email and the shared password, then sign in. No need to look up credentials.

If the database is empty (a fresh checkout, or a serverless cold start), the app **auto-seeds** the demo personas on first request, so `python3 seed_data.py` is only needed when you want to wipe and re-seed manually.

---

## Deploying to Vercel (live sandboxes, Postgres, cron)

Production runs on Vercel with live PSD2 sandbox connections. `vercel.json` routes all traffic to `api/index.py` and schedules 4 daily cron jobs.

| Concern | How it works |
|---|---|
| Certificates | Leaf certificates and keys are sensitive env vars (`UC_CERT_B64`, `UC_KEY_B64`, `ING_TLS_CERT_B64`, `ING_TLS_KEY_B64`, `ING_SIGNING_CERT_B64`, `ING_SIGNING_KEY_B64`). `runtime_certs.py` writes them to `/tmp` with mode 0600 on cold start. `certs/` is never uploaded; root, intermediate and OCSP-signer keys never leave the laptop. |
| Database | Neon Postgres from the Vercel Marketplace (`DATABASE_URL`). Consents and tokens survive across function instances. |
| Sessions | `FLASK_SECRET_KEY` is required on Vercel (the app refuses to start without it). |
| UniCredit redirect | `REDIRECT_URI=auto` builds `https://<current host>/callback`, so the flow works on `app.fintnet.ai` and on the `vercel.app` alias. |
| Nordea | The sandbox mock authorizer returns the code in the `Location` header, so the registered redirect URI is only echoed, never visited. Country (FI or SE) is chosen on the consent page. |
| ING | The sandbox example client redirects to `https://www.example.com/`; paste the code on `/ing/enter-code`. |
| OCSP and CRL | Stay on the Oracle VM (`ocsp.fintnet.ai`, `crl.fintnet.ai`): UniCredit requires plain HTTP for revocation checks and Vercel forces HTTPS. Refresh the CRL monthly with `./deploy_crl.sh` from the laptop. |
| Model | Claude Haiku 4.5 (`ANTHROPIC_API_KEY`), capped by `LLM_MAX_CALLS` per instance. |
| Tracing | Langfuse (`LANGFUSE_*`) plus a Splunk HEC event log: Upstash Redis on Vercel (`KV_REST_API_URL`, `KV_REST_API_TOKEN`), `logs/events.jsonl` locally. |

### Cron jobs (Vercel Hobby: once a day each, within the scheduled hour)

| UTC | Route | Does |
|---|---|---|
| 01:00 | `/cron/feed` | Books missing days in the synthetic bank (up to 7 per run), deletes history older than 13 months, re-syncs connected users |
| 02:00 | `/cron/categorise` | Upgrades provisional rule categories with Claude Haiku (capped per run) |
| 03:00 | `/cron/evaluate` | Creates the Langfuse dataset `fintnet-categorisation-daily/<date>` and runs one experiment on it |
| 04:00 | `/cron/health` | CRL next update, OCSP status of the UniCredit leaf, certificate expiry |

Every route requires `Authorization: Bearer $CRON_SECRET`, is idempotent per date (`job_runs` table, `?force=1` to rerun) and shows on the **Ops** page.

```bash
curl -H "Authorization: Bearer $CRON_SECRET" https://app.fintnet.ai/cron/feed
```

---

## Synthetic bank

A Berlin Group NextGenPSD2 AIS test bank inside the app (`synthbank/`), because the real sandboxes return a handful of static transactions.

- **Customers:** the 5 demo logins plus an evaluation population (default 200) across DE, FI, SE, NL and IT.
- **History:** a rolling 13 months, seeded once (`python seed_data.py`), then one day at a time by the feed cron.
- **Random categories, honest labels:** each discretionary purchase draws a category weighted by persona, then a merchant that truly belongs to it: about 60% known merchants, 30% new merchants from templates and 10% hard cases (payment-facilitator prefixes, truncation, typos, misleading names). New merchants keep the model tier doing work instead of turning into a cache lookup. No language model writes the data.
- **Label firewall:** ground truth lives in `sb_labels` and is read only by the evaluation job. The API (`/synthetic-bank/v1/...`), the tools and the model never see it.
- **API:** `POST /synthetic-bank/v1/consents`, `GET /synthetic-bank/v1/accounts`, `.../balances`, `.../transactions` with a `Consent-ID` header; simulated SCA at `/synthetic-bank/authorise/<consent>`.

## Money questions assistant (`/ask`)

Claude Haiku answers questions across every connected bank using 7 deterministic tools (`assistant.py`): accounts, spending by category, top merchants, monthly cash flow, period comparison, recurring payments and transaction search. Code computes every figure; the model chooses tools and words the answer. Credit, loan and investment questions are refused in code before any model call, and the page discloses that answers come from an AI system.

## Evaluations in Langfuse

| Dataset | Built by | Graded |
|---|---|---|
| `fintnet-categorisation-daily/<date>` | `/cron/evaluate` every day | Model accuracy overall, on new merchants and hard cases, confidence calibration, against the keyword-rules baseline |
| `fintnet-categorisation-benchmark` | `python evals/categoriser_experiment.py sync-benchmark` | Same, on a fixed 300-item sample for comparing prompts and models |
| `fintnet-assistant-questions` | `python evals/assistant_experiment.py sync` | Tool choice and the key figure in the answer (code), plus a Groq `openai/gpt-oss-120b` judge on faithfulness, scope and concision |

Run an experiment: `python evals/assistant_experiment.py run` or `python evals/categoriser_experiment.py run-benchmark`. Results are also saved to `evals/results/`.

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

```bash
python3 seed_data.py                  # 5 demo logins + 200 evaluation customers, 13 months each
python3 seed_data.py --population 0   # demo logins only (what an empty database gets on startup)
```

Re-runnable: only missing users, customers and history are created.

---

## Test accounts

All accounts use password **`TestPass123`**; the login page lists them as one-click chips.

Each login is the test user that exists in one bank's PSD2 sandbox, so the sandbox SCA screen and the returned account owner match the login. Each is also a synthetic bank customer with 13 months of data, connected on seed.

| Login | Email | Live sandbox | Sandbox SCA |
|------|-------|-------|-------|
| Thomas Mann | `thomas.mann@example.de` | Commerzbank, PSU-ID `DE80480800200405423400` | Pre-approved sandbox consent |
| Aino Salo | `aino.salo@example.fi` | Nordea FI | Mock authorizer `70311198` |
| Margit Alros | `margit.alros@example.se` | Nordea SE (SEK and EUR accounts) | Mock authorizer `70311198` |
| A van Dijk | `a.vandijk@example.nl` | ING NL, profile "Hr A van Dijk, Mw B Mol-van Dijk" | Pick the profile, paste the code |
| Mario Rossi | `mario.rossi@example.it` | UniCredit IT | Sandbox user `ituser2bgk` (UniCredit developer portal, Test Data) |

---

## Roadmap (technical)

**More banks to integrate**
- Santander (Spain/Portugal) — Berlin Group, good sandbox
- BNP Paribas (France) — Berlin Group, large retail footprint
- HSBC (UK/Europe) — post-Brexit but active PSD2 API
- BBVA (Spain) — Berlin Group, strong open API ecosystem

**Infrastructure to add**
- Bank registry — config-driven bank catalogue (name, country, spec, base URL, auth method) so adding a new bank doesn't require a new client file
- Unified PSD2 adapter — single client that handles Berlin Group NextGenPSD2 spec (covers ~80% of EU banks); keep bespoke clients only for non-standard banks (Nordea, UniCredit)
- Token refresh / expiry handling — mark connection as `expired`, show Reconnect button
- Consent renewal — auto-prompt users before 90-day consent windows expire

---

## Next AI building blocks (planned)

The local-LLM categoriser is shipped (above). Two further AI capabilities are next:

### MCP server exposing FintNet's data

Wrap accounts, transactions, balances, and the recurring/waste detection as MCP tools and resources, so Claude Desktop or Code can answer questions like *"what did Max spend on groceries last month?"* against the live SQLite. Touches tool definitions, resource schemas, and the JSON-RPC handshake — the actual MCP protocol, not a framework abstraction.

*Stretch:* a second MCP server that wraps Splunk to let Claude query `logs/fintnet.json` events.

### Agentic "financial advisor" over your own data

A first iteration ships as `agent.py` (see section above) — local LLM + two tools, hand-written loop. The next step is to swap Ollama for the Anthropic SDK so the model is fast and capable enough for open-ended planning, and grow the toolset to 4–5 entries: `get_balance`, `get_transactions(filter)`, `get_recurring`, `categorize`, `web_search`. Goal: ask *"Why did my spending jump in March?"* and watch it plan → call tool → observe → re-plan until it converges.

*Stretch:* "Spending Q&A" chat — natural-language queries against the user's transactions ("restaurants over €50 in March") as a clean RAG-over-structured-data exercise.
