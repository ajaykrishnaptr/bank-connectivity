"""
One-shot PKI script — generates a full PSD2 / eIDAS QWAC certificate
chain for testing against UniCredit's sandbox.

Why three certs and not one? PSD2 banks expect a real certificate
chain, not a self-signed leaf. We mint:

    Root CA  ->  Intermediate CA  ->  End-entity (the QWAC we present in mTLS)

The leaf cert carries the PSD2 QC-Statements extension that signals to
the bank "this TPP is licensed for AIS in DE, NCA = BaFin". Without
that extension the bank's parser refuses the cert.

Outputs (all under `certs/`):
    root.crt / root.key      Root CA — keep the key offline in production.
    inter.crt / inter.key    Intermediate — signs the leaf.
    chain.crt                inter + root concatenated; this is what the
                             bank wants in their trust store.
    eIDAS_test.crt / .key    End-entity. Used in `psd2_client.py`'s
                             `cert=(CERT_PATH, KEY_PATH)`.
    crl.crl                  Empty CRL signed by inter, hosted on
                             GitHub Pages so the leaf passes revocation
                             checks.

Usage:
    python3 generate_psd2_cert.py

After running, point your `.env` at the new files:
    CERT_PATH=certs/eIDAS_test.crt
    KEY_PATH=certs/eIDAS_test.key
"""
import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509 import ObjectIdentifier
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pyasn1.codec.der import encoder as der_encoder
from pyasn1.type import char, univ


CERTS_DIR = Path("certs")
CERTS_DIR.mkdir(exist_ok=True)


# ── PSD2 / ETSI Object Identifiers ───────────────────────────────────────────
# These OIDs are spelled out in the relevant ETSI standards
# (TS 119 495 for PSD2). Banks parse the QC-Statements extension on
# the leaf cert and look for these specific OIDs to identify a
# compliant PSD2 cert. Don't change them — they're how the bank knows
# what kind of cert it's looking at.
OID_QC_STATEMENTS  = "1.3.6.1.5.5.7.1.3"   # id-pe-qcStatements
OID_QC_COMPLIANCE  = "0.4.0.1862.1.1"      # id-etsi-qcs-QcCompliance
OID_QC_TYPE        = "0.4.0.1862.1.6"      # id-etsi-qcs-QcType
OID_QCT_WEB        = "0.4.0.1862.1.6.3"    # id-etsi-qct-web (QWAC)
OID_PSD2_STATEMENT = "0.4.0.19495.2"       # id-etsi-qcs-PSD2Statement
OID_PSP_AI         = "0.4.0.19495.1.2"     # PSP role: Account Information SP

# OID 2.5.4.97 = organizationIdentifier — required on PSD2 eIDAS leafs.
OID_ORGANIZATION_IDENTIFIER = ObjectIdentifier("2.5.4.97")

# Two years matches what UniCredit's docs ask for.
DAYS = 730


def new_key() -> rsa.RSAPrivateKey:
    """Fresh 2048-bit RSA key — adequate for sandbox; production
    might want 3072+."""
    return rsa.generate_private_key(65537, 2048, default_backend())


