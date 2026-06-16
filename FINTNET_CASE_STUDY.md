# FintNet — Product Case Study

> **One-liner:** A self-hosted Open Banking aggregator that turns five European banks
> into one private dashboard — and then does the thing a dashboard never does: tells you
> *where to stop wasting money.*
>
> **Author:** Ajay Krishna · **Stage:** Working sandbox/demo, pre-production · **Updated:** 2026-06-04
>
> *This is the narrative I walk through in an interview. Detailed market sizing, competitor
> teardown, business-model options, and the metrics tree live in [STRATEGY_AND_METRICS.md](./STRATEGY_AND_METRICS.md).
> The functional spec lives in [PRD.md](./PRD.md).*

---

## TL;DR (the 30-second version)

I believed there was an unserved wedge in EU personal finance: people with accounts at
**multiple banks across currencies** have no private, single view of their money — and the
tools that aggregate it (a) resell your data and (b) stop at *showing* you charts instead of
*acting* on them.

Rather than mock this, I **built the hardest part first**: live PSD2 connectivity to five
European banks, each with a different auth and crypto model, plus the eIDAS certificate and
revocation infrastructure a licensed third party actually needs. That de-risked the
feasibility question most PMs only hand-wave. On top of it I built the part that's actually
the product — an **insight engine** that surfaces redundant subscriptions, silent price
hikes, lapsed memberships, and fixed-cost burden, and rolls them into a single number:
**money you could save this month.**

That number is my **North Star**. It is simultaneously the user's value, the product's
health metric, and the demo's hook.

---

## 1. The problem

**Whose problem:** an EU-resident knowledge worker — a freelancer banking in Germany and
Sweden, a salaried employee with a Finnish daily account and a German savings pot — who holds
**2–4 bank accounts across at least one currency boundary.**

For this person, three things are simultaneously true:

1. **Their money is fragmented.** No single bank app shows the whole picture, and mental
   arithmetic across EUR/SEK is where budgeting quietly dies.
2. **The aggregators that solve fragmentation monetise the data.** Plaid, Tink (now Visa),
   TrueLayer, Yapily — their business *is* the data pipe. Consumer apps built on them inherit
   that posture. A privacy-conscious technical user actively does not want their transaction
   stream sitting in a third-party cloud, especially once an LLM is in the loop.
3. **Even when aggregated, the output is passive.** Every PFM app converges on the same
   donut chart. A chart tells you that you spent €340 on "Entertainment." It does *not* tell
   you that you're paying for Netflix, Disney+, *and* Spotify on two different cards and one
   of them silently went up €3. That gap — between **data** and **decision** — is the real
   unmet need.

**The insight that organises everything below:** *fragmentation is the table-stakes problem;
the actual product is the layer that converts an aggregated ledger into a small number of
actions a user will actually take.*

---

## 2. Why now

- **PSD2 made direct, license-based bank access a legal right** across the EU — no
  screen-scraping, no reseller required. A determined builder can connect *directly*.
- **eIDAS QWAC certificates** give an independent party the cryptographic identity to act as
  a Third-Party Provider (TPP). The barrier is operational rigor, not permission.
