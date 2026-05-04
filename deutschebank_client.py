"""
Deutsche Bank PSD2 client (Berlin Group NextGenPSD2).

Auth model is the same shape as Commerzbank:
  * App-level OAuth2 `client_credentials` -> Bearer token (cached).
  * Per-user `consent_id` granted via SCA, returned alongside the
    `_links.scaRedirect.href` we send the user to.

DB-specific notes:
  * The PSU-ID is taken from an env var (`DB_SANDBOX_PSU_ID`) — DB's
    sandbox only sends data when this header matches a registered
    test user. The header is omitted if the env var is unset.
  * Their sandbox returns up to 180 days of transaction history,
    longer than most others, so we ask for that on each fetch.
"""
import os
import time
import uuid
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

# Sandbox URLs by default; override via env once you have production credentials.
# Both come from developer.db.com → Dashboard → My Apps.
BASE_URL  = os.getenv("DB_BASE_URL",  "https://simulator-api.db.com/gw/dbapi/banking/transactions/v2")
TOKEN_URL = os.getenv("DB_TOKEN_URL", "https://simulator-api.db.com/gw/dbapi/oauth2/token/v1")

# Test user reference from Dashboard → My Test Users. Empty string skips the header.
SANDBOX_PSU_ID = os.getenv("DB_SANDBOX_PSU_ID", "")

CLIENT_ID     = os.getenv("DB_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DB_CLIENT_SECRET", "")

_TOKEN_REFRESH_MARGIN_S = 30
_DEFAULT_HTTP_TIMEOUT   = 15
_TXN_LOOKBACK_DAYS      = 180   # DB's sandbox is generous — go further back than the others
_CONSENT_VALID_DAYS     = 90

_token_cache: tuple[str, float] = ("", 0.0)


class DeutscheBankApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def get_oauth_token() -> str:
    """Return a valid Bearer token, fetching or refreshing as needed.
    See the Commerzbank module for an explanation of the cache."""
    global _token_cache
    token, expiry = _token_cache
    if token and time.time() < expiry - _TOKEN_REFRESH_MARGIN_S:
        return token

    resp = requests.post(TOKEN_URL,
        data={
            "grant_type":    "client_credentials",
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=_DEFAULT_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    token      = data["access_token"]
    expires_in = data.get("expires_in", 900)
    _token_cache = (token, time.time() + expires_in)
    return token


def _headers(token: str, consent_id: str | None = None) -> dict:
    """Standard request headers. PSU-ID is conditional: only set if
    `DB_SANDBOX_PSU_ID` is configured."""
    h = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID":  str(uuid.uuid4()),
        "Accept":        "application/json",
    }
    if SANDBOX_PSU_ID:
        h["PSU-ID"] = SANDBOX_PSU_ID
    if consent_id:
        h["Consent-ID"] = consent_id
    return h


def _call(method: str, url: str, **kwargs):
    """Wrap a `requests` call so every error type becomes a single
    DeutscheBankApiError the caller can catch."""
    try:
        resp = requests.request(method, url, timeout=_DEFAULT_HTTP_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        raise DeutscheBankApiError(str(e), status_code=resp.status_code)
    except requests.exceptions.RequestException as e:
        raise DeutscheBankApiError(f"Request failed: {e}")


def create_consent(token: str, redirect_uri: str) -> dict:
    """Create an AIS consent. Returns the full bank response — caller
    pulls out `consentId` and the `_links.scaRedirect` URL.
    """
    valid_until = (datetime.today() + timedelta(days=_CONSENT_VALID_DAYS)).strftime("%Y-%m-%d")
    body = {
        "access":                   {"accounts": [], "balances": [], "transactions": []},
        "recurringIndicator":       True,
        "validUntil":               valid_until,
        "frequencyPerDay":          4,
        "combinedServiceIndicator": False,
    }
    headers = _headers(token)
    headers["TPP-Redirect-URI"] = redirect_uri
    headers["Content-Type"]     = "application/json"
    return _call("POST", f"{BASE_URL}/consents", headers=headers, json=body)


def get_consent_status(token: str, consent_id: str) -> str:
    """Return just the `consentStatus` string."""
    data = _call("GET", f"{BASE_URL}/consents/{consent_id}/status",
                 headers=_headers(token))
    return data.get("consentStatus", "unknown")


def get_accounts(token: str, consent_id: str) -> list:
    data = _call("GET", f"{BASE_URL}/accounts",
                 headers=_headers(token, consent_id))
    return data.get("accounts", [])


def get_balances(token: str, consent_id: str, account_id: str) -> list:
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}/balances",
                 headers=_headers(token, consent_id))
    return data.get("balances", [])


def get_transactions(token: str, consent_id: str, account_id: str) -> dict:
    """Last `_TXN_LOOKBACK_DAYS` of transactions, both booked and pending."""
    date_from = (datetime.today() - timedelta(days=_TXN_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}/transactions",
                 headers=_headers(token, consent_id),
                 params={"dateFrom": date_from, "bookingStatus": "both"})
    txns = data.get("transactions", {})
    return {
        "booked":  txns.get("booked",  []),
        "pending": txns.get("pending", []),
    }
