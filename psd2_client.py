import os
import uuid
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

CERT = (os.getenv("CERT_PATH", "certs/cert.pem"), os.getenv("KEY_PATH", "certs/key.pem"))


class PSD2ApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _headers(consent_id=None):
    h = {
        "X-Request-ID": str(uuid.uuid4()),
        "PSU-IP-Address": "127.0.0.1",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if consent_id:
        h["Consent-ID"] = consent_id
    return h


def _call(method, url, **kwargs):
    try:
        resp = requests.request(method, url, cert=CERT, timeout=15, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.SSLError as e:
        raise PSD2ApiError(f"SSL/certificate error: {e}")
    except requests.exceptions.HTTPError as e:
        raise PSD2ApiError(str(e), status_code=resp.status_code)
    except requests.exceptions.RequestException as e:
        raise PSD2ApiError(f"Request failed: {e}")


def create_consent(base_url, redirect_uri):
    valid_until = (datetime.today() + timedelta(days=365)).strftime("%Y-%m-%d")
    body = {
        "access": {"accounts": [], "balances": [], "transactions": []},
        "recurringIndicator": True,
        "validUntil": valid_until,
        "frequencyPerDay": 4,
        "combinedServiceIndicator": False,
    }
    return _call("POST", f"{base_url}/psd2/v2/consents",
                 headers=_headers(), json=body,
                 params={"TPP-Redirect-URI": redirect_uri})


def get_consent_status(base_url, consent_id):
    return _call("GET", f"{base_url}/psd2/v2/consents/{consent_id}/status",
                 headers=_headers())


def get_accounts(base_url, consent_id):
    data = _call("GET", f"{base_url}/psd2/v2/accounts",
                 headers=_headers(consent_id))
    return data.get("accounts", [])


def get_balances(base_url, consent_id, account_id):
    data = _call("GET", f"{base_url}/psd2/v2/accounts/{account_id}/balances",
                 headers=_headers(consent_id))
    return data.get("balances", [])


def get_transactions(base_url, consent_id, account_id):
    date_from = (datetime.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    data = _call("GET", f"{base_url}/psd2/v2/accounts/{account_id}/transactions",
                 headers=_headers(consent_id),
                 params={"dateFrom": date_from, "bookingStatus": "both"})
    transactions = data.get("transactions", {})
    return {
        "booked": transactions.get("booked", []),
        "pending": transactions.get("pending", []),
    }
