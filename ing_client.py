"""
ING Open Banking client (the Berlin-Group implementation).

ING is the most intricate auth dance of the five banks we support,
because every request has to be:
  1. mTLS-authenticated (TLS client cert via `cert=` in `requests`),
  2. body-digest hashed (`Digest: SHA-256=<base64>`),
  3. HTTP-signed (RFC-9421-ish "Signature" header over `(request-target)`,
     `date`, `digest`).

ING uses two distinct signing modes depending on whose identity is
being asserted, and `_headers()` toggles between them based on whether
a `token` was passed:

  Mode A — App-level (no customer token yet)
    keyId    = SN=<hex serial of the signing cert>
    payload  = signed with the TPP signing key
    extras   = `TPP-Signature-Certificate` carries the cert inline
    auth     = `Authorization: Signature ...` (the signature *is* the auth)

  Mode B — Per-customer (after we have a customer Bearer token)
    keyId    = the OAuth client_id
    payload  = same signing-string format, signed with the TPP signing key
    extras   = none
    auth     = `Authorization: Bearer <token>` AND a separate `Signature`
               header (so both a token *and* a sig are present)

This split is invisible to the rest of the app — `get_accounts`,
`get_balances` and `get_transactions` all just take a `customer_token`
string. `get_app_token` is the only function that runs in Mode A.
"""
import base64
import hashlib
import os
import time
import uuid
from datetime import datetime, timedelta
from email.utils import formatdate
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

from logging_config import log

load_dotenv()

BASE_URL  = "https://api.sandbox.ing.com"
AUTH_URL  = "https://myaccount.sandbox.ing.com/authorize/v2"
TOKEN_URL = f"{BASE_URL}/oauth2/token"

CLIENT_ID        = os.getenv("ING_CLIENT_ID", "")
SIGNING_KEY_PATH = os.getenv("ING_SIGNING_KEY_PATH",  "certs/ing_signing.key")
SIGNING_CRT_PATH = os.getenv("ING_SIGNING_CERT_PATH", "certs/ing_signing.cer")

# mTLS cert/key pair. Separate files from the signing pair above:
# the TLS pair is presented at TCP-handshake time, the signing pair
# at HTTP-message time. ING insists on both.
CERT = (
    os.getenv("ING_TLS_CERT_PATH", "certs/ing_tls.cer"),
    os.getenv("ING_TLS_KEY_PATH",  "certs/ing_tls.key"),
)
COUNTRY_CODE = os.getenv("ING_COUNTRY_CODE", "NL")

# ING's sandbox redirect URI is hardcoded to example.com — the user
# has to copy the auth `code` out of the browser and paste it back
# (see /ing/enter-code in app.py).
SANDBOX_REDIRECT_URI = "https://www.example.com/"

_AIS_SCOPES = "payment-accounts:transactions:view payment-accounts:balances:view"

_DEFAULT_HTTP_TIMEOUT   = 15
_TOKEN_REFRESH_MARGIN_S = 30
_TXN_LOOKBACK_DAYS      = 180

# (access_token, expiry_epoch). Process-wide cache for the *app* token only.
_app_token_cache: tuple[str, float] = ("", 0.0)


class INGApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


# ── Signing helpers ──────────────────────────────────────────────────────────

def _cert_serial_key_id() -> str:
    """Build the keyId used in Mode A — `SN=<uppercase hex serial>` of
    the signing certificate. ING uses the cert serial to identify
    which TPP signed a request before it has a Bearer token."""
    with open(SIGNING_CRT_PATH, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read(), default_backend())
    return f"SN={format(cert.serial_number, 'X')}"


def _tpp_cert_header() -> str:
    """Return the signing certificate as a single line, suitable for the
    `TPP-Signature-Certificate` header (Mode A only). The bank parses
    this back into a cert at the other end."""
    with open(SIGNING_CRT_PATH, "r") as f:
        return "".join(line.strip() for line in f if line.strip())


def _digest(body: bytes) -> str:
    """SHA-256 digest of the request body, formatted as
    `SHA-256=<base64>` for the `Digest` header. Empty bodies still
    produce a valid digest of "" — required even on GETs."""
    return "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()


def _http_signature(method: str, path_with_query: str, date: str, digest: str,
                    key_id: str | None = None) -> str:
    """Build the HTTP-signature header value.

    The signing string is canonical and order-sensitive — three lines,
    in this exact order, separated by `\\n`:

        (request-target): <method-lower> <path?query>
        date: <RFC 7231 date>
        digest: <SHA-256=base64>

    We sign it with the TPP signing key (RSA / PKCS#1 v1.5 / SHA-256)
    and return the structured `keyId="...",algorithm="...",...` string
    the bank expects in either `Signature` (Mode B) or `Authorization`
    (Mode A — prefixed with `Signature `).
    """
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


