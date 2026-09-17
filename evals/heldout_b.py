"""Held-out set B: 15 fresh questions, run once after the find_transactions fix.

No shape is reused from the 18 fitted cases or from held-out set A.
Ground truth is SQL over the database, never assistant.Tools.

Own-account transfers follow the product's own convention:
  counts include them, spending totals and extremes exclude them.
"""
from __future__ import annotations
import os, re, sys, json, sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path.home() / "bank_connectivity"
sys.path.insert(0, str(ROOT)); os.chdir(ROOT)
from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
import app as appmod          # noqa: E402
import assistant              # noqa: E402
from models import User       # noqa: E402

DB = ROOT / "instance" / "ais.db"
PERSONA = "thomas.mann@example.de"
TODAY = date.today()

def _c():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

with _c() as _k:
    OWN = {r["iban"] for r in _k.execute(
        "select a.iban from users u join accounts a on a.user_id=u.id where u.email=? and a.iban is not null", (PERSONA,))}

BASE = """select t.*, a.bank as acct_bank from users u join accounts a on a.user_id=u.id
          join transactions t on t.account_id=a.id where u.email=?"""

def q(where="", *p, external_only=True):
    with _c() as c:
        out = []
        for r in c.execute(BASE + where, (PERSONA, *p)):
            d = dict(r)
            internal = bool(d["counterparty_iban"] and d["counterparty_iban"] in OWN)
            if external_only and internal:
                continue
            out.append(d)
        return out

def tot(rs):  return round(sum(abs(float(r["amount"])) for r in rs), 2)
OUT = " and t.amount < 0"
IN_ = " and t.amount > 0"

def t_smallest_3m():
    rs = q(OUT + " and t.booking_date >= ?", (TODAY - timedelta(days=91)).isoformat())
    return round(min(abs(float(r["amount"])) for r in rs), 2)
def t_shopping_h1():   return tot(q(OUT + " and t.category='Shopping' and t.booking_date between ? and ?", "2026-01-01", "2026-06-30"))
def t_rewe_count():
    # PAYPAL *REWE and ZETTLE_*REWE are REWE purchases collected over another
    # rail, so "how many times did I shop at REWE" counts them too.
    return len(q(OUT + " and lower(t.creditor_name) like '%rewe%' and t.booking_date like '2026-%'"))
def t_biggest_housing():
    rs = q(OUT + " and t.category='Housing'")
    return round(max(abs(float(r["amount"])) for r in rs), 2)
def t_income_august():  return tot(q(IN_ + " and t.booking_date like '2026-08-%'"))
def t_atm_year():       return tot(q(OUT + " and t.category='ATM / Cash' and t.booking_date like '2026-%'"))
def t_cb_count():       return len(q(" and a.bank='commerzbank'", external_only=False))
def t_over_200():       return len(q(OUT + " and -t.amount > 200 and t.booking_date >= ?", (TODAY - timedelta(days=182)).isoformat()))
def t_delivery_vs_dining():
    fd = tot(q(OUT + " and t.category='Food Delivery' and t.booking_date like '2026-%'"))
    di = tot(q(OUT + " and t.category='Dining' and t.booking_date like '2026-%'"))
    return ("dining" if di > fd else "food delivery"), fd, di
def t_last_utilities():
    rs = sorted(q(OUT + " and t.category='Utilities'"), key=lambda r: r["booking_date"])
    return round(abs(float(rs[-1]["amount"])), 2)
def t_spend_may():      return tot(q(OUT + " and t.booking_date like '2026-05-%'"))
def t_top_merchant_year():
    agg = {}
    for r in q(OUT + " and t.booking_date like '2026-%'"):
        k = r["creditor_name"] or "?"
        agg[k] = agg.get(k, 0) + abs(float(r["amount"]))
    return max(agg, key=agg.get)
def t_ing_vs_cb():
    i = len(q(" and a.bank='ing'", external_only=False)); c = len(q(" and a.bank='commerzbank'", external_only=False))
    return ("ing" if i > c else "commerzbank"), i, c
def t_health_12m():     return tot(q(OUT + " and t.category='Health & Fitness' and t.booking_date >= ?",
                                     (TODAY - timedelta(days=365)).isoformat()))

CASES = [
    ("smallest-payment-3m",  "What was my smallest payment in the last 3 months?",                  "amount", t_smallest_3m,  False),
    ("shopping-h1",          "How much did I spend on shopping in the first half of 2026?",         "amount", t_shopping_h1,  False),
    ("rewe-count-2026",      "How many times did I shop at REWE this year?",                        "count",  t_rewe_count,   False),
    ("biggest-housing",      "What is the largest housing payment I have made?",                    "amount", t_biggest_housing, False),
    ("income-august",        "How much money came in during August 2026?",                          "amount", t_income_august, False),
    ("atm-cash-2026",        "How much cash have I taken out of ATMs this year?",                   "amount", t_atm_year,     False),
    ("commerzbank-tx-count", "How many transactions are on my Commerzbank accounts?",               "count",  t_cb_count,     True),
    ("payments-over-200",    "How many payments over 200 euros did I make in the last 6 months?",   "count",  t_over_200,     False),
    ("delivery-vs-dining",   "Have I spent more on food delivery or on dining this year?",          "choice", t_delivery_vs_dining, False),
    ("last-utilities",       "How much was my most recent utilities bill?",                         "amount", t_last_utilities, False),
    ("spend-may-2026",       "What was my total spending in May 2026?",                             "amount", t_spend_may,    False),
    ("top-merchant-2026",    "Who is my biggest merchant this year by amount spent?",               "name",   t_top_merchant_year, False),
    ("ing-vs-cb-tx",         "Do I have more transactions at ING or at Commerzbank?",               "choice", t_ing_vs_cb,    True),
    ("refuse-pension",       "How should I invest my pension?",                                     "refused", lambda: None,  False),
    ("health-fitness-12m",   "How much have I spent on health and fitness in the last 12 months?",  "amount", t_health_12m,   False),
]

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
    if kind == "choice":   return truth[0].lower() in text.lower()
    return False

def main():
    with appmod.app.app_context():
        user = User.query.filter_by(email=PERSONA).first()
        res = []
        for cid, question, kind, truthfn, cross in CASES:
            truth = truthfn()
            out = assistant.answer(question, user.id, appmod._recurring_summary)
            ok = score(kind, truth, out)
            tools = [t["name"] for t in (out.get("tools") or [])]
            res.append({"id": cid, "ok": ok, "truth": truth, "cross_bank": cross, "tools": tools,
                        "answer": (out.get("answer") or "").replace("\n", " ")[:200]})
            print(f"{'PASS' if ok else 'FAIL'}  {cid:22s} truth={str(truth):30.30s} tools={tools}")
            if not ok: print(f"      -> {res[-1]['answer']}")
        n = len(res); p = sum(r["ok"] for r in res)
        cb = [r for r in res if r["cross_bank"]]
        print("\n" + "=" * 72)
        print(f"HELD-OUT SET B : {p}/{n} = {p/n:.3f}   model={os.getenv('LLM_MODEL','claude-haiku-4-5')}")
        print(f"  cross-bank   : {sum(r['ok'] for r in cb)}/{len(cb)}")
        print(f"  tool used    : {sum(1 for r in res if r['tools'])}/{n}")
        Path("evals/results/heldout_b.json").write_text(json.dumps(res, indent=2, default=str))

main()
