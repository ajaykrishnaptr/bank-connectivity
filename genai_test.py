"""
Smallest possible smoke test of the local Ollama categorizer.

Usage:
    python3 genai_test.py

Prints one line per merchant with the model's chosen category. Useful
when hacking on the prompt — gives you a quick "is the model still
basically working?" signal without booting Flask. For the real
categorizer (with caching, overrides, and DB persistence) see
`categorize.py`.
"""
import ollama

# Mirror of `categorize.CATEGORIES`. Duplicated here so this script can
# run without importing the Flask app — handy when sketching prompt changes.
CATEGORIES = [
    "Groceries", "Food Delivery", "Dining", "Transport", "ATM / Cash",
    "Shopping", "Entertainment", "Utilities", "Healthcare",
    "Health & Fitness", "Housing", "Charity", "Income",
    "Transfers / Other",
]


def categorize_with_ai(merchant: str) -> str:
    """Send one merchant to the model and return its raw answer.

    Deliberately minimal — no fence-stripping, no validation against
    CATEGORIES, no fallback. Use this script to inspect the model's
    natural output; use `categorize.categorize()` in real code.
    """
    prompt = f"""You are a transaction categorization assistant.

Pick exactly ONE category from this list that best matches the merchant.
Reply with ONLY the category name, nothing else.

Categories: {", ".join(CATEGORIES)}

Merchant: {merchant}
Category:"""

    response = ollama.chat(
        model="qwen2.5:3b",
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0},
    )
    return response["message"]["content"].strip()


def main() -> None:
    test_merchants = [
        "Lidl",
        "Netflix",
        "Lieferando",
        "Spotify Family",
        "REWE Markt 2451 Düsseldorf-Bilk",
        "Deutsche Bahn AG",
        "DocMorris Apotheke",
        "Salary Siemens AG",
    ]
    for m in test_merchants:
        print(f"{m:<40} -> {categorize_with_ai(m)}")


if __name__ == "__main__":
    main()
