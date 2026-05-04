"""
FX helpers for showing balances in EUR.

We hit frankfurter.app (a free wrapper around European Central Bank
reference rates) once an hour and cache the result in memory. If the
network is down we fall back to a small hard-coded table so the
dashboard still renders — the UI marks fallback rates as stale via
`rates_updated_at()`.

Only a handful of European currencies are supported, which is enough
for the banks we currently connect to.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Union

import requests

from logging_config import log

# In-process cache. Module-level dicts persist for the lifetime of the
# Flask worker, so a second request inside the hour reuses the data.
_cache: dict = {"rates": {}, "ts": 0.0, "date": ""}

# Last-resort rates if the API is unreachable. Hand-picked snapshot —
# accurate enough that dashboards don't go wildly wrong, but stale by
# design. `_FALLBACK_DATE` lets `rates_updated_at()` flag this clearly.
_FALLBACK = {"EUR": 1.0, "SEK": 11.18, "NOK": 11.54, "DKK": 7.46, "GBP": 0.86, "CHF": 0.93}
_FALLBACK_DATE = "fallback"

# 1 hour, in seconds. ECB only updates once a day so this is plenty.
_TTL_SECONDS = 3600

# Currencies we know how to render with a flag emoji in the UI. Keep
# in sync with the banks the app connects to.
CURRENCY_FLAGS = {
    "EUR": "🇪🇺", "SEK": "🇸🇪", "NOK": "🇳🇴",
    "DKK": "🇩🇰", "GBP": "🇬🇧", "CHF": "🇨🇭",
}

Number = Union[int, float, Decimal]


def get_rates(base: str = "EUR") -> dict[str, float]:
    """Return a `{currency: rate}` map where `base` = 1.0.

    A rate of 11.18 for SEK against EUR means 1 EUR = 11.18 SEK, so
    `to_eur(amount_sek, "SEK", rates)` divides by the rate.

    Cached for an hour. On any network/HTTP failure we serve the
    `_FALLBACK` table — the caller is not told this happened, but
    `rates_updated_at()` will return "fallback".
    """
    if time.time() - _cache["ts"] < _TTL_SECONDS and _cache["rates"]:
        return _cache["rates"]

    try:
        resp = requests.get(
            f"https://api.frankfurter.app/latest?base={base}",
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        rates = data["rates"]
        rates[base] = 1.0  # API omits the base; add it so callers don't special-case.
        _cache.update(rates=rates, ts=time.time(), date=data.get("date", ""))
        return rates
    except (requests.RequestException, ValueError, KeyError) as exc:
        log.warning("currency.fetch_failed", extra={
            "event": "currency.fetch_failed", "base": base, "error": str(exc)[:200],
        })
        _cache.update(rates=dict(_FALLBACK), ts=time.time(), date=_FALLBACK_DATE)
        return dict(_FALLBACK)


def to_eur(amount: Number, currency: str, rates: dict[str, float]) -> float:
    """Convert `amount` (in `currency`) to EUR using the given rate map.

    If we don't know the currency we return the amount unchanged rather
    than crashing — a slightly wrong dashboard is better than a 500.
    Decimal inputs are accepted (Transaction.amount is Numeric) and
    converted to float for the division.
    """
    amount_f = float(amount)
    if currency == "EUR":
        return amount_f
    rate = rates.get(currency)
    if not rate:
        return amount_f
    return amount_f / rate


def rates_updated_at() -> str:
    """When the cached rates were published.

    Returns:
      * an ECB date string like "2026-05-02" for live data,
      * "fallback" if we're serving the hard-coded snapshot,
      * "" if no fetch has happened yet this process.
    """
    return _cache.get("date") or ""
