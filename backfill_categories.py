"""
Backfill: re-categorize every existing transaction using the AI categorizer.

Pulls unique merchants from the Transaction table, runs each through categorize()
(which uses overrides → cache → LLM), then updates every Transaction row in bulk.

Reports before/after stats so you can see which categories shifted.
"""
import time
from collections import Counter

from app import app
from categorize import categorize
from models import MerchantCategory, Transaction, db


def main():
    with app.app_context():
        # Load every transaction's current category and merchant
        rows = Transaction.query.all()
        before_counts = Counter(t.category for t in rows)

        # Build the unique-merchant set
        merchant_to_txns: dict[str, list] = {}
        for t in rows:
            merchant = (t.creditor_name or t.debtor_name or "").strip()
            if not merchant:
                continue
            merchant_to_txns.setdefault(merchant, []).append(t)

        unique_merchants = sorted(merchant_to_txns.keys())
        print(f"Total transactions:        {len(rows)}")
        print(f"Unique merchants to score: {len(unique_merchants)}")

        cache_before = MerchantCategory.query.count()
        print(f"Cache entries before:      {cache_before}\n")

        t0 = time.time()
        processed = 0
        changed_txns = 0
        merchant_changes = []  # (merchant, before, after)

        for merchant in unique_merchants:
            new_category = categorize(merchant)
            txns = merchant_to_txns[merchant]

            old_category_set = {t.category for t in txns}
            for t in txns:
                if t.category != new_category:
                    t.category = new_category
                    changed_txns += 1

            if old_category_set != {new_category}:
                merchant_changes.append((merchant, sorted(old_category_set), new_category))

            processed += 1
            if processed % 10 == 0:
                elapsed = time.time() - t0
                print(f"  ... {processed}/{len(unique_merchants)} merchants "
                      f"({elapsed:.0f}s elapsed)")

        db.session.commit()

        elapsed = time.time() - t0
        cache_after = MerchantCategory.query.count()
        after_counts = Counter(t.category for t in Transaction.query.all())

        print(f"\n=== DONE in {elapsed:.0f}s ===")
        print(f"Transactions updated:      {changed_txns} / {len(rows)}")
        print(f"Cache grew:                {cache_before} -> {cache_after}")

        # Category-level diff
        all_categories = sorted(set(before_counts) | set(after_counts))
        print(f"\nCategory diff (before -> after):")
        print(f"  {'category':<25} | {'before':>6} | {'after':>6} | delta")
        print(f"  {'-' * 25} | {'-' * 6} | {'-' * 6} | -----")
        for c in all_categories:
            b = before_counts.get(c, 0)
            a = after_counts.get(c, 0)
            delta = a - b
            arrow = "" if delta == 0 else (f"  ↑ +{delta}" if delta > 0 else f"  ↓ {delta}")
            print(f"  {(c or '<None>'):<25} | {b:>6} | {a:>6} | {arrow}")

        # Per-merchant changes (sample)
        if merchant_changes:
            print(f"\nMerchants whose category changed: {len(merchant_changes)}")
            print(f"  (showing first 15)")
            for merchant, before, after in merchant_changes[:15]:
                print(f"  {merchant:<35} : {','.join(before):<22} -> {after}")


if __name__ == "__main__":
    main()
