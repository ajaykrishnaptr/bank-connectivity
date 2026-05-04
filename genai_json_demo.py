"""
Demo: structured JSON output with confidence + reasoning.

Calls `categorize_with_confidence()` on a mix of easy and tricky
merchants and prints the full structured response so you can eyeball
how confident the model is and why it picked the category it did.

Unlike the cached `categorize()` path, every merchant here triggers a
fresh LLM call — running this script is the simplest way to A/B
prompt changes against a fixed list.

Usage:
    python3 genai_json_demo.py
"""
from categorize import categorize_with_confidence

# Mix of buckets so we can see how the model handles each:
#   * Clear-cut single-brand merchants — should land "high".
#   * Tricky / generic names — should drop to "medium" or "low".
#   * Historically-bad cases that exposed prompt weaknesses earlier.
TEST_MERCHANTS = [
    # Clear-cut
    "Lidl",
    "Netflix",
    "Uber",

    # Tricky / generic
    "Random Co Ltd",
    "Transfer 4827",

    # Previously confused the model
    "Deutsche Telekom",
    "BSES Delhi",
    "BVG",
    "Cross Sports Club",
]

# Used to colour-code confidence in the terminal. The keys must match
# what the prompt asks the model for — see categorize.py.
_CONFIDENCE_EMOJI = {"high": "🟢", "medium": "🟡", "low": "🔴"}


def main() -> None:
    for m in TEST_MERCHANTS:
        result = categorize_with_confidence(m)
        cat    = result["category"]
        conf   = result["confidence"]
        reason = result["reasoning"]
        emoji  = _CONFIDENCE_EMOJI.get(conf, "⚪")
        print(f"{emoji} {m:<25} -> {cat:<22} ({conf})")
        print(f"     why: {reason}\n")


if __name__ == "__main__":
    main()
