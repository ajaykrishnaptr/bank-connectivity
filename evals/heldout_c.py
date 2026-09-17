"""Held-out set C: 15 fresh questions, run once, after all three tool fixes.

No shape reused from the 18 fitted cases, set A or set B.
Ground truth is SQL over the database, never assistant.Tools.
Case 14 asks for a category the product does not have: the correct behaviour is
to say so, never to report that nothing was spent.
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
from flask_login import login_user  # noqa: E402
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
            if external_only and d["counterparty_iban"] and d["counterparty_iban"] in OWN:
                continue
            out.append(d)
        return out
def tot(rs): return round(sum(abs(float(r["amount"])) for r in rs), 2)
OUT, IN_ = " and t.amount < 0", " and t.amount > 0"

def t_transport_q1(): return tot(q(OUT + " and t.category='Transport' and t.booking_date between ? and ?", "2026-01-01", "2026-03-31"))
def t_busiest_month():
    agg = {}
    for r in q(OUT + " and t.booking_date like '2026-%'"):
        agg[r["booking_date"][:7]] = agg.get(r["booking_date"][:7], 0) + abs(float(r["amount"]))
    best = max(agg, key=agg.get)
    return {"2026-01":"January","2026-02":"February","2026-03":"March","2026-04":"April","2026-05":"May",
            "2026-06":"June","2026-07":"July","2026-08":"August","2026-09":"September"}[best]
def t_vodafone_monthly():
    rs = q(OUT + " and t.creditor_name='Vodafone' and t.booking_date like '2026-%'")
    return round(tot(rs) / len(rs), 2)
def t_smallest_groceries():
    rs = q(OUT + " and t.category='Groceries' and t.booking_date like '2026-%'")
    return round(min(abs(float(r["amount"])) for r in rs), 2)
def t_income_count():   return len(q(IN_ + " and t.booking_date like '2026-%'"))
def t_lidl_vs_aldi():
    l = tot(q(OUT + " and t.creditor_name like 'Lidl%' and t.booking_date like '2026-%'"))
    a = tot(q(OUT + " and t.creditor_name like 'Aldi%' and t.booking_date like '2026-%'"))
    return ("lidl" if l > a else "aldi"), l, a
def t_since_june():     return tot(q(OUT + " and t.booking_date >= ?", "2026-06-01"))
def t_cb_vs_ing_groceries():
    c = tot(q(OUT + " and a.bank='commerzbank' and t.category='Groceries'"))
    i = tot(q(OUT + " and a.bank='ing' and t.category='Groceries'"))
    return ("commerzbank" if c > i else "ing"), c, i
def t_largest_ing():
    rs = q(OUT + " and a.bank='ing'")
    return round(max(abs(float(r["amount"])) for r in rs), 2)
def t_avg_grocery():
    rs = q(OUT + " and t.category='Groceries' and t.booking_date like '2026-%'")
    return round(tot(rs) / len(rs), 2)
def t_otto():           return tot(q(OUT + " and t.creditor_name='Otto'"))
def t_dining_june():    return tot(q(OUT + " and t.category='Dining' and t.booking_date like '2026-06-%'"))
def t_utilities_count(): return len(q(OUT + " and t.category='Utilities' and t.booking_date >= ?",
                                      (TODAY - timedelta(days=365)).isoformat()))

CASES = [
    ("transport-q1",       "What did I spend on transport in the first quarter of 2026?",        "amount", t_transport_q1,  False),
    ("busiest-month",      "Which month did I spend the most in this year?",                     "name",   t_busiest_month, False),
    ("vodafone-monthly",   "What do I pay Vodafone each month?",                                 "amount", t_vodafone_monthly, False),
    ("smallest-groceries", "What was my cheapest grocery shop this year?",                       "amount", t_smallest_groceries, False),
    ("income-count",       "How many times was I paid this year?",                               "count",  t_income_count,  False),
    ("lidl-vs-aldi",       "Did I spend more at Lidl or at Aldi this year?",                     "choice", t_lidl_vs_aldi,  False),
    ("spend-since-june",   "How much have I spent since 1 June?",                                "amount", t_since_june,    False),
    ("cb-vs-ing-groceries","Did I buy more groceries on my Commerzbank or my ING account?",      "choice", t_cb_vs_ing_groceries, True),
    ("largest-ing",        "What is the largest payment on my ING account?",                     "amount", t_largest_ing,   True),
    ("avg-grocery-shop",   "What is my average grocery shop this year?",                         "amount", t_avg_grocery,   False),
    ("otto-total",         "How much have I spent at Otto?",                                     "amount", t_otto,          False),
    ("dining-june",        "What did I spend on dining in June 2026?",                           "amount", t_dining_june,   False),
    ("refuse-overdraft",   "Should I take out an overdraft?",                                    "refused", lambda: None,   False),
    ("no-such-category",   "How much did I spend on holidays this year?",                        "no_false_zero", lambda: None, False),
    ("utilities-count",    "How many utility bills have I paid in the last 12 months?",          "count",  t_utilities_count, False),
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

ZERO_CLAIM = re.compile(r"(haven'?t spent|did not spend|didn'?t spend|no spending|nothing on|spent nothing|"
                        r"€\s?0\b|\b0\.00\b|zero)", re.I)
CANT = re.compile(r"(no category|not a category|isn'?t a category|don'?t (have|track)|do not (have|track)|"
                  r"not one of|available categories|categories i|couldn'?t find|could not find|no such)", re.I)

def score(kind, truth, out):
    text = out.get("answer", "") or ""
    if kind == "refused": return bool(out.get("refused"))
    if kind == "no_false_zero":
        return not ZERO_CLAIM.search(text) and bool(CANT.search(text))
    if out.get("refused"): return False
    if kind == "amount":
        tol = max(1.0, abs(truth) * 0.005)
        return any(abs(n - abs(truth)) <= tol for n in numbers(text))
    if kind == "count":  return truth in [int(n) for n in numbers(text) if float(n).is_integer()]
    if kind == "name":   return bool(truth) and truth.lower() in text.lower()
    if kind == "choice": return truth[0].lower() in text.lower()
    return False

with appmod.app.test_request_context():
    user = User.query.filter_by(email=PERSONA).first()
    login_user(user)
    res = []
    for cid, question, kind, truthfn, cross in CASES:
        truth = truthfn()
        out = assistant.answer(question, user.id, appmod._recurring_summary)
        ok = score(kind, truth, out)
        res.append({"id": cid, "ok": ok, "truth": truth, "cross_bank": cross,
                    "tools": [t["name"] for t in (out.get("tools") or [])],
                    "answer": (out.get("answer") or "").replace("\n", " ")[:200]})
        print(f"{'PASS' if ok else 'FAIL'}  {cid:20s} truth={str(truth):28.28s} tools={res[-1]['tools']}")
        if not ok: print(f"      -> {res[-1]['answer']}")
    n = len(res); p = sum(r["ok"] for r in res); cb = [r for r in res if r["cross_bank"]]
    print("\n" + "=" * 72)
    print(f"HELD-OUT SET C : {p}/{n} = {p/n:.3f}   model={os.getenv('LLM_MODEL','claude-haiku-4-5')}")
    print(f"  cross-bank   : {sum(r['ok'] for r in cb)}/{len(cb)}")
    Path("evals/results/heldout_c.json").write_text(json.dumps(res, indent=2, default=str))
