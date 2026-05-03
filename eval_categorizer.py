"""
Compare rule-based vs AI categorization on real merchants from the seeded data.

Pulls a sample of unique merchants from the Transaction table, runs both the
old keyword rules and the new AI categorizer, and prints where they differ.
The AI answers are cached in the merchant_categories table along the way.
"""
import time

from app import app
from categorize import _categorize_by_rules, categorize
from models import MerchantCategory, Transaction


SAMPLE_SIZE = 25  # number of unique merchants to test


def main():
    with app.app_context():
        rows = Transaction.query.with_entities(
            Transaction.creditor_name, Transaction.debtor_name
        ).limit(3000).all()

        merchants = set()
        for c, d in rows:
            m = (c or d or "").strip()
            if m:
                merchants.add(m)

        sample = sorted(merchants)[:SAMPLE_SIZE]
        print(f"Total unique merchants in DB: {len(merchants)}")
        print(f"Testing {len(sample)} of them...\n")

        cache_before = MerchantCategory.query.count()
        t0 = time.time()

        differences = []
        agreements = 0

        for m in sample:
            rule_answer = _categorize_by_rules(m)
            ai_answer   = categorize(m)  # uses cache + AI under the hood
            if rule_answer == ai_answer:
                agreements += 1
            else:
                differences.append((m, rule_answer, ai_answer))

        elapsed = time.time() - t0
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
                marker = "+" if rule == "Transfers / Other" else "?"
                print(f"  {marker} {m:<36} | {rule:<22} | {ai}")

            print("\nLegend: '+' = AI rescued a merchant the rules couldn't categorize")
            print("        '?' = AI and rules disagree on category — judge for yourself")


if __name__ == "__main__":
    main()
