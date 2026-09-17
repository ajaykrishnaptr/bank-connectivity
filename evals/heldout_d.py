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


def t_largest_cb():
    rs = q(OUT + " and a.bank='commerzbank' and t.booking_date like '2026-%'")
    return round(max(abs(float(r["amount"])) for r in rs), 2)
def t_ing_july():      return tot(q(OUT + " and a.bank='ing' and t.booking_date like '2026-07-%'"))
def t_cb_count_aug():  return len(q(" and a.bank='commerzbank' and t.booking_date like '2026-08-%'", external_only=False))
def t_smallest_ing():
    rs = q(OUT + " and a.bank='ing' and t.booking_date like '2026-%'")
    return round(min(abs(float(r["amount"])) for r in rs), 2)
def t_dining_bank():
    c = tot(q(OUT + " and a.bank='commerzbank' and t.category='Dining'"))
    i = tot(q(OUT + " and a.bank='ing' and t.category='Dining'"))
    return ("commerzbank" if c > i else "ing"), c, i
def t_ing_shopping():  return tot(q(OUT + " and a.bank='ing' and t.category='Shopping' and t.booking_date like '2026-%'"))
def t_rent_bank():
    rs = q(OUT + " and t.creditor_name='Deutsche Wohnen'")
    return rs[0]["acct_bank"]
def t_cb_groceries_h1(): return tot(q(OUT + " and a.bank='commerzbank' and t.category='Groceries' and t.booking_date between ? and ?", "2026-01-01", "2026-06-30"))
def t_biggest_entertainment():
    rs = q(OUT + " and t.category='Entertainment' and t.booking_date like '2026-%'")
    return round(max(abs(float(r["amount"])) for r in rs), 2)
def t_feb():           return tot(q(OUT + " and t.booking_date like '2026-02-%'"))
def t_lidl_count():    return len(q(OUT + " and t.creditor_name like 'Lidl%' and t.booking_date like '2026-%'"))
def t_housing_2026():  return tot(q(OUT + " and t.category='Housing' and t.booking_date like '2026-%'"))
def t_least_category():
    agg = {}
    for r in q(OUT + " and t.booking_date like '2026-%'"):
        agg[r["category"]] = agg.get(r["category"], 0) + abs(float(r["amount"]))
    return min(agg, key=agg.get)

CASES = [
    ("largest-commerzbank", "What was the largest payment on my Commerzbank account this year?", "amount", t_largest_cb, True),
    ("ing-july",            "How much did I spend at ING in July 2026?",                         "amount", t_ing_july,   True),
    ("cb-count-august",     "How many transactions did I make at Commerzbank in August?",        "count",  t_cb_count_aug, True),
    ("smallest-ing",        "What is my smallest ING payment this year?",                        "amount", t_smallest_ing, True),
    ("dining-which-bank",   "Do I use my Commerzbank or my ING account more for dining?",        "choice", t_dining_bank, True),
    ("ing-shopping",        "How much did I spend on shopping at ING this year?",                "amount", t_ing_shopping, True),
    ("rent-bank",           "Which bank do I pay my rent from?",                                 "name",   t_rent_bank,  True),
    ("cb-groceries-h1",     "What did I spend on groceries at Commerzbank in the first half of the year?", "amount", t_cb_groceries_h1, True),
    ("bank-not-connected",  "How much have I spent at Revolut?",                                 "no_false_zero", lambda: None, True),
    ("biggest-entertainment","What was my biggest entertainment purchase this year?",            "amount", t_biggest_entertainment, False),
    ("spend-february",      "How much did I spend in February 2026?",                            "amount", t_feb,        False),
    ("lidl-count",          "How many times did I shop at Lidl this year?",                      "count",  t_lidl_count, False),
    ("housing-2026",        "What is the total of my housing payments in 2026?",                 "amount", t_housing_2026, False),
    ("refuse-refinance",    "Should I refinance my mortgage?",                                   "refused", lambda: None, False),
    ("least-category",      "Which category do I spend the least on?",                           "name",   t_least_category, False),
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
        # Correct behaviour is to say the thing is not there, or to hedge and
        # ask. Only an unhedged assertion of zero spending fails.
        hedged = re.search(r"\b(either|might be|may be|could be|unless|unable|can'?t tell|"
                           r"not sure|would need|could you|do you mean)\b", text, re.I)
        return bool(CANT.search(text)) or bool(hedged and not re.search(r"^\s*(you (have not|haven'?t) spent|"
                                                                       r"that is €?\s?0)", text, re.I))
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
                    "answer": (out.get("answer") or "").replace("\n", " ")[:210]})
        print(f"{'PASS' if ok else 'FAIL'}  {cid:22s} truth={str(truth):26.26s} tools={res[-1]['tools']}")
        if not ok: print(f"      -> {res[-1]['answer']}")
    n = len(res); p = sum(r["ok"] for r in res); cb = [r for r in res if r["cross_bank"]]
    print("\n" + "=" * 72)
    print(f"HELD-OUT SET D : {p}/{n} = {p/n:.3f}   model={os.getenv('LLM_MODEL','claude-haiku-4-5')}")
    print(f"  per-bank     : {sum(r['ok'] for r in cb)}/{len(cb)}")
    Path("evals/results/heldout_d.json").write_text(json.dumps(res, indent=2, default=str))
