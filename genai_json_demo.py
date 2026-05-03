"""
Demo: structured JSON output with confidence + reasoning.

Calls categorize_with_confidence() on a mix of easy and tricky merchants.
Prints the full structured response so you can see the model's reasoning.
"""
from categorize import categorize_with_confidence

TEST_MERCHANTS = [
    # Clear-cut cases — should be high confidence
    "Lidl",
    "Netflix",
    "Uber",

    # Tricky cases — model should hopefully say medium/low
    "Random Co Ltd",
    "Transfer 4827",

    # The ones that gave us trouble earlier
    "Deutsche Telekom",
    "BSES Delhi",
    "BVG",
    "Cross Sports Club",
]


def main():
    for m in TEST_MERCHANTS:
        result = categorize_with_confidence(m)
        cat = result["category"]
        conf = result["confidence"]
        reason = result["reasoning"]
        # Color-coded confidence in terminal
        emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(conf, "⚪")
        print(f"{emoji} {m:<25} → {cat:<22} ({conf})")
        print(f"     why: {reason}\n")


if __name__ == "__main__":
    main()
