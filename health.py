"""
Daily health check for the PSD2 trust chain the live sandboxes depend on.

Checks:
  * CRL at http://crl.fintnet.ai/crl.crl: reachable, and days until nextUpdate.
    UniCredit's gateway hard-fails mTLS on an expired CRL; refresh with
    `deploy_crl.sh` from the laptop (the signing key never leaves it).
  * OCSP status of the UniCredit leaf certificate at the URL in its AIA.
  * Expiry of every client certificate the app can load.

Status: "error" when the CRL is expired or unreachable or OCSP is not GOOD;
"warn" when the CRL expires within CRL_WARN_DAYS or a certificate within
CERT_WARN_DAYS; otherwise "ok".
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509 import ocsp
from cryptography.x509.oid import AuthorityInformationAccessOID

CRL_URL = os.getenv("CRL_URL", "http://crl.fintnet.ai/crl.crl")
CRL_WARN_DAYS = 7
CERT_WARN_DAYS = 60

_CERT_PATH_VARS = {
    "UniCredit QWAC": ("CERT_PATH", "certs/eIDAS_test.crt"),
    "ING TLS": ("ING_TLS_CERT_PATH", "certs/ing_tls.cer"),
    "ING signing": ("ING_SIGNING_CERT_PATH", "certs/ing_signing.cer"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_cert(data: bytes) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(data)
    except ValueError:
        return x509.load_der_x509_certificate(data)


def check_crl() -> dict:
    try:
        resp = requests.get(CRL_URL, timeout=15)
        resp.raise_for_status()
        try:
            crl = x509.load_der_x509_crl(resp.content)
        except ValueError:
            crl = x509.load_pem_x509_crl(resp.content)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "url": CRL_URL, "error": str(exc)[:200]}
    next_update = crl.next_update_utc
    days = (next_update - _now()).total_seconds() / 86400 if next_update else None
    status = "error" if days is None or days < 0 else ("warn" if days < CRL_WARN_DAYS else "ok")
    return {"status": status, "url": CRL_URL, "last_update": crl.last_update_utc.isoformat(),
            "next_update": next_update.isoformat() if next_update else None,
            "days_left": round(days, 1) if days is not None else None}


def check_ocsp() -> dict:
    path = os.getenv(*_CERT_PATH_VARS["UniCredit QWAC"])
    try:
        with open(path, "rb") as f:
            leaf = _load_cert(f.read())
        aia = leaf.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
        ocsp_url = next(d.access_location.value for d in aia if d.access_method == AuthorityInformationAccessOID.OCSP)
        issuer_url = next(d.access_location.value for d in aia
                          if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS)
        issuer = _load_cert(requests.get(issuer_url, timeout=15).content)
        req = ocsp.OCSPRequestBuilder().add_certificate(leaf, issuer, hashes.SHA1()).build()
        resp = requests.post(ocsp_url, data=req.public_bytes(serialization.Encoding.DER),
                             headers={"Content-Type": "application/ocsp-request"}, timeout=15)
        parsed = ocsp.load_der_ocsp_response(resp.content)
        if parsed.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL:
            return {"status": "error", "url": ocsp_url, "response_status": parsed.response_status.name}
        cert_status = parsed.certificate_status.name
        return {"status": "ok" if cert_status == "GOOD" else "error", "url": ocsp_url, "certificate_status": cert_status,
                "this_update": parsed.this_update_utc.isoformat(),
                "next_update": parsed.next_update_utc.isoformat() if parsed.next_update_utc else None}
    except FileNotFoundError:
        return {"status": "warn", "error": f"UniCredit certificate not found at {path}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def check_certificates() -> dict:
    out = {}
    for label, (var, default) in _CERT_PATH_VARS.items():
        path = os.getenv(var, default)
        try:
            with open(path, "rb") as f:
                cert = _load_cert(f.read())
        except FileNotFoundError:
            out[label] = {"status": "warn", "error": "not configured"}
            continue
        days = (cert.not_valid_after_utc - _now()).days
        out[label] = {"status": "error" if days < 0 else ("warn" if days < CERT_WARN_DAYS else "ok"),
                      "not_after": cert.not_valid_after_utc.isoformat(), "days_left": days}
    return out


def run() -> dict:
    result = {"crl": check_crl(), "ocsp": check_ocsp(), "certificates": check_certificates()}
    statuses = [result["crl"]["status"], result["ocsp"]["status"]] + [c["status"] for c in result["certificates"].values()]
    result["status"] = "error" if "error" in statuses else ("warn" if "warn" in statuses else "ok")
    return result
