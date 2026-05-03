import base64
import hashlib
import os
import time
import uuid
from datetime import datetime, timedelta
from email.utils import formatdate
from urllib.parse import urlencode, urlparse, quote

import requests
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

BASE_URL  = "https://api.sandbox.ing.com"
AUTH_URL  = "https://myaccount.sandbox.ing.com/authorize/v2"
TOKEN_URL = f"{BASE_URL}/oauth2/token"

CLIENT_ID        = os.getenv("ING_CLIENT_ID", "")
SIGNING_KEY_PATH = os.getenv("ING_SIGNING_KEY_PATH", "certs/ing_signing.key")
SIGNING_CRT_PATH = os.getenv("ING_SIGNING_CERT_PATH", "certs/ing_signing.cer")
CERT             = (
    os.getenv("ING_TLS_CERT_PATH", "certs/ing_tls.cer"),
    os.getenv("ING_TLS_KEY_PATH",  "certs/ing_tls.key"),
)
COUNTRY_CODE = os.getenv("ING_COUNTRY_CODE", "NL")

# Sandbox example client uses https://www.example.com/ as its registered redirect URI
SANDBOX_REDIRECT_URI = "https://www.example.com/"

_AIS_SCOPES = "payment-accounts:transactions:view payment-accounts:balances:view"

_app_token_cache: tuple[str, float] = ("", 0.0)


class INGApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _cert_serial_key_id() -> str:
    """Return keyId as SN=<hex-serial> from the signing certificate."""
    with open(SIGNING_CRT_PATH, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read(), default_backend())
    return f"SN={format(cert.serial_number, 'X')}"


def _tpp_cert_header() -> str:
    """Return signing cert as single-line string for TPP-Signature-Certificate header."""
    with open(SIGNING_CRT_PATH, "r") as f:
        return "".join(line.strip() for line in f if line.strip())


def _digest(body: bytes) -> str:
    return "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()


def _http_signature(method: str, path_with_query: str, date: str, digest: str,
                    key_id: str | None = None) -> str:
    signing_string = (
        f"(request-target): {method.lower()} {path_with_query}\n"
        f"date: {date}\n"
        f"digest: {digest}"
    )
    with open(SIGNING_KEY_PATH, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    sig = private_key.sign(signing_string.encode(), padding.PKCS1v15(), hashes.SHA256())
    if key_id is None:
        key_id = _cert_serial_key_id()
    return (
        f'keyId="{key_id}",algorithm="rsa-sha256",'
        f'headers="(request-target) date digest",signature="{base64.b64encode(sig).decode()}"'
    )


def _headers(token: str | None, method: str, path_with_query: str, body: bytes = b"") -> dict:
    date   = formatdate(usegmt=True)
    digest = _digest(body)
    h = {
        "Date":         date,
        "Digest":       digest,
        "X-ING-ReqID": str(uuid.uuid4()),
        "Accept":       "application/json",
    }
    if body:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        # Bearer-token calls: keyId is the client_id, signature in Signature header
        h["Authorization"] = f"Bearer {token}"
        h["Signature"]     = _http_signature(method, path_with_query, date, digest, key_id=CLIENT_ID)
    else:
        # App token request: keyId is SN=<cert-serial>, signature in Authorization, with TPP cert
        h["TPP-Signature-Certificate"] = _tpp_cert_header()
        h["Authorization"]             = f"Signature {_http_signature(method, path_with_query, date, digest)}"
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
    data  = _call("POST", TOKEN_URL, body=f"grant_type=client_credentials&client_id={CLIENT_ID}".encode())
    token = data["access_token"]
    _app_token_cache = (token, time.time() + data.get("expires_in", 900))
    return token


def get_authorization_url(state: str) -> str:
    """Build the ING authorize URL to redirect the user to for SCA/consent."""
    params = {
        "client_id":     CLIENT_ID,
        "scope":         _AIS_SCOPES,
        "state":         state,
        "redirect_uri":  SANDBOX_REDIRECT_URI,
        "response_type": "code",
    }
    return f"{AUTH_URL}/{COUNTRY_CODE}?{urlencode(params)}"


def exchange_code(code: str) -> str:
    """Exchange authorization code for customer access token."""
    global _app_token_cache
    _app_token_cache = ("", 0.0)  # force fresh app token
    body = f"grant_type=authorization_code&code={code}".encode()
    app_token = get_app_token()
    resp = _call("POST", TOKEN_URL, body=body, token=app_token)
    print("[ING] Customer token scope:", resp.get("scope"))
    print("[ING] Customer token expires_in:", resp.get("expires_in"))
    return resp["access_token"]


def get_accounts(customer_token: str) -> list:
    return _call("GET", f"{BASE_URL}/v3/accounts", token=customer_token).get("accounts", [])


def get_balances(customer_token: str, account_id: str) -> list:
    return _call("GET", f"{BASE_URL}/v3/accounts/{account_id}/balances",
                 token=customer_token).get("balances", [])


def get_transactions(customer_token: str, account_id: str) -> dict:
    date_from = (datetime.today() - timedelta(days=180)).strftime("%Y-%m-%d")
    data = _call("GET", f"{BASE_URL}/v3/accounts/{account_id}/transactions",
                 token=customer_token, params={"dateFrom": date_from})
    txns = data.get("transactions", {})
    if not isinstance(txns, dict):
        txns = {}
    return {
        "booked":  txns.get("booked",  []),
        "pending": txns.get("pending", []),
    }