def _headers(token: str | None, method: str, path_with_query: str,
             body: bytes = b"") -> dict:
    """Build the full header set for one ING request.

    Switches between Mode A (no token, signature is the auth) and
    Mode B (Bearer token + separate Signature header) based on whether
    `token` is set. See module docstring for the long form.
    """
    date   = formatdate(usegmt=True)
    digest = _digest(body)
    h = {
        "Date":         date,
        "Digest":       digest,
        "X-ING-ReqID":  str(uuid.uuid4()),
        "Accept":       "application/json",
    }
    if body:
        h["Content-Type"] = "application/x-www-form-urlencoded"

    if token:
        # Mode B: customer-context call.
        h["Authorization"] = f"Bearer {token}"
        h["Signature"]     = _http_signature(method, path_with_query, date, digest, key_id=CLIENT_ID)
    else:
        # Mode A: app-context call (token endpoints).
        h["TPP-Signature-Certificate"] = _tpp_cert_header()
        h["Authorization"] = f"Signature {_http_signature(method, path_with_query, date, digest)}"
    return h


def _call(method: str, url: str, body: bytes = b"",
          token: str | None = None, params: dict | None = None):
    """Send a fully-signed request and return decoded JSON.

    Note `path` is rebuilt from `urlparse(url).path` plus the
    serialised `params` — the signature must cover the *exact* string
    that appears on the wire as the request target, so we can't rely
    on `requests` to canonicalise it for us.
    """
    parsed = urlparse(url)
    path   = parsed.path + (f"?{urlencode(params)}" if params else "")
    try:
        resp = requests.request(
            method, url,
            headers=_headers(token, method, path, body),
            data=body or None,
            params=params,
            cert=CERT,
            timeout=_DEFAULT_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        raise INGApiError(str(e), status_code=resp.status_code)
    except requests.exceptions.RequestException as e:
        raise INGApiError(f"Request failed: {e}")


# ── Auth flow ────────────────────────────────────────────────────────────────

def get_app_token() -> str:
    """Return a valid app-level (client_credentials) token, fetching or
    refreshing as needed. This token is only used as the `token` in
    Mode-B calls during the customer-token exchange step."""
    global _app_token_cache
    token, expiry = _app_token_cache
    if token and time.time() < expiry - _TOKEN_REFRESH_MARGIN_S:
        return token

    body  = f"grant_type=client_credentials&client_id={CLIENT_ID}".encode()
    data  = _call("POST", TOKEN_URL, body=body)
    token = data["access_token"]
    _app_token_cache = (token, time.time() + data.get("expires_in", 900))
    return token


def get_authorization_url(state: str) -> str:
    """Build the consent-URL we redirect the user to. ING's UI handles
    SCA and then redirects to `SANDBOX_REDIRECT_URI` with `?code=…`."""
    params = {
        "client_id":     CLIENT_ID,
        "scope":         _AIS_SCOPES,
        "state":         state,
        "redirect_uri":  SANDBOX_REDIRECT_URI,
        "response_type": "code",
    }
    return f"{AUTH_URL}/{COUNTRY_CODE}?{urlencode(params)}"


def exchange_code(code: str) -> str:
    """Trade the authorization code for a customer access token.

    We force a fresh app token first because some sandboxes reject
    code exchange when a stale (but still time-valid) app token is
    presented.
    """
    global _app_token_cache
    _app_token_cache = ("", 0.0)
    app_token = get_app_token()

    body = f"grant_type=authorization_code&code={code}".encode()
    resp = _call("POST", TOKEN_URL, body=body, token=app_token)

    log.info("ing.customer_token", extra={
        "event":      "ing.customer_token",
        "scope":      resp.get("scope"),
        "expires_in": resp.get("expires_in"),
    })
    return resp["access_token"]


# ── Data endpoints ───────────────────────────────────────────────────────────

def get_accounts(customer_token: str) -> list:
    return _call("GET", f"{BASE_URL}/v3/accounts", token=customer_token).get("accounts", [])


def get_balances(customer_token: str, account_id: str) -> list:
    return _call("GET", f"{BASE_URL}/v3/accounts/{account_id}/balances",
                 token=customer_token).get("balances", [])


def get_transactions(customer_token: str, account_id: str) -> dict:
    """Return last `_TXN_LOOKBACK_DAYS` of transactions in
    `{"booked": [...], "pending": [...]}` shape.

    Defensive: ING occasionally returns `transactions: null` on
    accounts with zero history; we coerce to an empty dict so the
    caller never has to type-check.
    """
    date_from = (datetime.today() - timedelta(days=_TXN_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    data = _call("GET", f"{BASE_URL}/v3/accounts/{account_id}/transactions",
                 token=customer_token, params={"dateFrom": date_from})
    txns = data.get("transactions", {})
    if not isinstance(txns, dict):
        txns = {}
    return {
        "booked":  txns.get("booked",  []),
        "pending": txns.get("pending", []),
    }
