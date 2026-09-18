"""
Commerzbank PSD2 client.

Auth model:
  * App-level: OAuth2 `client_credentials` grant gives us a Bearer
    access token (cached for `expires_in` seconds with a 30-second
    safety margin).
  * Per-user: an explicit `consent_id` granted via SCA. The Commerzbank
    sandbox provides one canonical consent ID (`SANDBOX_CONSENT`) that
    behaves like a real consent for testing.

The data endpoints follow the Berlin Group shape, so the JSON returned
here has the same `transactions: {booked, pending}` skeleton as
`psd2_client`. Only auth differs.
"""
import os
import time as _time
from urllib.parse import urlparse as _urlparse
import time
import uuid
from datetime import datetime, timedelta

import requests

from fintnet.telemetry.logging_config import log
from dotenv import load_dotenv

load_dotenv()

# Sandbox URLs — production overrides via env vars in real deployments.
BASE_URL  = "https://api-sandbox.commerzbank.com/accounts-api/29/v1"
TOKEN_URL = "https://api-sandbox.commerzbank.com/auth/realms/sandbox/protocol/openid-connect/token"

# Sandbox-specific test data. PSU-ID is the customer reference the
# sandbox expects on every data request; SANDBOX_CONSENT is a
# pre-authorised consent ID the sandbox accepts for AIS calls.
SANDBOX_PSU_ID  = "DE80480800200405423400"
SANDBOX_CONSENT = "VALID_RECURRING_PSD2_ALL_ACCOUNTS_WO"

CLIENT_ID     = os.getenv("CB_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CB_CLIENT_SECRET", "")

# Refresh the token a bit before it actually expires so an in-flight
# request never gets stuck holding a token that died mid-call.
_TOKEN_REFRESH_MARGIN_S = 30
_DEFAULT_HTTP_TIMEOUT   = 15
_TXN_LOOKBACK_DAYS      = 90
_CONSENT_VALID_DAYS     = 90

# (access_token, expiry_epoch_seconds). Process-wide cache.
_token_cache: tuple[str, float] = ("", 0.0)


class CommerzbankApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def get_oauth_token() -> str:
    """Return a valid Bearer token, fetching or refreshing as needed.

    The cache is module-level so every request handler in the same
    process shares one token. That's fine for a single-worker dev
    server; with multiple workers each gets its own copy and the bank
    sees a few extra token requests — still well within rate limits.
    """
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
    expires_in = data.get("expires_in", 900)  # 15 min default if bank omits it
    _token_cache = (token, time.time() + expires_in)
    return token


def _headers(token: str, consent_id: str | None = None) -> dict:
    """Standard request headers. PSU-ID is required by the sandbox on
    every call (real users provide it from their session)."""
    h = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID":  str(uuid.uuid4()),
        "PSU-ID":        SANDBOX_PSU_ID,
        "Accept":        "application/json",
    }
    if consent_id:
        h["Consent-ID"] = consent_id
    return h


def _log_call(bank: str, method: str, url: str, status, started: float, error: str | None = None) -> None:
    """One event per bank API call: what was called, where, with what result.

    These are the live PSD2 sandbox calls, so the operations event log can show
    the connection is real (`event=bank.api.call | stats count by bank, host`).
    """
    parsed = _urlparse(url)
    log.info("bank.api.call", extra={
        "event": "bank.api.call", "bank": bank, "method": method, "host": parsed.netloc,
        "path": parsed.path, "status_code": status, "data": "live sandbox",
        "latency_ms": int((_time.time() - started) * 1000), "error": error,
    })


def _call(method: str, url: str, **kwargs):
    """Send a request and return decoded JSON. Wraps every `requests`
    failure in CommerzbankApiError so callers catch a single type."""
    started = _time.time()
    try:
        resp = requests.request(method, url, timeout=_DEFAULT_HTTP_TIMEOUT, **kwargs)
        _log_call("commerzbank", method, url, resp.status_code, started)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        raise CommerzbankApiError(str(e), status_code=resp.status_code)
    except requests.exceptions.RequestException as e:
        _log_call("commerzbank", method, url, None, started, error=str(e)[:200])
        raise CommerzbankApiError(f"Request failed: {e}")


def create_consent(token: str, redirect_uri: str) -> dict:
    """Create an AIS consent. Returns the bank's full response,
    including `consentId` and the SCA redirect link to send the user
    to next.
    """
    valid_until = (datetime.today() + timedelta(days=_CONSENT_VALID_DAYS)).strftime("%Y-%m-%d")
    body = {
        "access":                   {"allPsd2": "allAccounts"},
        "recurringIndicator":       True,
        "validUntil":               valid_until,
        "frequencyPerDay":          "4",
        "combinedServiceIndicator": False,
    }
    headers = _headers(token)
    headers["TPP-Redirect-URI"] = redirect_uri
    headers["Content-Type"]     = "application/json"
    return _call("POST", f"{BASE_URL}/consents", headers=headers, json=body)


def get_consent_status(token: str, consent_id: str) -> str:
    """Return just the `consentStatus` string. Useful values: "valid",
    "received", "rejected", "expired", "revokedByPsu"."""
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
    """Last `_TXN_LOOKBACK_DAYS` of transactions, both booked and pending.
    Returns the standard `{"booked": [...], "pending": [...]}` shape."""
    date_from = (datetime.today() - timedelta(days=_TXN_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}/transactions",
                 headers=_headers(token, consent_id),
                 params={"dateFrom": date_from, "bookingStatus": "both"})
    transactions = data.get("transactions", {})
    return {
        "booked":  transactions.get("booked",  []),
        "pending": transactions.get("pending", []),
    }
