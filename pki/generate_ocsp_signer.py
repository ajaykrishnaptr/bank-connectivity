"""
Mint a delegate OCSP signing cert under the existing intermediate CA.

The OCSP responder running on our public VM uses this delegate cert to
sign responses, so the intermediate CA private key never leaves the
laptop. The delegate cert carries:

  * Extended Key Usage = id-kp-OCSPSigning (mandatory per RFC 6960 to
    be accepted as a delegated responder).
  * id-pkix-ocsp-nocheck — tells clients not to recursively check the
    responder cert's own revocation status, which would otherwise loop.

Outputs:
    certs/ocsp_signer.crt
    certs/ocsp_signer.key
"""
import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier


CERTS_DIR = Path("certs")
DAYS = 365
OID_OCSP_NOCHECK = ObjectIdentifier("1.3.6.1.5.5.7.48.1.5")


def main() -> None:
    inter_cert = x509.load_pem_x509_certificate(
        (CERTS_DIR / "inter.crt").read_bytes(), default_backend()
    )
    inter_key = serialization.load_pem_private_key(
        (CERTS_DIR / "inter.key").read_bytes(), password=None, backend=default_backend()
    )

    signer_key = rsa.generate_private_key(65537, 2048, default_backend())

    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "DE"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TestTPP"),
        x509.NameAttribute(NameOID.COMMON_NAME, "FintNet OCSP Responder"),
    ])
    now = datetime.datetime.utcnow()

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(inter_cert.subject)
        .public_key(signer_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.OCSP_SIGNING]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(signer_key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(inter_cert.public_key()),
            critical=False,
        )
        .add_extension(x509.UnrecognizedExtension(OID_OCSP_NOCHECK, b"\x05\x00"), critical=False)
        .sign(inter_key, hashes.SHA256(), default_backend())
    )

    (CERTS_DIR / "ocsp_signer.key").write_bytes(signer_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    (CERTS_DIR / "ocsp_signer.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print("Wrote certs/ocsp_signer.crt and certs/ocsp_signer.key")


if __name__ == "__main__":
    main()
