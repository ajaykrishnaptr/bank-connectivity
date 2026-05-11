"""
N26 sandbox TLS spike.

Single-purpose throwaway: confirm N26's sandbox accepts our self-signed
QWAC chain at the TLS layer before we commit to a full client.

What it does:
  1. Reads the leaf cert and pulls `organizationIdentifier` out — that's
     our PSD2 client_id per N26's docs.
  2. Builds an OAuth2 authorize URL with PKCE (S256).
  3. Hits GET /sandbox/oauth2/authorize over mTLS.

Possible outcomes:
  TLS handshake fails        -> N26 doesn't trust our chain. Email them the
                                cert hash for whitelisting (UniCredit path).
  HTTP 302 with Location     -> They accepted the cert AND the client_id.
                                Good to go — Location points to SCA flow.
  HTTP 4xx with body         -> Cert accepted at TLS layer; auth params
                                wrong. Read the body to see what's off.
"""
import base64
import hashlib
import os
import secrets
import urllib.parse

import requests
from cryptography import x509
from cryptography.x509.oid import ObjectIdentifier

CERT_PATH = "certs/eIDAS_test.crt"
KEY_PATH  = "certs/eIDAS_test.key"

N26_AUTHORIZE = "https://xs2a.tech26.de/sandbox/oauth2/authorize"
REDIRECT_URI  = "https://www.example.com/"   # placeholder; updated when we wire routes
SCOPE         = "DEDICATED_AISP"


def extract_org_id(cert_path: str) -> str:
    """Pull organizationIdentifier (OID 2.5.4.97) out of the leaf cert.
    That's what N26 expects as client_id."""
    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    oid = ObjectIdentifier("2.5.4.97")
    for attr in cert.subject:
        if attr.oid == oid:
            return attr.value
    raise SystemExit("Leaf cert has no organizationIdentifier — regen with generate_psd2_cert.py")


def pkce_pair() -> tuple[str, str]:
    """RFC 7636 S256 PKCE pair."""
    verifier  = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def main() -> None:
    for p in (CERT_PATH, KEY_PATH):
        if not os.path.exists(p):
            raise SystemExit(f"Missing cert file: {p}")

    client_id = extract_org_id(CERT_PATH)
    print(f"client_id (from cert org ID): {client_id}")

    _, challenge = pkce_pair()
    params = {
        "client_id":             client_id,
        "scope":                 SCOPE,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "redirect_uri":          REDIRECT_URI,
        "response_type":         "CODE",
        "state":                 secrets.token_urlsafe(8),
    }
    url = f"{N26_AUTHORIZE}?{urllib.parse.urlencode(params)}"
    print(f"GET {url}\n")

    import uuid
    headers = {
        "X-Request-ID": str(uuid.uuid4()),
        "Accept":       "application/json",
    }
    try:
        resp = requests.get(url, cert=(CERT_PATH, KEY_PATH),
                            headers=headers,
                            allow_redirects=False, timeout=15)
    except requests.exceptions.SSLError as e:
        print(f"TLS FAILED — N26 did not accept the cert at the transport layer:\n  {e}")
        return
    except requests.exceptions.RequestException as e:
        print(f"Request error: {e}")
        return

    print(f"HTTP {resp.status_code}")
    print("Response headers:")
    for k, v in resp.headers.items():
        print(f"  {k}: {v}")
    if resp.text:
        print("\nBody (first 800 chars):")
        print(resp.text[:800])


if __name__ == "__main__":
    main()
