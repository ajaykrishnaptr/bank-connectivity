"""Held-out evaluation of the FintNet money-questions assistant.

Differences from evals/assistant_experiment.py, which is the fitted set:
  * 15 questions written against the product surface, none reusing a shape
    from the 18 tuned cases.
  * Ground truth comes from SQL over the database, NOT from assistant.Tools.
    The existing harness asks the same tools the assistant calls, so a tool
    bug makes the answer and the truth wrong together and the case passes.
  * Run once. The prompt is not touched afterwards.

Usage: .venv/bin/python <this file>
"""
from __future__ import annotations
import os, re, sys, json, sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path.home() / "bank_connectivity"
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import app as appmod          # noqa: E402
import assistant              # noqa: E402
from models import User       # noqa: E402

DB = ROOT / "instance" / "ais.db"
PERSONA = "thomas.mann@example.de"
TODAY = date.today()

# ---------------------------------------------------------------- SQL truth
def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def _own_ibans(c, email):
    return {r["iban"] for r in c.execute(
        "select a.iban from users u join accounts a on a.user_id=u.id where u.email=? and a.iban is not null", (email,))}

# Money between the customer's own accounts is neither income nor spending.
SPEND = """select t.* from users u join accounts a on a.user_id=u.id join transactions t on t.account_id=a.id
           where u.email=? and t.amount < 0"""
INCOME = SPEND.replace("t.amount < 0", "t.amount > 0")

def rows(sql, *params):
    with _conn() as c:
        own = _own_ibans(c, PERSONA)
        out = []
        for r in c.execute(sql, params):
            if r["counterparty_iban"] and r["counterparty_iban"] in own:
                continue
            out.append(dict(r))
        return out

def spend(where="", *p):
    return rows(SPEND + where, PERSONA, *p)

def income(where="", *p):
    return rows(INCOME + where, PERSONA, *p)

def total(rs):
    return round(sum(abs(float(r["amount"])) for r in rs), 2)

def last_month():
    first = TODAY.replace(day=1)
    prev_end = first - timedelta(days=1)
    return prev_end.replace(day=1).isoformat(), prev_end.isoformat()

LM0, LM1 = last_month()

def month_total(cat, y, m):
    return total(spend(" and t.category=? and t.booking_date like ?", cat, f"{y}-{m:02d}-%"))

# ---------------------------------------------------------------- the 15
def t_transport_july():   return month_total("Transport", 2026, 7)
def t_range_mar_apr():    return total(spend(" and t.booking_date between ? and ?", "2026-03-01", "2026-04-30"))
def t_top_category():
    agg = {}
    for r in spend(" and t.booking_date between ? and ?", LM0, LM1):
        agg[r["category"]] = agg.get(r["category"], 0) + abs(float(r["amount"]))
    return max(agg, key=agg.get) if agg else None
def t_largest_6m():
    rs = spend(" and t.booking_date >= ?", (TODAY - timedelta(days=182)).isoformat())
    return round(max(abs(float(r["amount"])) for r in rs), 2) if rs else 0.0
def t_count_ing():        return len(rows(SPEND.replace("t.amount < 0", "1=1") + " and a.bank='ing'", PERSONA))
def t_aug_vs_jul():
    a, j = month_total("Groceries", 2026, 8), month_total("Groceries", 2026, 7)
    return ("August" if a > j else "July"), a, j
def t_avg_income_6m():
    months = {}
    for r in income(" and t.booking_date >= ?", (TODAY - timedelta(days=182)).isoformat()):
        months.setdefault(r["booking_date"][:7], 0)
        months[r["booking_date"][:7]] += float(r["amount"])
    return round(sum(months.values()) / len(months), 2) if months else 0.0
def t_vattenfall():       return total(spend(" and t.creditor_name='Vattenfall' and t.booking_date like '2026-%'"))
def t_groceries_7d():     return total(spend(" and t.category='Groceries' and t.booking_date >= ?",
                                             (TODAY - timedelta(days=7)).isoformat()))
def t_dining_share():
    d = total(spend(" and t.category='Dining' and t.booking_date between ? and ?", LM0, LM1))
    a = total(spend(" and t.booking_date between ? and ?", LM0, LM1))
    return round(100 * d / a, 1) if a else 0.0
def t_entertainment_count():
    return len(spend(" and t.category='Entertainment' and t.booking_date like '2026-%'"))
def t_bank_most_spend():
    agg = {}
    for r in spend(" and t.booking_date between ? and ?", LM0, LM1):
        agg[r["bank"]] = agg.get(r["bank"], 0) + abs(float(r["amount"]))
    return max(agg, key=agg.get) if agg else None
def t_ing_groceries_year():
    return total(spend(" and a.bank='ing' and t.category='Groceries' and t.booking_date like '2026-%'"))
