"""
Money questions across every connected bank (use case F4).

Design rule: code computes every number, the model only decides which tools
to call and words the answer. The tools below query the user's own
transactions (all connected banks, converted to EUR the same way the Balances
page does). The model never sees raw rows beyond what a tool returns.

Guardrails:
  * Credit, loan, creditworthiness and investment questions are refused in
    code before any model call (AI Act Annex III 5(b) keeps credit decisions
    out; investment advice is regulated), and the system prompt repeats it.
  * Affordability questions ("can I afford X?") are answered in code with the
    surplus figure only. The judgement is declined and no model is called.
  * At most MAX_ROUNDS tool rounds per question; per-instance call cap in llm.py.
  * Every question is one Langfuse trace; every tool call a child span.
  * The page discloses that answers come from an AI system (AI Act Article 50).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Callable

import currency_utils
import db_utils
import llm
import observability
import prompts
from categorize import CATEGORIES
from models import Account, BankConnection, Transaction, db

MAX_ROUNDS = 6
MAX_TOKENS = 1500

# Every phrasing here must be caught by code, because the model refusing on its
# own judgement is not a control. "Should I take out an overdraft?" reached the
# model, which declined sensibly, and nothing recorded that a refusal had
# happened. Borrowing, investing and retirement money are all outside the line.
_REFUSE = re.compile(
    r"\b(loans?|mortgages?|remortgages?|credit ?scores?|creditworthiness|borrow(ing)?|kredite?|darlehen|schufa|"
    r"overdrafts?|dispo(kredit)?|refinanc\w+|lend(ing|er)?|debts?|consolidat\w+|interest rates?|apr|"
    r"invest(ing|ment|ments|or|ors)?|stocks?|shares|etfs?|isas?|sparplan|brokers?|"
    r"(index|mutual|investment|hedge) funds?|fonds|"
    r"crypto(currency|currencies)?|bitcoin|aktien?|geldanlage|"
    r"pensions?|retirement|rente|altersvorsorge|"
    r"should i buy)\b", re.IGNORECASE)

REFUSAL = ("I can't help with credit, loans or investment decisions. I can answer questions about your "
           "spending, income, balances and recurring payments across your connected banks.")

# "Can I afford X?" is not a credit decision, so _REFUSE is the wrong tool for
# it. It is still a judgement about the customer's finances, and the model is
# not the thing that should be making it. The line we hold is narrower than a
# refusal: code states the arithmetic it can stand behind, and declines the
# yes or no. No model call happens on this path at all.
_AFFORD = re.compile(r"\bafford\b|\bleisten\b|\bdo (i|we) have enough\b|\bcan (i|we) spend\b",
                     re.IGNORECASE)


def _affordability_answer(tools: "Tools") -> str:
    """The surplus, the window it came from, and what the figure cannot know."""
    months = tools.monthly_cash_flow(7)["months"]
    full = [m for m in months if m["month"] != f"{date.today():%Y-%m}"]
    if not full:
        return ("Whether you can afford something is your decision, and I won't make it for you. "
                "I also don't yet hold a full month of history to show you the arithmetic.")
    nets = [m["net_eur"] for m in full]
    avg = round(sum(nets) / len(nets), 2)
    return (
        "Whether you can afford it is your decision, and I won't make it for you. "
        f"The arithmetic I can stand behind: across the {len(full)} full months from "
        f"{full[0]['month']} to {full[-1]['month']}, your income minus your spending averaged "
        f"{avg:,.2f} EUR a month, ranging from {min(nets):,.2f} to {max(nets):,.2f}. "
        "That figure knows nothing about your savings, anything you have already committed to, "
        "or a change in your income, so it is not a yes or a no."
    )

# The system prompt lives in Langfuse (prompts.py holds the fallback text).

TOOLS: list[dict] = [
    {"name": "list_accounts",
     "description": "All connected accounts with bank, currency, balance in native currency and EUR, the first and last transaction date, plus the total balance, the balance per bank and the number of accounts per bank. Call this to learn what data exists.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "spending_by_category",
     "description": "Money paid out per category between two dates (inclusive), in EUR, with transaction counts per category and in total. Optional bank filter. Use this for \"how much\" and \"how many times\" questions about a period.",
     "input_schema": {"type": "object", "properties": {
         "date_from": {"type": "string", "description": "YYYY-MM-DD"},
         "date_to": {"type": "string", "description": "YYYY-MM-DD"},
         "bank": {"type": "string", "description": "optional bank id: unicredit, commerzbank, nordea or ing"},
         "category": {"type": "string", "description": "optional single category, e.g. Groceries"}},
         "required": ["date_from", "date_to"], "additionalProperties": False}},
    {"name": "top_merchants",
     "description": "Merchants the user paid most between two dates, in EUR, optionally within one category.",
     "input_schema": {"type": "object", "properties": {
         "date_from": {"type": "string"}, "date_to": {"type": "string"},
         "category": {"type": "string"}, "limit": {"type": "integer", "description": "1-20, default 10"}},
         "required": ["date_from", "date_to"], "additionalProperties": False}},
    {"name": "monthly_cash_flow",
     "description": "Income, spending and net per calendar month for the last N months (1-13), in EUR.",
     "input_schema": {"type": "object", "properties": {"months": {"type": "integer"}},
                      "required": ["months"], "additionalProperties": False}},
    {"name": "compare_periods",
     "description": "Spending per category in period A versus period B, with the difference and percentage change, in EUR. Use for questions like 'why did my spending jump in March'.",
     "input_schema": {"type": "object", "properties": {
         "a_from": {"type": "string"}, "a_to": {"type": "string"},
         "b_from": {"type": "string"}, "b_to": {"type": "string"}},
         "required": ["a_from", "a_to", "b_from", "b_to"], "additionalProperties": False}},
    {"name": "recurring_payments",
     "description": "Recurring payments (fixed subscriptions and variable bills), recurring income, and wasted-spend signals such as price rises and duplicate subscriptions.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "find_transactions",
     "description": "Individual transactions matching filters, at most 25 listed, WITH totals and extremes over every match. "
                    "Use count_matched for \"how many times\", total_eur for \"how much in total\", and largest/smallest "
                    "for \"biggest\" or \"smallest\" questions. Never add up the listed rows yourself and never treat the "
                    "first listed row as the largest: only some rows are listed. sort=amount orders the listing by size "
                    "instead of by date. bank restricts it to one connected bank, so per-bank totals and extremes are "
                    "answerable. If matched_nothing comes back, the filter found nothing: prefer category over "
                    "merchant_contains and retry before telling the customer they spent nothing.",
     "input_schema": {"type": "object", "properties": {
         "date_from": {"type": "string"}, "date_to": {"type": "string"}, "category": {"type": "string"},
         "merchant_contains": {"type": "string"},
         "direction": {"type": "string", "enum": ["out", "in", "any"]},
         "min_amount_eur": {"type": "number"}, "limit": {"type": "integer"},
         "sort": {"type": "string", "enum": ["date", "amount"]},
         "bank": {"type": "string"}},
         "additionalProperties": False}},
]


def _d(value: Any, default: date) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return default


UNRESOLVED_CATEGORY = "\x00unresolved"


def _category(name: str | None) -> str | None:
    """The app's category whose name matches, whatever case, spacing or wording the model used.

    Returns UNRESOLVED_CATEGORY when nothing matches, so the caller can say so.
    An earlier version fell back to the model's own string, and the tools then
    reported a confident zero for a category that does not exist: asked about
    "Health and Fitness", the assistant answered that nothing had been spent,
    while 12 transactions sat under "Health & Fitness".
    """
    if not name:
        return None
    def key(text: str) -> str:
        return re.sub(r"[^a-z]", "", text.lower())
    k = key(name)
    for category in CATEGORIES:
        if key(category) == k:
            return category
    # A near miss on a two-word name: "Cash" for "ATM / Cash", "Health and
    # Fitness" for "Health & Fitness". Accept it only when one category matches.
    loose = k.replace("and", "")
    near = [c for c in CATEGORIES
            if loose and (loose in key(c).replace("and", "") or key(c).replace("and", "") in loose)]
    return near[0] if len(near) == 1 else UNRESOLVED_CATEGORY


UNRESOLVED_BANK = "\x00unresolvedbank"


class Tools:
    """Deterministic tool implementations over one user's transactions."""

    def __init__(self, user_id: int, recurring_fn: Callable[[], dict]):
        self.user_id = user_id
        self.recurring_fn = recurring_fn
        self.rates = currency_utils.get_rates("EUR")
        booked = (db.session.query(Transaction, Account)
                  .join(Account, Transaction.account_id == Account.id)
                  .filter(Account.user_id == user_id, Transaction.status == "booked").all())
        own = db_utils.own_ibans(user_id)
        # Figures leave out transfers between the user's own accounts; the
        # transaction search still finds them, flagged.
        self.all_rows = booked
        self.rows = [(t, a) for t, a in booked if not db_utils.is_internal(t, own)]
        self.own = own

    def _eur(self, t: Transaction, a: Account) -> float:
        return currency_utils.to_eur(float(t.amount or 0), t.currency or a.currency or "EUR", self.rates)

    def _banks(self) -> list[str]:
        return sorted({a.bank for _, a in self.all_rows})

    def _bank(self, name: str | None) -> str | None:
        """The connected bank whose name matches, whatever case or spacing the model used.

        The filter used to compare the raw string, so bank="Commerzbank" matched
        nothing and the tool reported zero spending at a bank holding EUR 19,475.
        """
        if not name:
            return None
        def key(text: str) -> str:
            return re.sub(r"[^a-z0-9]", "", text.lower())
        k = key(name)
        for bank in self._banks():
            if key(bank) == k:
                return bank
        near = [b for b in self._banks() if k and (k in key(b) or key(b) in k)]
        return near[0] if len(near) == 1 else UNRESOLVED_BANK

    def _between(self, d0: date, d1: date, bank: str | None = None):
        for t, a in self.rows:
            if t.booking_date and d0 <= t.booking_date <= d1 and (not bank or a.bank == bank):
                yield t, a

    def coverage(self) -> str:
        dates = [t.booking_date for t, _ in self.rows if t.booking_date]
        return f"{min(dates)} to {max(dates)}" if dates else "no transactions yet"

    def list_accounts(self) -> dict:
        out = []
        accounts = Account.query.filter_by(user_id=self.user_id).order_by(Account.bank).all()
        for acc in accounts:
            native = sum(float(t.amount or 0) for t in acc.transactions)
            dates = [t.booking_date for t in acc.transactions if t.booking_date]
            out.append({"bank": acc.bank, "name": acc.name, "owner": acc.owner_name, "currency": acc.currency,
                        "data": "generated" if (acc.resource_id or "").startswith("SB-") else "live sandbox",
                        "balance_native": round(native, 2),
                        "balance_eur": round(currency_utils.to_eur(native, acc.currency or "EUR", self.rates), 2),
                        "first_transaction": str(min(dates)) if dates else None,
                        "last_transaction": str(max(dates)) if dates else None,
                        "transactions": len(acc.transactions)})
        by_bank: dict[str, float] = defaultdict(float)
        for a in out:
            by_bank[a["bank"]] += a["balance_eur"]
        return {"accounts": out,
                "total_balance_eur": round(sum(a["balance_eur"] for a in out), 2),
                "balance_by_bank_eur": {b: round(v, 2) for b, v in sorted(by_bank.items())},
                "accounts_by_bank": {b: sum(1 for a in out if a["bank"] == b) for b in sorted(by_bank)},
                "note": "balance = sum of stored transactions; totals are computed here, do not add them up yourself"}

    def spending_by_category(self, date_from: str, date_to: str, bank: str | None = None,
                             category: str | None = None) -> dict:
        d0, d1 = _d(date_from, date.today() - timedelta(days=30)), _d(date_to, date.today())
        bank_asked = bank
        bank = self._bank(bank)
        if bank == UNRESOLVED_BANK:
            return {"error": f"no connected bank named {bank_asked!r}", "connected_banks": self._banks()}
        wanted = _category(category)
        if wanted == UNRESOLVED_CATEGORY:
            return {"error": f"there is no category named {category!r}", "valid_categories": list(CATEGORIES)}
        totals, counts = defaultdict(float), defaultdict(int)
        for t, a in self._between(d0, d1, bank):
            if float(t.amount or 0) < 0:
                totals[t.category or "Other"] += -self._eur(t, a)
                counts[t.category or "Other"] += 1
        cats = sorted(({"category": c, "eur": round(v, 2), "transactions": counts[c]} for c, v in totals.items()
                       if not wanted or c == wanted), key=lambda x: -x["eur"])
        if wanted and not cats:      # asked for a category with no spending in the period
            cats = [{"category": wanted, "eur": 0.0, "transactions": 0}]
        total = round(sum(totals.values()), 2)
        if wanted:
            total = round(sum(c["eur"] for c in cats), 2)
        return {"date_from": str(d0), "date_to": str(d1), "bank": bank or "all", "category": wanted or "all",
                "categories": cats, "total_out_eur": total,
                "transactions": sum(c["transactions"] for c in cats) if wanted else sum(counts.values()),
                # One total only. An earlier second field excluded the whole
                # "Transfers / Other" category, which is money paid to people, and
                # the model reported that instead of what the app shows as spending.
                "note": "total_out_eur is all money out in the period; transfers between the user's own "
                        "accounts are already excluded everywhere"}

    def top_merchants(self, date_from: str, date_to: str, category: str | None = None, limit: int = 10) -> dict:
        d0, d1 = _d(date_from, date.today() - timedelta(days=30)), _d(date_to, date.today())
        totals, counts, banks = defaultdict(float), defaultdict(int), defaultdict(set)
        for t, a in self._between(d0, d1):
            if float(t.amount or 0) < 0 and (not category or t.category == category):
                name = t.creditor_name or t.debtor_name or "Unknown"
                totals[name] += -self._eur(t, a)
                counts[name] += 1
                banks[name].add(a.bank)
        limit = max(1, min(int(limit or 10), 20))
        top = sorted(totals.items(), key=lambda kv: -kv[1])[:limit]
        return {"date_from": str(d0), "date_to": str(d1), "category": category or "all",
                "merchants": [{"merchant": m, "eur": round(v, 2), "transactions": counts[m],
                               "banks": sorted(banks[m])} for m, v in top]}

    def monthly_cash_flow(self, months: int = 6) -> dict:
        months = max(1, min(int(months or 6), 13))
        today = date.today()
        keys, y, m = [], today.year, today.month
        for _ in range(months):
            keys.insert(0, (y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        flow = {k: {"income": 0.0, "spending": 0.0} for k in keys}
        for t, a in self.rows:
            if not t.booking_date:
                continue
            k = (t.booking_date.year, t.booking_date.month)
            if k in flow:
                eur = self._eur(t, a)
                flow[k]["income" if eur > 0 else "spending"] += abs(eur)
        return {"months": [{"month": f"{y}-{m:02d}", "income_eur": round(v["income"], 2),
                            "spending_eur": round(v["spending"], 2),
                            "net_eur": round(v["income"] - v["spending"], 2)} for (y, m), v in flow.items()],
                "note": f"current month {today:%Y-%m} is partial"}

    def compare_periods(self, a_from: str, a_to: str, b_from: str, b_to: str) -> dict:
        a = self.spending_by_category(a_from, a_to)
        b = self.spending_by_category(b_from, b_to)
        amap = {c["category"]: c["eur"] for c in a["categories"]}
        bmap = {c["category"]: c["eur"] for c in b["categories"]}
        rows = []
        for cat in sorted(set(amap) | set(bmap)):
            va, vb = amap.get(cat, 0.0), bmap.get(cat, 0.0)
            rows.append({"category": cat, "a_eur": va, "b_eur": vb, "diff_eur": round(vb - va, 2),
                         "pct_change": round((vb - va) / va * 100, 1) if va else None})
        rows.sort(key=lambda r: -abs(r["diff_eur"]))
        return {"period_a": [a["date_from"], a["date_to"]], "period_b": [b["date_from"], b["date_to"]],
                "total_a_eur": a["total_out_eur"], "total_b_eur": b["total_out_eur"],
                "total_diff_eur": round(b["total_out_eur"] - a["total_out_eur"], 2),
                "total_pct_change": round((b["total_out_eur"] - a["total_out_eur"]) / a["total_out_eur"] * 100, 1)
                if a["total_out_eur"] else None,
                "by_category": rows}

    def recurring_payments(self) -> dict:
        return self.recurring_fn()

    def find_transactions(self, date_from: str | None = None, date_to: str | None = None,
                          category: str | None = None, merchant_contains: str | None = None,
                          direction: str = "any", min_amount_eur: float | None = None,
                          limit: int = 15, sort: str = "date", bank: str | None = None) -> dict:
        """Matching transactions, plus the totals and extremes over everything that matched.

        The aggregates are the point. Only `limit` rows are listed, so a model
        that adds up the listed rows answers "how much in total" from a sample,
        and one that reads the first row of a date-sorted list answers "what was
        the largest" with the most recent. Both are computed here instead.
        """
        d0, d1 = _d(date_from, date(2000, 1, 1)), _d(date_to, date.today())
        bank_asked = bank
        bank = self._bank(bank)
        if bank == UNRESOLVED_BANK:
            return {"error": f"no connected bank named {bank_asked!r}", "connected_banks": self._banks()}
        needle = (merchant_contains or "").lower()
        wanted = _category(category)     # the model may write "dining" for "Dining"
        if wanted == UNRESOLVED_CATEGORY:
            return {"error": f"there is no category named {category!r}", "valid_categories": list(CATEGORIES)}
        matched_rows = []
        in_range = [(t, a) for t, a in self.all_rows if t.booking_date and d0 <= t.booking_date <= d1]
        for t, a in in_range:
            eur = self._eur(t, a)
            if bank and a.bank != bank:
                continue
            if wanted and (t.category or "") != wanted:
                continue
            if needle and needle not in (t.creditor_name or t.debtor_name or "").lower():
                continue
            if direction == "out" and eur >= 0 or direction == "in" and eur <= 0:
                continue
            if min_amount_eur is not None and abs(eur) < float(min_amount_eur):
                continue
            matched_rows.append({"date": str(t.booking_date), "bank": a.bank,
                                 "counterparty": t.creditor_name or t.debtor_name, "category": t.category,
                                 "amount_native": float(t.amount or 0), "currency": t.currency,
                                 "amount_eur": round(eur, 2),
                                 "own_account_transfer": db_utils.is_internal(t, self.own)})

        by_date = sorted(matched_rows, key=lambda r: r["date"], reverse=True)
        by_size = sorted(matched_rows, key=lambda r: abs(r["amount_eur"]), reverse=True)
        listed = (by_size if sort == "amount" else by_date)[:max(1, min(int(limit or 15), 25))]

        # Money the customer moved between their own accounts is not spending or
        # income, so it carries its own total rather than silently joining the main one.
        external = [r for r in matched_rows if not r["own_account_transfer"]]
        out = {"transactions": listed, "count_returned": len(listed), "count_matched": len(matched_rows),
               "category": wanted or "all", "sorted_by": "amount" if sort == "amount" else "date",
               "total_eur": round(sum(abs(r["amount_eur"]) for r in external), 2),
               "count_excluding_own_transfers": len(external),
               "note": "total_eur, largest and smallest cover EVERY matched transaction, not only the listed ones. "
                       "total_eur excludes transfers between the customer's own accounts; count_matched includes them."}
        if external:
            big = max(external, key=lambda r: abs(r["amount_eur"]))
            small = min(external, key=lambda r: abs(r["amount_eur"]))
            out["largest"] = {k: big[k] for k in ("date", "bank", "counterparty", "category", "amount_eur")}
            out["smallest"] = {k: small[k] for k in ("date", "bank", "counterparty", "category", "amount_eur")}
        if not matched_rows:
            # "Nothing matched this filter" is not "the customer spent nothing".
            # merchant_contains is a literal substring of the counterparty name, and
            # the names are in the bank's own language, so a search for "ATM" misses
            # "Bargeldauszahlung Muenchen".
            out["matched_nothing"] = True
            out["filters_used"] = {k: v for k, v in (("category", category), ("merchant_contains", merchant_contains),
                                                     ("min_amount_eur", min_amount_eur), ("direction", direction),
                                                     ("bank", bank))
                                   if v not in (None, "any")}
            out["guidance"] = ("No transaction matched these filters, which does not mean the customer spent nothing. "
                               "merchant_contains is a literal substring of the counterparty name in the bank's own "
                               "language. Retry with a category from valid_categories, or with no merchant filter, "
                               "before reporting an absence.")
            out["valid_categories"] = list(CATEGORIES)
        if len(external) != len(matched_rows):
            out["total_eur_including_own_transfers"] = round(sum(abs(r["amount_eur"]) for r in matched_rows), 2)
        return out

    def run(self, name: str, args: dict) -> dict:
        fn = getattr(self, name, None)
        if name not in {t["name"] for t in TOOLS} or fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**(args or {}))
        except TypeError as exc:
            return {"error": f"bad arguments: {exc}"}


def answer(question: str, user_id: int, recurring_fn: Callable[[], dict], session_id: str | None = None) -> dict:
    """Answer one question. Returns answer text, the tool calls made, and bookkeeping."""
    question = (question or "").strip()[:500]
    with observability.request("assistant-question", input={"question": question}, tags=["assistant"],
                               metadata={"user_id": user_id}, user_id=f"user-{user_id}",
                               session_id=session_id) as req:
        if not question:
            return {"answer": "Ask a question about your money.", "tools": [], "refused": False}
        if _REFUSE.search(question):
            observability.score("refused_in_code", 1)
            req.update(output={"answer": REFUSAL, "refused": True})
            return {"answer": REFUSAL, "tools": [], "refused": True, "trace_id": observability.trace_id()}

        tools = Tools(user_id, recurring_fn)
        if _AFFORD.search(question):
            bounded = _affordability_answer(tools)
            observability.score("bounded_in_code", 1)
            req.update(output={"answer": bounded, "bounded": True})
            return {"answer": bounded, "tools": [], "refused": False, "bounded": True,
                    "trace_id": observability.trace_id()}

        if not llm.available():
            return {"answer": "The assistant is unavailable right now (no model key or call budget used).",
                    "tools": [], "refused": False}
        # Generated-account connections are stored as gen_<bank>; the model sees the bank itself.
        banks = sorted({c.bank.removeprefix("gen_") for c in BankConnection.query.filter_by(user_id=user_id, status="active")})
        system, prompt = prompts.compile("fintnet-assistant-system", today=date.today().isoformat(),
                                         banks=", ".join(banks) or "none", coverage=tools.coverage())
        messages: list[dict] = [{"role": "user", "content": question}]
        trail: list[dict] = []
        final_text = ""
        for round_no in range(MAX_ROUNDS):
            try:
                response = llm.create("assistant-turn", system=system, messages=messages, tools=TOOLS,
                                      max_tokens=MAX_TOKENS, metadata={"round": round_no}, prompt=prompt)
            except llm.LLMUnavailable as exc:
                final_text = f"The assistant could not finish: {exc}."
                break
            if response.stop_reason != "tool_use":
                final_text = "".join(b.text for b in response.content if b.type == "text").strip()
                break
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                with observability.step(f"tool:{block.name}", input=block.input) as span:
                    output = tools.run(block.name, dict(block.input or {}))
                    span.update(output=output)
                trail.append({"name": block.name, "input": dict(block.input or {}), "output": output})
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(output, default=str)[:12000],
                                **({"is_error": True} if "error" in output else {})})
            messages.append({"role": "user", "content": results})
        else:
            final_text = "I could not complete this within the tool-call limit. Try a narrower question."

        req.update(output={"answer": final_text, "tool_calls": [t["name"] for t in trail]})
        observability.score("tool_calls", len(trail))
        return {"answer": final_text, "tools": trail, "refused": False, "model": llm.MODEL,
                "context": {"today": date.today().isoformat(), "connected_banks": banks,
                            "data_coverage": tools.coverage()},
                "trace_id": observability.trace_id()}
