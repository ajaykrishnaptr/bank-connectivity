"""
Generates a PSD2-compliant self-signed QWAC certificate with the mandatory
id-etsi-qcs-PSD2Statement (OID 0.4.0.19495.2) qcStatements extension.
"""
import datetime
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509 import ObjectIdentifier
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.backends import default_backend
from pyasn1.type import univ, char, namedtype
from pyasn1.codec.der import encoder as der_encoder

# ── PSD2 OIDs ────────────────────────────────────────────────────────────────
OID_QC_STATEMENTS      = "1.3.6.1.5.5.7.1.3"       # id-pe-qcStatements
OID_PSD2_STATEMENT     = "0.4.0.19495.2"             # id-etsi-qcs-PSD2Statement
OID_PSP_AI             = "0.4.0.19495.1.2"           # Account Information SP


def build_psd2_qc_statement(nca_name: str, nca_id: str) -> bytes:
    """
    Encode the PSD2QcType ASN.1 structure:

    PSD2QcType ::= SEQUENCE {
        rolesOfPSP  SEQUENCE OF SEQUENCE { OID, UTF8String },
        nCAName     UTF8String,
        nCAId       UTF8String
    }
    """
    role = univ.Sequence()
    role.setComponentByPosition(0, univ.ObjectIdentifier(
        [int(x) for x in OID_PSP_AI.split(".")]
    ))
    role.setComponentByPosition(1, char.UTF8String("PSP_AI"))

    roles = univ.SequenceOf()
    roles.setComponentByPosition(0, role)

    psd2_qc = univ.Sequence()
    psd2_qc.setComponentByPosition(0, roles)
    psd2_qc.setComponentByPosition(1, char.UTF8String(nca_name))
    psd2_qc.setComponentByPosition(2, char.UTF8String(nca_id))

    # Wrap in qcStatement: SEQUENCE { statementId OID, statementInfo ANY }
    statement = univ.Sequence()
    statement.setComponentByPosition(0, univ.ObjectIdentifier(
        [int(x) for x in OID_PSD2_STATEMENT.split(".")]
    ))
    statement.setComponentByPosition(1, psd2_qc)

    # qcStatements ::= SEQUENCE OF QcStatement
    qc_statements = univ.SequenceOf()
    qc_statements.setComponentByPosition(0, statement)

    return der_encoder.encode(qc_statements)


def generate_psd2_cert(
    cn: str,
    org: str,
    country: str,
    serial_number: str,
    nca_name: str,
    nca_id: str,
    cert_path: str,
    key_path: str,
    days: int = 365,
):
    key: RSAPrivateKey = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
        x509.NameAttribute(NameOID.COUNTRY_NAME, country),
        x509.NameAttribute(NameOID.SERIAL_NUMBER, serial_number),
    ])

    now = datetime.datetime.utcnow()
    psd2_ext_der = build_psd2_qc_statement(nca_name, nca_id)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(cn),
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([
                x509.ExtendedKeyUsageOID.CLIENT_AUTH,
                x509.ExtendedKeyUsageOID.SERVER_AUTH,
            ]),
            critical=False,
        )
        .add_extension(
            x509.UnrecognizedExtension(
                oid=ObjectIdentifier(OID_QC_STATEMENTS),
                value=psd2_ext_der,
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256(), default_backend())
    )

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))

    print(f"Certificate written to: {cert_path}")
    print(f"Private key written to: {key_path}")


if __name__ == "__main__":
    generate_psd2_cert(
        cn="AK-Test-TPP",
        org="TestTPP",
        country="DE",
        serial_number="PSDDE-BAFIN-19337",
        nca_name="BaFin",
        nca_id="PSDDE-BAFIN-19337",
        cert_path="certs/cert.pem",
        key_path="certs/key.pem",
    )
