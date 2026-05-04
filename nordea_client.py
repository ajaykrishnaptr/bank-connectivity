"""
Nordea Open Banking client.

Nordea uses its own legacy OBP-style API rather than Berlin Group, so
the field names coming back from /accounts and /transactions look
nothing like Commerzbank or DB. Most of this file is the wire-protocol
plumbing; the bottom third is a normalisation layer that maps Nordea's
shape onto the Berlin-Group shape the rest of the app expects.

Auth model:
  * OAuth2 `authorization_code` grant. Caller asks for a Location URL
    via `initiate_authorize`, sends the user to it, and once the user
    completes SCA Nordea redirects back with a `code` we trade for an
    access token via `exchange_code`.
  * In sandbox the `X-Nordea-Mock-Authorizer-Id` header makes Nordea
    auto-approve the consent without showing an SCA UI — handy for
    automated testing.

Sandbox accepts the literal string `SKIP_SIGNATURE_VALIDATION_FOR_SANDBOX`
in place of a real HTTP signature; production needs a real PKCS#1 sig
in the `Signature` header.
"""
import os
import uuid
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL  = "https://api.nordeaopenbanking.com/personal/v5"
AUTH_URL  = f"{BASE_URL}/authorize"
TOKEN_URL = f"{BASE_URL}/authorize/token"

# Permissions we ask for. Read-only — no payment-initiation.
SCOPES = ["ACCOUNTS_BASIC", "ACCOUNTS_BALANCES", "ACCOUNTS_DETAILS", "ACCOUNTS_TRANSACTIONS"]

# Sandbox user ID that gets auto-approved when the mock-authorizer header is set.
MOCK_AUTHORIZER_ID = "70311198"

CLIENT_ID     = os.getenv("NORDEA_CLIENT_ID",     "")
CLIENT_SECRET = os.getenv("NORDEA_CLIENT_SECRET", "")
COUNTRY       = os.getenv("NORDEA_COUNTRY", "FI")  # FI / SE / NO / DK

# Sandbox bypass token — sandbox accepts this literal string instead
# of a valid HTTP signature. NEVER ship this to production.
_SANDBOX_SKIP_SIG = "SKIP_SIGNATURE_VALIDATION_FOR_SANDBOX"

_DEFAULT_HTTP_TIMEOUT = 15
_AUTH_DURATION_S      = 129600  # 36 hours — sandbox quirk; real values come from the bank


class NordeaApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _originating_date() -> str:
    """RFC 7231 / IMF-fixdate format that Nordea expects in
    `X-Nordea-Originating-Date`."""
    return datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")