- **Local LLMs crossed the usefulness line.** A 3B-parameter model now runs on a laptop fast
  enough to categorise a transaction stream — which means the privacy promise ("your bank
  data never leaves the device") is finally *technically* deliverable, not just marketing.

The combination is what's new: the legal right + the crypto identity + on-device AI is the
first time a *private-by-architecture* aggregator-with-insight is buildable by a small team.

---

## 3. What I built (and why building it was the point)

Most product portfolios stop at a Figma flow. FintNet's differentiator is that the riskiest
assumption — *"can an independent party actually get clean, direct, multi-bank data?"* — is
**proven, not assumed.**

### 3a. The hard, de-risking layer: real connectivity

Five banks, **four genuinely different auth models** — this is the part you cannot fake and
that taught me what the spec documents hide:

| Bank | Country | Auth model | Status |
|---|---|---|---|
| Nordea | FI/SE/NO/DK | OAuth2 auth-code + hosted SCA | Ready |
| Commerzbank | DE | OAuth2 client-credentials + consent | Ready |
| UniCredit | IT | mTLS QWAC + Berlin Group consent + SCA | Ready (sandbox) |
| Deutsche Bank | DE | OAuth2 + Berlin Group consent + SCA | Built, awaiting creds |
| ING | NL/BE/DE | mTLS + HTTP request signatures + OAuth2 | Working (sandbox) |

What building it surfaced — the constraints a slide deck never would:

- **SCA is the activation cliff.** Every connection routes through the bank's own
  strong-authentication page. That redirect is the single biggest drop-off risk in the funnel,
  and it's outside my control — which makes "connections completed / connections started" a
  metric I have to design *around*, not just measure.
- **Consent is leaky and per-account.** ING's sandbox returns `403` on individual accounts
  even with valid scopes; I had to make sync *tolerate partial failure* rather than fail the
  whole connection. Real-world data is never all-or-nothing.
- **Revocation infrastructure is a real cost of being a TPP.** UniCredit's gateway hard-fails
  if my certificate's OCSP responder is unreachable and refuses `https://` CRL endpoints — so
  I stood up a self-hosted OCSP responder and CRL distribution point on an always-free cloud
  VM. *Lesson for the business case: "become your own aggregator" has an ongoing operational
  tax, and that tax is a real input to build-vs-buy.*

### 3b. The product layer: insight, not a ledger

- **On-device categorisation.** Every transaction is sorted into one of 14 buckets by a local
  LLM (Ollama / Qwen 2.5 3B), with a cache + hand-curated overrides + a keyword fallback. Bank
  data never touches a third-party cloud. This is the privacy promise made literal.
- **The "wasted spend" detector** — the actual product. Four signal families:
  1. **Redundant** — two streaming/fitness subs doing the same job.
  2. **Price creep** — a fixed subscription whose price quietly climbed.
  3. **Lapse** — a gym or transit pass you've likely stopped using (inferred from adjacent
     behaviour, e.g. a transit pass + a pile of recent Ubers).
  4. **Burden** — total fixed auto-debits as a share of income.
- **A natural-language agent** (`agent.py`) that answers *"how much did I spend on groceries
  in the last 30 days?"* by planning tool calls — a seed of the conversational future.

### 3c. Production hygiene that signals seriousness

Per-user data isolation at the query layer, passwords hashed, structured JSON logging that's
Splunk-ready out of the box, idempotent demo seeding (5 personas, ~850 transactions). This
isn't a prototype that falls over when you click twice.

---

## 4. North Star & the metric tree

**North Star Metric: € of potential savings surfaced per active user per month.**

Why this one:
- It is the **user's value** (money found) and the **product's value** in the same unit.
- It is **leading, not lagging** — it predicts retention better than logins, because a user
  who was just shown €18/month of waste has a reason to come back.
- It **forces the right product behaviour.** Optimising it means getting categorisation,
  recurring-detection, and signal precision right — not adding vanity charts.

The supporting tree (full version in the strategy doc):

```
              € savings surfaced / active user / month   ← North Star
             /                    |                      \
   Activation             Insight quality            Engagement
   (banks connected,      (signals/user,             (return visits,
    consent completed)     precision, % acted-on)     alerts acted-on)
```

The honest counterpart metric I'd watch to avoid gaming it: **signal precision / dismissal
rate.** Surfacing €500 of "savings" the user immediately dismisses as wrong destroys trust
faster than surfacing nothing. The North Star only counts if the insights are *right*.

---

## 5. Competitive wedge

