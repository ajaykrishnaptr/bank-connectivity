"""
Materialise PSD2 client certificates from environment variables.

Why this exists:
  The bank clients (`psd2_client`, `ing_client`) read certificate and key
  FILE PATHS, because `requests` presents mTLS certs from files. On Vercel
  the `certs/` folder is never deployed (it holds private keys), so the
  certs travel as base64 sensitive environment variables instead. This
  module decodes them to /tmp with mode 0600 and points the path variables
  the clients already read at those files.

Import it before any bank client module. It is a no-op when none of the
*_B64 variables are set, so a local checkout keeps using `certs/`.

Only leaf certificates and their keys belong here. The root, intermediate
and OCSP-signer keys never leave the laptop.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

# env var holding the base64 PEM  ->  env var the client reads the path from
_CERT_VARS = {
    "UC_CERT_B64":          "CERT_PATH",
    "UC_KEY_B64":           "KEY_PATH",
    "ING_TLS_CERT_B64":     "ING_TLS_CERT_PATH",
    "ING_TLS_KEY_B64":      "ING_TLS_KEY_PATH",
    "ING_SIGNING_CERT_B64": "ING_SIGNING_CERT_PATH",
    "ING_SIGNING_KEY_B64":  "ING_SIGNING_KEY_PATH",
}

CERT_DIR = Path(os.getenv("RUNTIME_CERT_DIR", "/tmp/fintnet-certs"))


def materialise() -> list[str]:
    """Write every configured cert to CERT_DIR and export its path.

    Returns the path variable names that were set, for logging.
    """
    written: list[str] = []
    for b64_var, path_var in _CERT_VARS.items():
        value = os.getenv(b64_var, "").strip()
        if not value:
            continue
        CERT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = CERT_DIR / f"{path_var.lower()}.pem"
        target.write_bytes(base64.b64decode(value))
        target.chmod(0o600)
        os.environ[path_var] = str(target)
        written.append(path_var)
    return written


MATERIALISED = materialise()