def save(path: Path, obj) -> None:
    """Write a key (PKCS#1 / TraditionalOpenSSL PEM) or a cert
    (X.509 PEM) to disk and log the path."""
    with open(path, "wb") as f:
        if isinstance(obj, rsa.RSAPrivateKey) or hasattr(obj, "private_bytes"):
            f.write(obj.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        else:
            f.write(obj.public_bytes(serialization.Encoding.PEM))
    print(f"  Written: {path}")


def _oid(dotted: str) -> univ.ObjectIdentifier:
    """Convert "0.4.0.1862.1.1" -> a pyasn1 ObjectIdentifier."""
    return univ.ObjectIdentifier([int(x) for x in dotted.split(".")])


def build_psd2_qc_extension() -> bytes:
    """Hand-build the DER bytes of the PSD2 qcStatements extension.

    The extension is a SEQUENCE OF QcStatement, where each QcStatement
    is its own SEQUENCE { OID [, statementInfo] }. We pack three:

      1. QcCompliance         — bare OID, no info ("yes I'm a QC").
      2. QcType = qct-web      — declares this is a QWAC (web auth).
      3. PSD2Statement         — OID + a struct containing:
            - SEQUENCE OF Role { OID, UTF8String }   (we register PSP_AI)
            - UTF8String "BaFin"                       (NCA name)
            - UTF8String "PSDDE-BAFIN-19337"           (NCA-issued ID)

    Banks parse this lump back into ASN.1 and check our PSP role + NCA
    line up with their internal allowlist.
    """
    # 1. QcCompliance
    stmt_compliance = univ.Sequence()
    stmt_compliance.setComponentByPosition(0, _oid(OID_QC_COMPLIANCE))

    # 2. QcType = qct-web (QWAC marker)
    qc_type_value = univ.Sequence()
    qc_type_value.setComponentByPosition(0, _oid(OID_QCT_WEB))
    stmt_qc_type = univ.Sequence()
    stmt_qc_type.setComponentByPosition(0, _oid(OID_QC_TYPE))
    stmt_qc_type.setComponentByPosition(1, qc_type_value)

    # 3. PSD2Statement — roles + NCA + NCA ID
    role = univ.Sequence()
    role.setComponentByPosition(0, _oid(OID_PSP_AI))
    role.setComponentByPosition(1, char.UTF8String("PSP_AI"))

    roles = univ.SequenceOf()
    roles.setComponentByPosition(0, role)

    psd2_qc = univ.Sequence()
    psd2_qc.setComponentByPosition(0, roles)
    psd2_qc.setComponentByPosition(1, char.UTF8String("BaFin"))
    psd2_qc.setComponentByPosition(2, char.UTF8String("PSDDE-BAFIN-19337"))

    stmt_psd2 = univ.Sequence()
    stmt_psd2.setComponentByPosition(0, _oid(OID_PSD2_STATEMENT))
    stmt_psd2.setComponentByPosition(1, psd2_qc)

    # Pack all three into one outer SEQUENCE OF and DER-encode.
    qc_statements = univ.SequenceOf()
    qc_statements.setComponentByPosition(0, stmt_compliance)
    qc_statements.setComponentByPosition(1, stmt_qc_type)
    qc_statements.setComponentByPosition(2, stmt_psd2)

    return der_encoder.encode(qc_statements)


def make_name(cn: str, org: str = "TestTPP", country: str = "DE",
              org_id: str | None = None) -> x509.Name:
    """Standard X.509 Name with C / O / CN, plus optional
    organizationIdentifier (required on PSD2 leafs)."""
    attrs = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, country),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ]
    if org_id:
        attrs.append(x509.NameAttribute(OID_ORGANIZATION_IDENTIFIER, org_id))
    return x509.Name(attrs)


def now() -> datetime.datetime:
    """UTC `now`. Wrapped because cryptography expects naive UTC and
    the constructor is repeated four times below."""
    return datetime.datetime.utcnow()


