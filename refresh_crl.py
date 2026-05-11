"""
Regenerate the empty CRL signed by our PSD2 intermediate CA.

Why this exists:
  The CRL on our OCSP responder VM has a `nextUpdate` field 30 days
  after generation. F5 (UniCredit's gateway) hard-fails mTLS when it
  fetches an expired CRL, so the file must be refreshed before that
  deadline. We do it weekly to keep at least ~23 days of headroom.

Inputs:
  certs/inter.crt, certs/inter.key — the intermediate that signs the CRL.

Output:
  certs/crl.crl — DER-encoded empty CRL, valid 30 days from now.

This script does NOT push to the VM. `deploy_crl.sh` handles transport.
"""
import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization

CERTS_DIR     = Path(__file__).resolve().parent / "certs"
INTER_CRT     = CERTS_DIR / "inter.crt"
INTER_KEY     = CERTS_DIR / "inter.key"
CRL_OUT       = CERTS_DIR / "crl.crl"
VALID_DAYS    = 30


def main() -> None:
    for p in (INTER_CRT, INTER_KEY):
        if not p.exists():
            raise SystemExit(f"Missing required file: {p}")

    with open(INTER_KEY, "rb") as f:
        inter_key = serialization.load_pem_private_key(f.read(), password=None,
                                                       backend=default_backend())
    with open(INTER_CRT, "rb") as f:
        inter_cert = x509.load_pem_x509_certificate(f.read(), default_backend())

    now = datetime.datetime.utcnow()
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(inter_cert.subject)
        .last_update(now)
        .next_update(now + datetime.timedelta(days=VALID_DAYS))
        .sign(inter_key, hashes.SHA256(), default_backend())
    )
    CRL_OUT.write_bytes(crl.public_bytes(serialization.Encoding.DER))

    print(f"Wrote {CRL_OUT}")
    print(f"  lastUpdate: {crl.last_update_utc.isoformat()}")
    print(f"  nextUpdate: {crl.next_update_utc.isoformat()}")


if __name__ == "__main__":
    main()
