import base64
import hashlib
import os
import time
import uuid
from datetime import datetime, timedelta
from email.utils import formatdate
from urllib.parse import urlencode, urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

BASE_URL  = "https://api.sandbox.ing.com"
TOKEN_URL = f"{BASE_URL}/oauth2/token"

CLIENT_ID        = os.getenv("ING_CLIENT_ID", "")
SIGNING_KEY_PATH = os.getenv("ING_SIGNING_KEY_PATH", "certs/ing_signing.key")
CERT             = (
    os.getenv("ING_TLS_CERT_PATH", "certs/ing_tls.cer"),
    os.getenv("ING_TLS_KEY_PATH",  "certs/ing_tls.key"),
)
COUNTRY_CODE = os.getenv("ING_COUNTRY_CODE", "NL")

_AIS_SCOPES = "payment-accounts:transactions:view payment-accounts:balances:view"

_app_token_cache: tuple[str, float] = ("", 0.0)


class INGApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _digest(body: bytes) -> str:
    return "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()


def _http_signature(method: str, path_with_query: str, date: str, digest: str) -> str:
    signing_string = (
        f"(request-target): {method.lower()} {path_with_query}\n"
        f"date: {date}\n"
        f"digest: {digest}"
    )
    with open(SIGNING_KEY_PATH, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    sig = private_key.sign(signing_string.encode(), padding.PKCS1v15(), hashes.SHA256())
    return (
        f'Signature keyId="{CLIENT_ID}",algorithm="rsa-sha256",'
        f'headers="(request-target) date digest",signature="{base64.b64encode(sig).decode()}"'
    )


def _headers(token: str | None, method: str, path_with_query: str, body: bytes = b"") -> dict:
    date   = formatdate(usegmt=True)
    digest = _digest(body)
    h = {
        "Date":         date,
        "Digest":       digest,
        "Signature":    _http_signature(method, path_with_query, date, digest),
        "X-ING-ReqID": str(uuid.uuid4()),
        "Accept":       "application/json",
    }
    if body:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _call(method: str, url: str, body: bytes = b"",
          token: str | None = None, params: dict | None = None):
    parsed = urlparse(url)
    path   = parsed.path + (f"?{urlencode(params)}" if params else "")
    try:
        resp = requests.request(
            method, url,
            headers=_headers(token, method, path, body),
            data=body or None,
            params=params,
            cert=CERT,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        raise INGApiError(str(e), status_code=resp.status_code)
    except requests.exceptions.RequestException as e:
        raise INGApiError(f"Request failed: {e}")


def get_app_token() -> str:
    global _app_token_cache
    token, expiry = _app_token_cache
    if token and time.time() < expiry - 30:
        return token
    data       = _call("POST", TOKEN_URL, body=f"grant_type=client_credentials&client_id={CLIENT_ID}".encode())
    token      = data["access_token"]
    _app_token_cache = (token, time.time() + data.get("expires_in", 900))
    return token


def get_authorization_url(app_token: str, redirect_uri: str) -> str:
    """Returns the ING-hosted SCA URL to redirect the user to."""
    params = {
        "scope":         _AIS_SCOPES,
        "redirect_uri":  redirect_uri,
        "country_code":  COUNTRY_CODE,
        "response_type": "code",
        "client_id":     CLIENT_ID,
    }
    data = _call("GET", f"{BASE_URL}/oauth2/authorization-server-url",
                 token=app_token, params=params)
    from urllib.parse import quote
    location = data.get("location", "")
    if location:
        sep = "&" if "?" in location else "?"
        if "client_id" not in location:
            location = f"{location}{sep}client_id={CLIENT_ID}"
            sep = "&"
        if "redirect_uri" not in location:
            location = f"{location}{sep}redirect_uri={quote(redirect_uri, safe='')}"
    return location


def exchange_code(code: str, redirect_uri: str) -> str:
    body = f"grant_type=authorization_code&code={code}&redirect_uri={redirect_uri}".encode()
    return _call("POST", TOKEN_URL, body=body)["access_token"]


def get_accounts(customer_token: str) -> list:
    return _call("GET", f"{BASE_URL}/v3/accounts", token=customer_token).get("accounts", [])


def get_balances(customer_token: str, account_id: str) -> list:
    return _call("GET", f"{BASE_URL}/v3/accounts/{account_id}/balances",
                 token=customer_token).get("balances", [])


def get_transactions(customer_token: str, account_id: str) -> dict:
    date_from = (datetime.today() - timedelta(days=180)).strftime("%Y-%m-%d")
    data = _call("GET", f"{BASE_URL}/v3/accounts/{account_id}/transactions",
                 token=customer_token, params={"dateFrom": date_from, "limit": 200})
    return {"booked": data.get("transactions", []), "pending": []}
