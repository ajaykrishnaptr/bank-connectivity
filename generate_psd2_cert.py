"""
Generates a full PSD2 certificate chain:
  Root CA → Intermediate CA → End-entity eIDAS PSD2 cert (QWAC)

Outputs:
  certs/root.crt          — Root CA certificate
  certs/root.key          — Root CA private key
  certs/inter.crt         — Intermediate CA certificate
  certs/inter.key         — Intermediate CA private key
  certs/chain.crt         — Intermediate + Root (send to UniCredit for trust)
  certs/eIDAS_test.crt    — End-entity PSD2 certificate (used in mTLS)
  certs/eIDAS_test.key    — End-entity private key
"""
import datetime
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509 import ObjectIdentifier
from cryptography.hazmat.backends import default_backend
from pyasn1.type import univ, char
from pyasn1.codec.der import encoder as der_encoder

CERTS_DIR = Path("certs")
CERTS_DIR.mkdir(exist_ok=True)

# PSD2 OIDs
OID_QC_STATEMENTS  = "1.3.6.1.5.5.7.1.3"   # id-pe-qcStatements
OID_PSD2_STATEMENT = "0.4.0.19495.2"         # id-etsi-qcs-PSD2Statement
OID_PSP_AI         = "0.4.0.19495.1.2"       # Account Information SP

DAYS = 730  # 2 years to match the instructions


def new_key():
    return rsa.generate_private_key(65537, 2048, default_backend())


def save(path, obj):
    with open(path, "wb") as f:
        if isinstance(obj, (rsa.RSAPrivateKey,)) or hasattr(obj, "private_bytes"):
            f.write(obj.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        else:
            f.write(obj.public_bytes(serialization.Encoding.PEM))
    print(f"  Written: {path}")


def build_psd2_qc_extension() -> bytes:
    role = univ.Sequence()
    role.setComponentByPosition(0, univ.ObjectIdentifier(
        [int(x) for x in OID_PSP_AI.split(".")]
    ))
    role.setComponentByPosition(1, char.UTF8String("PSP_AI"))

    roles = univ.SequenceOf()
    roles.setComponentByPosition(0, role)

    psd2_qc = univ.Sequence()
    psd2_qc.setComponentByPosition(0, roles)
    psd2_qc.setComponentByPosition(1, char.UTF8String("BaFin"))
    psd2_qc.setComponentByPosition(2, char.UTF8String("PSDDE-BAFIN-19337"))

    statement = univ.Sequence()
    statement.setComponentByPosition(0, univ.ObjectIdentifier(
        [int(x) for x in OID_PSD2_STATEMENT.split(".")]
    ))
    statement.setComponentByPosition(1, psd2_qc)

    qc_statements = univ.SequenceOf()
    qc_statements.setComponentByPosition(0, statement)

    return der_encoder.encode(qc_statements)


def make_name(cn, org="TestTPP", country="DE", serial=None):
    attrs = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, country),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ]
    if serial:
        attrs.append(x509.NameAttribute(NameOID.SERIAL_NUMBER, serial))
    return x509.Name(attrs)


def now():
    return datetime.datetime.utcnow()


# ── 1. ROOT CA ───────────────────────────────────────────────────────────────
print("\n[1/3] Generating Root CA...")
root_key = new_key()
root_name = make_name("AK-Test-TPP Root CA")
root_cert = (
    x509.CertificateBuilder()
    .subject_name(root_name)
    .issuer_name(root_name)
    .public_key(root_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now())
    .not_valid_after(now() + datetime.timedelta(days=DAYS))
    .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    .add_extension(x509.KeyUsage(
        digital_signature=True, content_commitment=False, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=True,
        crl_sign=True, encipher_only=False, decipher_only=False,
    ), critical=True)
    .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
    .sign(root_key, hashes.SHA256(), default_backend())
)
save(CERTS_DIR / "root.key", root_key)
save(CERTS_DIR / "root.crt", root_cert)


# ── 2. INTERMEDIATE CA ───────────────────────────────────────────────────────
print("\n[2/3] Generating Intermediate CA...")
inter_key = new_key()
inter_name = make_name("AK-Test-TPP Intermediate CA")
inter_cert = (
    x509.CertificateBuilder()
    .subject_name(inter_name)
    .issuer_name(root_name)
    .public_key(inter_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now())
    .not_valid_after(now() + datetime.timedelta(days=DAYS))
    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
    .add_extension(x509.KeyUsage(
        digital_signature=True, content_commitment=False, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=True,
        crl_sign=True, encipher_only=False, decipher_only=False,
    ), critical=True)
    .add_extension(x509.SubjectKeyIdentifier.from_public_key(inter_key.public_key()), critical=False)
    .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
    .sign(root_key, hashes.SHA256(), default_backend())
)
save(CERTS_DIR / "inter.key", inter_key)
save(CERTS_DIR / "inter.crt", inter_cert)

# chain.crt = inter + root (what UniCredit needs to configure trust)
chain_path = CERTS_DIR / "chain.crt"
with open(chain_path, "wb") as f:
    f.write(inter_cert.public_bytes(serialization.Encoding.PEM))
    f.write(root_cert.public_bytes(serialization.Encoding.PEM))
print(f"  Written: {chain_path}")


# ── 3. END-ENTITY PSD2 CERT (eIDAS QWAC) ────────────────────────────────────
print("\n[3/3] Generating eIDAS PSD2 end-entity certificate...")
ee_key = new_key()
ee_name = make_name("AK-Test-TPP", serial="PSDDE-BAFIN-19337")
ee_cert = (
    x509.CertificateBuilder()
    .subject_name(ee_name)
    .issuer_name(inter_name)
    .public_key(ee_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now())
    .not_valid_after(now() + datetime.timedelta(days=DAYS))
    .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    .add_extension(x509.KeyUsage(
        digital_signature=True, content_commitment=False, key_encipherment=True,
        data_encipherment=False, key_agreement=False, key_cert_sign=False,
        crl_sign=False, encipher_only=False, decipher_only=False,
    ), critical=True)
    .add_extension(x509.ExtendedKeyUsage([
        ExtendedKeyUsageOID.CLIENT_AUTH,
        ExtendedKeyUsageOID.SERVER_AUTH,
    ]), critical=False)
    .add_extension(x509.SubjectAlternativeName([x509.DNSName("AK-Test-TPP")]), critical=False)
    .add_extension(x509.SubjectKeyIdentifier.from_public_key(ee_key.public_key()), critical=False)
    .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(inter_key.public_key()), critical=False)
    .add_extension(
        x509.UnrecognizedExtension(
            oid=ObjectIdentifier(OID_QC_STATEMENTS),
            value=build_psd2_qc_extension(),
        ),
        critical=False,
    )
    .sign(inter_key, hashes.SHA256(), default_backend())
)
save(CERTS_DIR / "eIDAS_test.key", ee_key)
save(CERTS_DIR / "eIDAS_test.crt", ee_cert)

print("\nDone. Summary:")
print("  Submit to UniCredit: certs/chain.crt (trust chain)")
print("  Use in .env:         CERT_PATH=certs/eIDAS_test.crt")
print("                       KEY_PATH=certs/eIDAS_test.key")
