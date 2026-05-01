import os
import time
import uuid
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL  = "https://api-sandbox.commerzbank.com/accounts-api/29/v1"
TOKEN_URL = "https://api-sandbox.commerzbank.com/auth/realms/sandbox/protocol/openid-connect/token"

SANDBOX_CONSENT = "VALID_RECURRING_PSD2_ALL_ACCOUNTS_WO"
SANDBOX_PSU_ID  = "DE80480800200405423400"

CLIENT_ID     = os.getenv("CB_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CB_CLIENT_SECRET", "")

# Module-level token cache: (access_token, expiry_timestamp)
_token_cache: tuple[str, float] = ("", 0.0)


class CommerzbankApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def get_oauth_token() -> str:
    """Return a valid access token, fetching or refreshing as needed."""
    global _token_cache
    token, expiry = _token_cache
    if token and time.time() < expiry - 30:  # 30s buffer before expiry
        return token

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    expires_in = data.get("expires_in", 900)
    _token_cache = (token, time.time() + expires_in)
    return token


def _headers(token, consent_id=None):
    h = {
        "Authorization": f"Bearer {token}",
        "X-Request-ID": str(uuid.uuid4()),
        "PSU-ID": SANDBOX_PSU_ID,
        "Accept": "application/json",
    }
    if consent_id:
        h["Consent-ID"] = consent_id
    return h


def _call(method, url, **kwargs):
    try:
        resp = requests.request(method, url, timeout=15, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        raise CommerzbankApiError(str(e), status_code=resp.status_code)
    except requests.exceptions.RequestException as e:
        raise CommerzbankApiError(f"Request failed: {e}")


def create_consent(token: str):
    valid_until = (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    body = {
        "access": {"allPsd2": "allAccounts"},
        "recurringIndicator": True,
        "validUntil": valid_until,
        "frequencyPerDay": "4",
        "combinedServiceIndicator": False,
    }
    return _call("POST", f"{BASE_URL}/consents", headers=_headers(token), json=body)


def get_accounts(token: str):
    data = _call("GET", f"{BASE_URL}/accounts",
                 headers=_headers(token, SANDBOX_CONSENT))
    return data.get("accounts", [])


def get_balances(token: str, account_id: str):
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}/balances",
                 headers=_headers(token, SANDBOX_CONSENT))
    return data.get("balances", [])


def get_transactions(token: str, account_id: str):
    date_from = (datetime.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}/transactions",
                 headers=_headers(token, SANDBOX_CONSENT),
                 params={"dateFrom": date_from, "bookingStatus": "both"})
    transactions = data.get("transactions", {})
    return {
        "booked": transactions.get("booked", []),
        "pending": transactions.get("pending", []),
    }
