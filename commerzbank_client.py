import os
import uuid
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL   = "https://api-sandbox.commerzbank.com/accounts-api/29/v1"
TOKEN_URL  = "https://api-sandbox.commerzbank.com/oauth2/token"

# Sandbox fixed values — token endpoint always returns VALID_A_TOKEN
# when called with these fixed parameters + your client_id
SANDBOX_CONSENT       = "VALID_RECURRING_PSD2_ALL_ACCOUNTS_WO"
SANDBOX_PSU_ID        = "DE80480800200405423400"
SANDBOX_CODE          = "VALID_CODE"
SANDBOX_CODE_VERIFIER = "VALID_CODE_VERIFIER"

CLIENT_ID = os.getenv("CB_CLIENT_ID", "")


class CommerzbankApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def get_oauth_token() -> str:
    """Exchange fixed sandbox code for OAuth token. Returns VALID_A_TOKEN."""
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": SANDBOX_CODE,
        "code_verifier": SANDBOX_CODE_VERIFIER,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json().get("access_token", "")


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
