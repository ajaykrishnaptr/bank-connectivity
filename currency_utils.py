"""
Live exchange rates from frankfurter.app (European Central Bank data).
Rates are cached in-memory for 1 hour to avoid hammering the API.
"""
import time

import requests

_cache: dict = {"rates": {}, "ts": 0.0, "date": ""}
_FALLBACK = {"EUR": 1.0, "SEK": 11.18, "NOK": 11.54, "DKK": 7.46, "GBP": 0.86, "CHF": 0.93}
_TTL = 3600


def get_rates(base: str = "EUR") -> dict:
    """Return {currency: rate} dict with base currency = 1.0. Cached 1 h."""
    if time.time() - _cache["ts"] < _TTL and _cache["rates"]:
        return _cache["rates"]
    try:
        resp = requests.get(
            f"https://api.frankfurter.app/latest?base={base}",
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        rates = data["rates"]
        rates[base] = 1.0
        _cache.update(rates=rates, ts=time.time(), date=data.get("date", ""))
        return rates
    except Exception:
        return dict(_FALLBACK)


def to_eur(amount: float, currency: str, rates: dict) -> float:
    """Convert amount in `currency` to EUR."""
    if currency == "EUR":
        return amount
    rate = rates.get(currency)
    if not rate:
        return amount
    return amount / rate


def rates_updated_at() -> str:
    return _cache.get("date") or "cached"


CURRENCY_FLAGS = {
    "EUR": "🇪🇺", "SEK": "🇸🇪", "NOK": "🇳🇴",
    "DKK": "🇩🇰", "GBP": "🇬🇧", "CHF": "🇨🇭",
}
