import os
import time
import uuid
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

# Fill these in after registering at developer.db.com → Dashboard → My Apps
BASE_URL  = os.getenv("DB_BASE_URL",  "https://simulator-api.db.com/gw/dbapi/banking/transactions/v2")
TOKEN_URL = os.getenv("DB_TOKEN_URL", "https://simulator-api.db.com/gw/dbapi/oauth2/token/v1")

# From Dashboard → My Test Users
SANDBOX_PSU_ID = os.getenv("DB_SANDBOX_PSU_ID", "")

CLIENT_ID     = os.getenv("DB_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DB_CLIENT_SECRET", "")

_token_cache: tuple[str, float] = ("", 0.0)


class DeutscheBankApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def get_oauth_token() -> str:
    global _token_cache
    token, expiry = _token_cache
    if token and time.time() < expiry - 30:
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
        "Accept": "application/json",
    }
    if SANDBOX_PSU_ID:
        h["PSU-ID"] = SANDBOX_PSU_ID
    if consent_id:
        h["Consent-ID"] = consent_id
    return h


def _call(method, url, **kwargs):
    try:
        resp = requests.request(method, url, timeout=15, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        raise DeutscheBankApiError(str(e), status_code=resp.status_code)
    except requests.exceptions.RequestException as e:
        raise DeutscheBankApiError(f"Request failed: {e}")


def create_consent(token: str, redirect_uri: str) -> dict:
    """Create AIS consent. Returns consentId + _links.scaRedirect for user redirect."""
    valid_until = (datetime.today() + timedelta(days=90)).strftime("%Y-%m-%d")
    body = {
        "access": {"accounts": [], "balances": [], "transactions": []},
        "recurringIndicator": True,
        "validUntil": valid_until,
        "frequencyPerDay": 4,
        "combinedServiceIndicator": False,
    }
    headers = _headers(token)
    headers["TPP-Redirect-URI"] = redirect_uri
    headers["Content-Type"] = "application/json"
    return _call("POST", f"{BASE_URL}/consents", headers=headers, json=body)


def get_consent_status(token: str, consent_id: str) -> str:
    data = _call("GET", f"{BASE_URL}/consents/{consent_id}/status",
                 headers=_headers(token))
    return data.get("consentStatus", "unknown")


def get_accounts(token: str, consent_id: str):
    data = _call("GET", f"{BASE_URL}/accounts",
                 headers=_headers(token, consent_id))
    return data.get("accounts", [])


def get_balances(token: str, consent_id: str, account_id: str):
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}/balances",
                 headers=_headers(token, consent_id))
    return data.get("balances", [])


def get_transactions(token: str, consent_id: str, account_id: str):
    date_from = (datetime.today() - timedelta(days=180)).strftime("%Y-%m-%d")
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}/transactions",
                 headers=_headers(token, consent_id),
                 params={"dateFrom": date_from, "bookingStatus": "both"})
    txns = data.get("transactions", {})
    return {
        "booked": txns.get("booked", []),
        "pending": txns.get("pending", []),
    }
