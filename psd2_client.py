"""
Berlin-Group NextGenPSD2 client targeting UniCredit's Hydrogen sandbox.

The PSD2 standard defines a uniform shape for AIS (Account
Information Services) endpoints — `/consents`, `/accounts`,
`/balances`, `/transactions` — but every bank takes liberties on top.
This module talks UniCredit's flavour of the protocol; other banks'
quirks live in `commerzbank_client.py`, `nordea_client.py`, etc.

Authentication: mTLS only. The TPP (us) presents a QWAC certificate
on every request via `requests`'s `cert=` parameter. The bank's
sandbox provides test certificates — see `generate_psd2_cert.py` and
the README.

UniCredit-specific quirks (confirmed by diffing a working TPP's call
captured from UniCredit Splunk on 2026-05-11):
  * API prefix is `/hydrogen/v1/...`, NOT the Berlin Group canonical
    `/psd2/v2/...` or `/xs2a/v1/...`. Other prefixes are blocked by
    F5 with HTTP 403 before reaching the app.
  * Required custom headers beyond Berlin Group standard:
      X-Country, X-Legal-Entity (e.g. "UI" = UniCredit Italia),
      X-API-BaseContextURL, PSU-ID-Type, TPP-Redirect-Preferred.
    Without these the app returns FORMAT_ERROR.
  * PSU-IP-Address: must be a routable public IP. 127.0.0.1 / 0.0.0.0
    are rejected. Sandbox accepts "1.1.1.1" as a test value.
  * TPP-Redirect-URI must be HTTPS and is sent as a HEADER, not a
    query parameter.

Identity threading:
  * Every request carries a fresh `X-Request-ID` UUID for tracing.
  * A `Consent-ID` header is added once we have one.
"""
import os
import uuid
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

# QWAC certificate for mTLS. `requests` accepts a (cert_path, key_path)
# tuple and presents it on every request below.
CERT = (os.getenv("CERT_PATH", "certs/cert.pem"),
        os.getenv("KEY_PATH",  "certs/key.pem"))

# Routable public IP; UniCredit sandbox rejects 127.0.0.1. In production
# this should be the actual client IP from the inbound request.
_PSU_IP_ADDRESS = os.getenv("UC_PSU_IP_ADDRESS", "1.1.1.1")

# UniCredit subsidiary the request is routed to. "UI" = UniCredit Italia.
# Other group entities (DE, AT, etc.) would use different codes.
_X_COUNTRY      = os.getenv("UC_X_COUNTRY",      "IT")
_X_LEGAL_ENTITY = os.getenv("UC_X_LEGAL_ENTITY", "UI")

# How long a fresh consent stays valid. The Berlin Group spec caps
# this at 365 days for AIS, which is what we use.
_CONSENT_VALID_DAYS  = 365
_TXN_LOOKBACK_DAYS   = 90    # transactions we ask for on each fetch
_DEFAULT_HTTP_TIMEOUT = 15    # seconds


class PSD2ApiError(Exception):
    """Raised for any failure talking to the PSD2 API.

    `status_code` is set when the failure was an HTTP error response;
    it's None for SSL / connection / parse failures so the caller can
    distinguish "server told us no" from "we couldn't reach it".
    """
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _headers(base_url: str, consent_id: str | None = None) -> dict:
    """Request headers for UniCredit's Hydrogen sandbox.

    Pass `consent_id` for data endpoints; omit for consent creation.
    The `base_url` is echoed back to UniCredit in `X-API-BaseContextURL`
    so the gateway can construct callback URLs that point back at itself.
    """
    h = {
        "X-Request-ID":           str(uuid.uuid4()),
        "PSU-ID-Type":            "ALL",
        "PSU-IP-Address":         _PSU_IP_ADDRESS,
        "X-Country":              _X_COUNTRY,
        "X-Legal-Entity":         _X_LEGAL_ENTITY,
        "X-API-BaseContextURL":   base_url,
        "Content-Type":           "application/json",
        "Accept":                 "application/json",
    }
    if consent_id:
        h["Consent-ID"] = consent_id
    return h


def _call(method: str, url: str, **kwargs):
    """Send an mTLS-authenticated request and return decoded JSON.

    Translates every `requests` failure mode into PSD2ApiError so
    callers only have to catch one exception type.
    """
    try:
        resp = requests.request(method, url, cert=CERT,
                                timeout=_DEFAULT_HTTP_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.SSLError as e:
        raise PSD2ApiError(f"SSL/certificate error: {e}")
    except requests.exceptions.HTTPError as e:
        raise PSD2ApiError(str(e), status_code=resp.status_code)
    except requests.exceptions.RequestException as e:
        raise PSD2ApiError(f"Request failed: {e}")


def create_consent(base_url: str, redirect_uri: str) -> dict:
    """Ask the bank to create a new AIS consent.

    Requests permission to read everything the spec allows —
    accounts, balances, transactions — for a year, with up to four
    refreshes per day. Returns the bank's full response, which
    includes `consentId` and the `_links.scaRedirect.href` the caller
    sends the user to for SCA.
    """
    valid_until = (datetime.today() + timedelta(days=_CONSENT_VALID_DAYS)).strftime("%Y-%m-%d")
    body = {
        "access":                   {"allPsd2": "allAccounts"},
        "recurringIndicator":       True,
        "validUntil":               valid_until,
        "frequencyPerDay":          4,
        "combinedServiceIndicator": False,
    }
    headers = _headers(base_url)
    headers["TPP-Redirect-Preferred"] = "true"
    headers["TPP-Redirect-URI"]       = redirect_uri
    return _call("POST", f"{base_url}/hydrogen/v1/consents",
                 headers=headers, json=body)


def get_consent_status(base_url: str, consent_id: str) -> dict:
    """Return the bank's view of the consent's status.

    Useful values: "received" (created but not yet authenticated),
    "valid" (SCA done, ready to use), "rejected", "expired", "revokedByPsu".
    """
    return _call("GET", f"{base_url}/hydrogen/v1/consents/{consent_id}/status",
                 headers=_headers(base_url))


def get_accounts(base_url: str, consent_id: str) -> list:
    """List of accounts the consent grants access to."""
    data = _call("GET", f"{base_url}/hydrogen/v1/accounts",
                 headers=_headers(base_url, consent_id))
    return data.get("accounts", [])


def get_balances(base_url: str, consent_id: str, account_id: str) -> list:
    """All balance types the bank reports for this account
    (closingBooked, expected, interimAvailable, ...)."""
    data = _call("GET", f"{base_url}/hydrogen/v1/accounts/{account_id}/balances",
                 headers=_headers(base_url, consent_id))
    return data.get("balances", [])


def get_transactions(base_url: str, consent_id: str, account_id: str) -> dict:
    """Last `_TXN_LOOKBACK_DAYS` of transactions, both booked and pending.

    Returns a `{"booked": [...], "pending": [...]}` dict — the same
    shape every bank client returns, so `db_utils.upsert_transactions`
    can handle them uniformly.
    """
    date_from = (datetime.today() - timedelta(days=_TXN_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    data = _call("GET", f"{base_url}/hydrogen/v1/accounts/{account_id}/transactions",
                 headers=_headers(base_url, consent_id),
                 params={"dateFrom": date_from, "bookingStatus": "both"})
    transactions = data.get("transactions", {})
    return {
        "booked":  transactions.get("booked",  []),
        "pending": transactions.get("pending", []),
    }