| Who | What they nail | The gap FintNet attacks |
|---|---|---|
| Plaid / Tink / TrueLayer | The data pipe (infra) | They sell *to* apps; the consumer's data is the product. Not private-by-design. |
| Emma / Snoop / Cleo / Money Dashboard | Consumer PFM polish | Cloud-hosted data; insight is mostly charts + generic nudges; UK-centric. |
| Bank-native apps (Revolut, N26) | One slick account | Single-bank by definition — they *are* the fragmentation. |

**FintNet's wedge:** *private-by-architecture* (on-device AI, direct PSD2, no reseller) +
*action-first insight* (the savings number, not the donut) + *cross-border by default*
(multi-currency is a first-class citizen, not an afterthought).

It is deliberately **not** trying to out-polish Revolut or out-scale Plaid. It owns the
narrow intersection: *technical, privacy-conscious, multi-bank EU users who want to be told
what to cancel.*

---

## 6. Business model — how this becomes a company

The current build is intentionally single-user and free (it's a reference implementation).
For the interview question *"so how does it make money?"*, three credible paths, in order of
conviction:

1. **Prosumer subscription (most likely).** A privacy-first PFM at €4–6/month for unlimited
   bank connections + the full insight engine. The savings number *is* the pricing
   justification: "we find you €X/month, we cost €5." Conversion is honest when value is
   quantified.
2. **B2B2C "insight engine as a feature."** License the on-device categorisation + waste
   detection to challenger banks and credit unions who have the data but not the insight
   layer — and who increasingly need a privacy story. The connectivity work becomes a moat
   here, not the product.
3. **Affiliate / switching (lowest conviction, highest scrutiny).** When we detect a
   redundant or overpriced sub, facilitate the switch/cancel. Real revenue but a direct
   conflict-of-interest risk with the trust the product is built on — I'd gate this behind
   strict "user-interest-first" rules or not ship it.

The strategy doc carries TAM/SAM/SOM and the reasoning behind these.

---

## 7. Prioritised roadmap (Now / Next / Later)

Framed as an argument about *sequence*, not a feature pile. Each item tagged with its driver.

**Now — make the value undeniable & retainable**
- **Surface the North Star in the UI** — a "we found €X/month" headline on the home page.
  *Driver: the value is computed but invisible; this is the cheapest activation lever.*
- **Background re-sync + consent-renewal prompts.** *Driver: PSD2 consent expires at 90 days;
  silent data death is the #1 retention killer for an aggregator.*

**Next — widen the funnel & deepen trust**
- **Config-driven bank registry + unified Berlin Group adapter** so adding a conformant bank
  is config, not a new client file. *Driver: coverage is the top reason a multi-bank user
  bounces; this makes coverage cheap to grow.*
- **Signal precision work** (the override fixes for known categorisation bias). *Driver:
  protects the counter-metric; trust compounds.*

**Later — expand the surface**
- Conversational agent over the live data (swap the local model for a frontier model).
- B2B2C packaging of the insight engine.
- Payment initiation (PIS) to *act* on insights, not just surface them.

---

## 8. What I'd do differently / what I learned as a PM

- **I over-invested in connectivity breadth before validating the insight loop.** Five banks
  is a great *feasibility* proof but a poor *prioritisation* proof — a sharper PM would have
  shipped two banks + the savings number to a handful of real users first. I'm treating the
  "Now" roadmap as the correction.
- **The hardest product decisions were trust trade-offs, not technical ones** — e.g. whether
  to ever monetise via affiliate switching. Building it taught me that the *architecture*
  (on-device AI) is itself a product-strategy decision, not just an engineering one.
- **Measuring the right thing is harder than building it.** "Money saved" is the right North
  Star only if paired with a precision guardrail; I'd want real-user dismissal data before
  trusting the number.

---

### Appendices
- **[STRATEGY_AND_METRICS.md](./STRATEGY_AND_METRICS.md)** — market sizing, competitor teardown, business-model detail, full metrics tree.
- **[PRD.md](./PRD.md)** — functional requirements, data model, bank-by-bank connectivity matrix, security requirements.