def t_utilities_vs_dining():
    u = total(spend(" and t.category='Utilities' and t.booking_date between ? and ?", LM0, LM1))
    d = total(spend(" and t.category='Dining' and t.booking_date between ? and ?", LM0, LM1))
    return ("utilities" if u > d else "dining"), u, d

CASES = [
    ("transport-july",       "What did I spend on transport in July 2026?",                      "amount",  t_transport_july,  False),
    ("range-mar-apr",        "How much did I spend in total between 1 March and 30 April 2026?", "amount",  t_range_mar_apr,   False),
    ("top-category",         "Which category did I spend the most on last month?",               "name",    t_top_category,    False),
    ("largest-payment-6m",   "What was my single largest payment in the last 6 months?",         "amount",  t_largest_6m,      False),
    ("count-at-ing",         "How many transactions do I have at ING?",                          "count",   t_count_ing,       True),
    ("aug-vs-jul-groceries", "Did I spend more on groceries in August or in July 2026?",         "choice",  t_aug_vs_jul,      False),
    ("avg-income-6m",        "What is my average monthly income over the last 6 months?",        "amount",  t_avg_income_6m,   False),
    ("vattenfall-2026",      "How much have I paid Vattenfall in 2026?",                         "amount",  t_vattenfall,      False),
    ("groceries-7d",         "How much did I spend on groceries in the last 7 days?",            "amount",  t_groceries_7d,    False),
    ("dining-share",         "What share of my spending last month went on dining?",             "amount",  t_dining_share,    False),
    ("entertainment-count",  "How many entertainment purchases did I make this year?",           "count",   t_entertainment_count, False),
    ("bank-most-spend",      "At which of my banks did I spend more last month?",                "name",    t_bank_most_spend, True),
    ("ing-groceries-2026",   "How much have I spent on groceries at ING this year?",             "amount",  t_ing_groceries_year, True),
    ("utilities-vs-dining",  "Did I spend more on utilities or on dining last month?",           "choice",  t_utilities_vs_dining, False),
    ("refuse-mortgage",      "Am I likely to be approved for a mortgage?",                       "refused", lambda: None,      False),
]

# ---------------------------------------------------------------- scoring
def numbers(text):
    out = []
    for raw in re.findall(r"\d[\d.,]*", text or ""):
        s = raw.rstrip(".,")
        if re.search(r"\d,\d{2}$", s) and "." in s[:-3]: s = s.replace(".", "").replace(",", ".")
        elif re.search(r",\d{2}$", s) and s.count(",") == 1: s = s.replace(",", ".")
        else: s = s.replace(",", "")
        try: out.append(float(s))
        except ValueError: pass
    return out

def score(kind, truth, out):
    text = out.get("answer", "") or ""
    if kind == "refused":  return bool(out.get("refused"))
    if out.get("refused"): return False
    if kind == "amount":
        tol = max(1.0, abs(truth) * 0.005)
        return any(abs(n - abs(truth)) <= tol for n in numbers(text))
    if kind == "count":    return truth in [int(n) for n in numbers(text) if float(n).is_integer()]
    if kind == "name":     return bool(truth) and truth.lower() in text.lower()
    if kind == "choice":
        win, a, b = truth
        return win.lower() in text.lower()
    return False

# ---------------------------------------------------------------- run
def main():
    with appmod.app.app_context():
        user = User.query.filter_by(email=PERSONA).first()
        results, calls = [], 0
        for cid, q, kind, truthfn, crossbank in CASES:
            truth = truthfn()
            out = assistant.answer(q, user.id, appmod._recurring_summary)
            ok = score(kind, truth, out)
            tools = [s["name"] for s in (out.get("trail") or [])]
            calls += 1 + len(tools)
            results.append({"id": cid, "ok": ok, "kind": kind, "truth": truth, "cross_bank": crossbank,
                            "tools": tools, "refused": bool(out.get("refused")),
                            "answer": (out.get("answer") or "").replace("\n", " ")[:190]})
            print(f"{'PASS' if ok else 'FAIL'}  {cid:22s} truth={truth!r:34.34s} tools={tools}")
            if not ok: print(f"      -> {results[-1]['answer']}")
        n = len(results); p = sum(r["ok"] for r in results)
        cb = [r for r in results if r["cross_bank"]]; sb = [r for r in results if not r["cross_bank"]]
        print("\n" + "=" * 72)
        print(f"HELD-OUT OVERALL : {p}/{n} = {p/n:.3f}   (model: {os.getenv('LLM_MODEL','claude-haiku-4-5')})")
        print(f"  cross-bank     : {sum(r['ok'] for r in cb)}/{len(cb)}")
        print(f"  single/aggregate: {sum(r['ok'] for r in sb)}/{len(sb)}")
        print(f"  approx Haiku calls: {calls}")
        Path("evals/results/heldout_a.json").write_text(json.dumps(results, indent=2, default=str))

if __name__ == "__main__":
    main()
