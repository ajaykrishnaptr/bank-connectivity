"""
Compare rule-based vs AI categorisation on real merchants from the seeded data.

Pulls a sample of unique merchants out of the Transaction table, runs
each through both the deterministic keyword rules AND the AI
categorizer, and prints every disagreement so you can eyeball whether
the AI is improving categorisation or just adding noise.

Side effect: AI answers are written to the merchant_categories cache
along the way, so future categorize() calls for these merchants are
fast.

Usage:
    python3 eval_categorizer.py
"""
import time

from app import app
from categorize import _categorize_by_rules, categorize
from models import MerchantCategory, Transaction


# Number of unique merchants we eval. Pulled deterministically (sorted)
# so re-runs are comparable; bump this to inspect more, but each one
# may cost an LLM call on first sight.
SAMPLE_SIZE = 25


def main() -> None:
    with app.app_context():
        # We only need the merchant fields, not the full row.
        rows = Transaction.query.with_entities(
            Transaction.creditor_name, Transaction.debtor_name
        ).limit(3000).all()

        merchants: set[str] = set()
        for c, d in rows:
            m = (c or d or "").strip()
            if m:
                merchants.add(m)

        sample = sorted(merchants)[:SAMPLE_SIZE]
        print(f"Total unique merchants in DB: {len(merchants)}")
        print(f"Testing {len(sample)} of them...\n")

        cache_before = MerchantCategory.query.count()
        t0 = time.time()

        differences: list[tuple[str, str, str]] = []
        agreements = 0

        for m in sample:
            rule_answer = _categorize_by_rules(m)
            # `categorize` is the public entry point — it goes through
            # overrides + cache + LLM, so this also warms the cache.
            ai_answer = categorize(m)
            if rule_answer == ai_answer:
                agreements += 1
            else:
                differences.append((m, rule_answer, ai_answer))

        elapsed     = time.time() - t0
        cache_after = MerchantCategory.query.count()

        print(f"\n=== RESULTS ({elapsed:.1f}s total) ===")
        print(f"Tested:        {len(sample)} merchants")
        print(f"Cache grew:    {cache_before} -> {cache_after} entries")
        print(f"Agreements:    {agreements}/{len(sample)}")
        print(f"Disagreements: {len(differences)}\n")

        if differences:
            print("Where they differed:")
            print(f"  {'merchant':<38} | {'rules':<22} | AI")
            print(f"  {'-' * 38} | {'-' * 22} | -----------------")
            for m, rule, ai in differences:
                # '+' = the rule layer fell through to "Transfers / Other"
                #       and the AI gave us a more specific answer.
                # '?' = both layers picked something, but disagreed.
                marker = "+" if rule == "Transfers / Other" else "?"
                print(f"  {marker} {m:<36} | {rule:<22} | {ai}")

            print("\nLegend: '+' = AI rescued a merchant the rules couldn't categorize")
            print("        '?' = AI and rules disagree on category — judge for yourself")


if __name__ == "__main__":
    main()