def main() -> None:
    """Generate the full chain end-to-end and write it to `certs/`."""

    # ── 1. ROOT CA ───────────────────────────────────────────────────────────
    print("\n[1/3] Generating Root CA...")
    root_key  = new_key()
    root_name = make_name("AK-Test-TPP Root CA")
    root_cert = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)              # self-signed
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()),
                       critical=False)
        .sign(root_key, hashes.SHA256(), default_backend())
    )
    save(CERTS_DIR / "root.key", root_key)
    save(CERTS_DIR / "root.crt", root_cert)


    # ── 2. INTERMEDIATE CA ───────────────────────────────────────────────────
    # path_length=0 means: this CA can sign end-entity certs but
    # cannot mint another CA below itself. Standard for a 2-tier PKI.
    print("\n[2/3] Generating Intermediate CA...")
    inter_key  = new_key()
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(inter_key.public_key()),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
                       critical=False)
        .sign(root_key, hashes.SHA256(), default_backend())
    )
    save(CERTS_DIR / "inter.key", inter_key)
    save(CERTS_DIR / "inter.crt", inter_cert)

    # chain.crt = intermediate + root, in that order. UniCredit
    # imports this into their trust store; presenting just the leaf
    # would fail because they need to walk the chain to a trusted root.
    chain_path = CERTS_DIR / "chain.crt"
    with open(chain_path, "wb") as f:
        f.write(inter_cert.public_bytes(serialization.Encoding.PEM))
        f.write(root_cert.public_bytes(serialization.Encoding.PEM))
    print(f"  Written: {chain_path}")


    # ── 3. END-ENTITY PSD2 CERT (eIDAS QWAC) ─────────────────────────────────
    # The leaf has CLIENT_AUTH (we use it for mTLS) and SERVER_AUTH
    # (some banks' tools require both). The PSD2 qcStatements extension
    # is what marks this as a real PSD2 cert rather than a vanilla web cert.
    print("\n[3/3] Generating eIDAS PSD2 end-entity certificate...")
    ee_key  = new_key()
    ee_name = make_name("testAK-tpp.unicredit.eu", org_id="PSDDE-BAFIN-19337")
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
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("testAK-tpp.unicredit.eu")]),
                       critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ee_key.public_key()),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(inter_key.public_key()),
                       critical=False)
        # Both URIs are plain http:// — F5 BIG-IP gateways (UniCredit's
        # included) refuse https:// for revocation endpoints.
        .add_extension(x509.AuthorityInformationAccess([
            x509.AccessDescription(
                x509.AuthorityInformationAccessOID.OCSP,
                x509.UniformResourceIdentifier("http://ocsp.fintnet.ai"),
            ),
            x509.AccessDescription(
                x509.AuthorityInformationAccessOID.CA_ISSUERS,
                x509.UniformResourceIdentifier("http://crl.fintnet.ai/inter.crt"),
            ),
        ]), critical=False)
        .add_extension(x509.CRLDistributionPoints([
            x509.DistributionPoint(
                full_name=[x509.UniformResourceIdentifier(
                    "http://crl.fintnet.ai/crl.crl")],
                relative_name=None,
                reasons=None,
                crl_issuer=None,
            )
        ]), critical=False)
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


    # ── 4. CRL ───────────────────────────────────────────────────────────────
    # Empty CRL — no certs revoked yet. Hosted at the CRLDistributionPoints
    # URL on the leaf so revocation checks succeed.
    print("\n[4/4] Generating CRL...")
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(inter_name)
        .last_update(now())
        .next_update(now() + datetime.timedelta(days=30))
        .sign(inter_key, hashes.SHA256(), default_backend())
    )
    crl_path = CERTS_DIR / "crl.crl"
    with open(crl_path, "wb") as f:
        f.write(crl.public_bytes(serialization.Encoding.DER))
    print(f"  Written: {crl_path}")


    # ── 5. OCSP responder index.txt ──────────────────────────────────────────
    # Our OCSP responder (openssl ocsp -port) consults an index.txt to decide
    # whether a serial is V (valid), R (revoked), or unknown. We mark our
    # leaf as V; the file gets pushed to the responder VM after each re-issue.
    print("\n[5/5] Writing OCSP index.txt for the new leaf...")
    write_ocsp_index(ee_cert, CERTS_DIR / "ocsp_index.txt")

    print("\nDone. Summary:")
    print("  Submit to UniCredit: certs/chain.crt (trust chain)")
    print("  Use in .env:         CERT_PATH=certs/eIDAS_test.crt")
    print("                       KEY_PATH=certs/eIDAS_test.key")
    print("  OCSP / CRL served from VM at fintnet.ai (push ocsp_index.txt + crl.crl + inter.crt)")


def write_ocsp_index(ee_cert: x509.Certificate, path: Path) -> None:
    """Write a one-line openssl `index.txt` entry marking `ee_cert` as V (valid).

    Format (tab-separated): status, expiry (YYMMDDHHMMSSZ), revocation date,
    serial (uppercase hex), filename, subject DN.
    """
    serial   = format(ee_cert.serial_number, "X")
    not_after = ee_cert.not_valid_after.strftime("%y%m%d%H%M%SZ")
    # openssl one-line subject runs least- to most-specific (C first, CN last);
    # cryptography iterates the opposite way, so reverse it.
    subj = "/" + "/".join(
        f"{a.rfc4514_attribute_name}={a.value}" for a in reversed(list(ee_cert.subject))
    )
    line = f"V\t{not_after}\t\t{serial}\tunknown\t{subj}\n"
    path.write_text(line)
    print(f"  Written: {path}")


if __name__ == "__main__":
    main()
