import os
import uuid
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL  = "https://api.nordeaopenbanking.com/personal/v5"
AUTH_URL  = f"{BASE_URL}/authorize"
TOKEN_URL = f"{BASE_URL}/authorize/token"

SCOPES = ["ACCOUNTS_BASIC", "ACCOUNTS_BALANCES", "ACCOUNTS_DETAILS", "ACCOUNTS_TRANSACTIONS"]
MOCK_AUTHORIZER_ID = "70311198"  # sandbox fixed test user

CLIENT_ID     = os.getenv("NORDEA_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("NORDEA_CLIENT_SECRET", "")
COUNTRY       = os.getenv("NORDEA_COUNTRY", "FI")


class NordeaApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _originating_date():
    return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")


def _headers(token=None, mock_authorizer=False):
    h = {
        "X-IBM-Client-Id": CLIENT_ID,
        "X-IBM-Client-Secret": CLIENT_SECRET,
        "Signature": "SKIP_SIGNATURE_VALIDATION_FOR_SANDBOX",
        "X-Nordea-Originating-Date": _originating_date(),
        "X-Request-ID": str(uuid.uuid4()),
        "Accept": "application/json",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    if mock_authorizer:
        h["X-Nordea-Mock-Authorizer-Id"] = MOCK_AUTHORIZER_ID
    return h


def _call(method, url, **kwargs):
    try:
        resp = requests.request(method, url, timeout=15, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:300] if e.response is not None else ""
        raise NordeaApiError(f"{e} — {body}", status_code=e.response.status_code)
    except requests.exceptions.RequestException as e:
        raise NordeaApiError(f"Request failed: {e}")


def initiate_authorize(redirect_uri: str) -> tuple[str, str]:
    """POST to Nordea authorize. Returns (location_url, state).

    In sandbox the mock authorizer auto-approves and Nordea immediately
    redirects to redirect_uri with code. We use allow_redirects=False to
    capture the Location header rather than following it.
    """
    state = str(uuid.uuid4())
    resp = requests.post(AUTH_URL,
        headers={**_headers(mock_authorizer=True), "Content-Type": "application/json"},
        json={
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "country": COUNTRY,
            "response_type": "code",
            "duration": 129600,
            "state": state,
        },
        allow_redirects=False,
        timeout=15,
    )
    if resp.status_code not in (200, 201, 302):
        raise NordeaApiError(f"Authorize failed {resp.status_code}: {resp.text[:300]}", status_code=resp.status_code)
    location = resp.headers.get("Location") or resp.headers.get("location", "")
    if not location:
        raise NordeaApiError(f"No Location header in authorize response: {resp.text[:300]}")
    return location, state


def exchange_code(code: str, redirect_uri: str) -> str:
    """Exchange auth code for access token."""
    resp = requests.post(TOKEN_URL,
        headers={**_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=15
    )
    if not resp.ok:
        raise NordeaApiError(f"Token exchange failed: {resp.text[:300]}", status_code=resp.status_code)
    return resp.json().get("access_token", "")


# ── Data helpers — normalize to Berlin Group field names so templates work ───

def _extract_iban(account: dict) -> str:
    for num in account.get("account_numbers", []):
        if num.get("_type") == "IBAN":
            return num.get("value", "")
    return account.get("_id", "")


def get_accounts(token: str) -> list:
    data = _call("GET", f"{BASE_URL}/accounts", headers=_headers(token))
    raw = data.get("response", {}).get("accounts", [])
    return [{
        "resourceId": acc.get("_id", ""),
        "iban": _extract_iban(acc),
        "currency": acc.get("currency", ""),
        "name": acc.get("account_name", acc.get("product", "")),
        "ownerName": acc.get("name", ""),
    } for acc in raw]


def get_balances(token: str, account_id: str) -> list:
    """Nordea embeds balances in account details — normalize to Berlin Group format."""
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}", headers=_headers(token))
    details = data.get("response", {})
    currency = details.get("currency", "")
    balances = []
    mapping = [
        ("booked_balance", "closingBooked"),
        ("available_balance", "interimAvailable"),
        ("value_dated_balance", "expected"),
        ("credit_limit", "creditLine"),
    ]
    for field, balance_type in mapping:
        if field in details:
            balances.append({
                "balanceType": balance_type,
                "balanceAmount": {"amount": str(details[field]), "currency": currency},
            })
    return balances


def get_transactions(token: str, account_id: str) -> dict:
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}/transactions",
        headers=_headers(token)
    )
    raw = data.get("response", {}).get("transactions", [])
    booked = []
    pending = []
    for t in raw:
        normalized = {
            "bookingDate": t.get("booking_date", t.get("value_date", "")),
            "valueDate": t.get("value_date", ""),
            "transactionAmount": {
                "amount": str(t.get("amount", "")),
                "currency": t.get("currency", ""),
            },
            "creditorName": t.get("counterparty_name", ""),
            "debtorName": t.get("counterparty_name", ""),
            "remittanceInformationUnstructured": t.get("message", t.get("narrative", "")),
        }
        if t.get("booking_date"):
            booked.append(normalized)
        else:
            pending.append(normalized)
    return {"booked": booked, "pending": pending}
