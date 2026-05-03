import ollama

CATEGORIES = [
    "Groceries", "Food Delivery", "Dining", "Transport", "ATM / Cash",
    "Shopping", "Entertainment", "Utilities", "Healthcare",
    "Health & Fitness", "Housing", "Charity", "Income",
    "Transfers / Other",
]


def categorize_with_ai(merchant: str) -> str:
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
    answer = response["message"]["content"].strip()
    return answer


# Try it on a few merchants
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
    cat = categorize_with_ai(m)
    print(f"{m:<40} → {cat}")