def _headers(token: str | None = None, mock_authorizer: bool = False) -> dict:
    """Build the Nordea header set.

    `token`: bearer token for data calls (None for the auth dance).
    `mock_authorizer`: set True only on the initial `/authorize` call to
    use the sandbox auto-approve.
    """
    h = {
        "X-IBM-Client-Id":           CLIENT_ID,
        "X-IBM-Client-Secret":       CLIENT_SECRET,
        "Signature":                 _SANDBOX_SKIP_SIG,
        "X-Nordea-Originating-Date": _originating_date(),
        "X-Request-ID":              str(uuid.uuid4()),
        "Accept":                    "application/json",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    if mock_authorizer:
        h["X-Nordea-Mock-Authorizer-Id"] = MOCK_AUTHORIZER_ID
    return h


def _call(method: str, url: str, **kwargs):
    """HTTP wrapper that turns every failure into NordeaApiError.

    Includes a snippet of the response body in the error message so a
    "400 Bad Request" surfaces *why* it was bad in the flash() shown
    to the user.
    """
    try:
        resp = requests.request(method, url, timeout=_DEFAULT_HTTP_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        body = e.response.text[:300] if e.response is not None else ""
        raise NordeaApiError(f"{e} — {body}", status_code=e.response.status_code)
    except requests.exceptions.RequestException as e:
        raise NordeaApiError(f"Request failed: {e}")


def initiate_authorize(redirect_uri: str) -> tuple[str, str]:
    """Start Nordea's OAuth flow. Returns `(location_url, state)`.

    In sandbox the mock-authorizer auto-approves and Nordea returns a
    302 whose `Location` already contains `?code=…`, so the caller
    can exchange it immediately. We pass `allow_redirects=False` to
    capture that Location header rather than following it.

    `state` is a CSRF token — the caller should stash it in the
    session and verify it matches when the user comes back.
    """
    state = str(uuid.uuid4())
    resp = requests.post(AUTH_URL,
        headers={**_headers(mock_authorizer=True), "Content-Type": "application/json"},
        json={
            "client_id":     CLIENT_ID,
            "redirect_uri":  redirect_uri,
            "scope":         SCOPES,
            "country":       COUNTRY,
            "response_type": "code",
            "duration":      _AUTH_DURATION_S,
            "state":         state,
        },
        allow_redirects=False,
        timeout=_DEFAULT_HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 201, 302):
        raise NordeaApiError(f"Authorize failed {resp.status_code}: {resp.text[:300]}",
                             status_code=resp.status_code)

    location = resp.headers.get("Location") or resp.headers.get("location", "")
    if not location:
        raise NordeaApiError(f"No Location header in authorize response: {resp.text[:300]}")
    return location, state


def exchange_code(code: str, redirect_uri: str) -> str:
    """Trade an authorization code for a Bearer access token. The
    token is what we'll use in `_headers(token=...)` for every
    subsequent data call."""
    resp = requests.post(TOKEN_URL,
        headers={**_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  redirect_uri,
        },
        timeout=_DEFAULT_HTTP_TIMEOUT,
    )
    if not resp.ok:
        raise NordeaApiError(f"Token exchange failed: {resp.text[:300]}",
                             status_code=resp.status_code)
    return resp.json().get("access_token", "")


# ── Data helpers — translate Nordea's OBP shape into the Berlin-Group ────
# shape the rest of the app expects. db_utils.upsert_transactions only
# knows about Berlin-Group field names (`bookingDate`, `transactionAmount`,
# `creditorName`, `remittanceInformationUnstructured`...), so every Nordea
# response is rewritten before it leaves this module.

def _extract_iban(account: dict) -> str:
    """Pull the IBAN out of Nordea's `account_numbers` array, falling
    back to the internal account ID if no IBAN is present."""
    for num in account.get("account_numbers", []):
        if num.get("_type") == "IBAN":
            return num.get("value", "")
    return account.get("_id", "")


def get_accounts(token: str) -> list:
    """Return accounts in the Berlin-Group `{"resourceId", "iban", ...}` shape."""
    data = _call("GET", f"{BASE_URL}/accounts", headers=_headers(token))
    raw = data.get("response", {}).get("accounts", [])
    return [{
        "resourceId": acc.get("_id", ""),
        "iban":       _extract_iban(acc),
        "currency":   acc.get("currency", ""),
        "name":       acc.get("account_name", acc.get("product", "")),
        "ownerName":  acc.get("name", ""),
    } for acc in raw]


def get_balances(token: str, account_id: str) -> list:
    """Balances. Nordea bundles them into the account-detail response,
    so we GET the detail and synthesise a Berlin-Group `balances` array
    by mapping Nordea's named fields onto Berlin-Group `balanceType` values.
    """
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}", headers=_headers(token))
    details  = data.get("response", {})
    currency = details.get("currency", "")

    # Nordea field name -> Berlin Group balanceType.
    mapping = [
        ("booked_balance",      "closingBooked"),
        ("available_balance",   "interimAvailable"),
        ("value_dated_balance", "expected"),
        ("credit_limit",        "creditLine"),
    ]
    balances = []
    for field, balance_type in mapping:
        if field in details:
            balances.append({
                "balanceType":   balance_type,
                "balanceAmount": {"amount": str(details[field]), "currency": currency},
            })
    return balances


def get_transactions(token: str, account_id: str) -> dict:
    """Transactions in the Berlin-Group `{"booked": [...], "pending": [...]}` shape.

    A Nordea transaction is "pending" if it has no `booking_date`. We
    use that as the splitter rather than a separate API call.
    """
    data = _call("GET", f"{BASE_URL}/accounts/{account_id}/transactions",
                 headers=_headers(token))
    raw = data.get("response", {}).get("transactions", [])

    booked, pending = [], []
    for t in raw:
        normalized = {
            "bookingDate":      t.get("booking_date", t.get("value_date", "")),
            "valueDate":        t.get("value_date", ""),
            "transactionAmount": {
                "amount":   str(t.get("amount", "")),
                "currency": t.get("currency", ""),
            },
            # Nordea has one counterparty field for both directions;
            # we surface it as both creditor and debtor and let the
            # categorizer pick whichever is non-empty.
            "creditorName":                       t.get("counterparty_name", ""),
            "debtorName":                         t.get("counterparty_name", ""),
            "remittanceInformationUnstructured":  t.get("message", t.get("narrative", "")),
        }
        if t.get("booking_date"):
            booked.append(normalized)
        else:
            pending.append(normalized)
    return {"booked": booked, "pending": pending}
