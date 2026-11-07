"""Builders for client TLS material with one deliberate property varied at a time."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any, Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NOW = datetime(2030, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)
LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)
FAR_AHEAD = datetime(2100, 1, 1, tzinfo=UTC)

type KeyKind = Literal["rsa", "weak-rsa", "p256", "p384", "ed25519", "secp256k1"]
type Usage = x509.ObjectIdentifier | None
CLIENT_AUTH = ExtendedKeyUsageOID.CLIENT_AUTH
SERVER_AUTH = ExtendedKeyUsageOID.SERVER_AUTH


@dataclass(frozen=True, slots=True)
class Material:
    """The three PEM items an operator uploads."""

    ca: str
    cert: str
    key: str


@cache
def key_of(kind: KeyKind, variant: int = 0) -> Any:
    """Return a private key; one per (kind, variant) for the whole run."""
    match kind:
        case "rsa":
            return rsa.generate_private_key(65537, 2048)
        case "weak-rsa":
            return rsa.generate_private_key(65537, 1024)
        case "p256":
            return ec.generate_private_key(ec.SECP256R1())
        case "p384":
            return ec.generate_private_key(ec.SECP384R1())
        case "secp256k1":
            return ec.generate_private_key(ec.SECP256K1())
        case "ed25519":
            return ed25519.Ed25519PrivateKey.generate()


def key_pem(key: Any, *, passphrase: bytes | None = None) -> str:
    """Encode a key as PKCS#8, encrypted when a passphrase is given."""
    encryption: serialization.KeySerializationEncryption = (
        serialization.NoEncryption()
        if passphrase is None
        else serialization.BestAvailableEncryption(passphrase)
    )
    pem: str = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    ).decode("ascii")
    return pem


def cert_pem(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _signer_hash(key: Any) -> hashes.SHA256 | None:
    return None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make_ca(
    key: Any,
    *,
    common_name: str = "test CA",
    is_ca: bool = True,
    not_before: datetime = LONG_AGO,
    not_after: datetime = FAR_AHEAD,
) -> x509.Certificate:
    """Build a self-signed CA certificate."""
    name = _name(common_name)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
        .sign(key, _signer_hash(key))
    )


def make_leaf(
    issuer_key: Any,
    issuer: x509.Certificate,
    key: Any,
    *,
    usage: Usage = CLIENT_AUTH,
    not_before: datetime = LONG_AGO,
    not_after: datetime = FAR_AHEAD,
) -> x509.Certificate:
    """Build a leaf certificate for key, signed by issuer_key; usage None omits the extension."""
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name("client"))
        .issuer_name(issuer.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    if usage is not None:
        builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
    return builder.sign(issuer_key, _signer_hash(issuer_key))


def valid_material(kind: KeyKind = "p256") -> Material:
    """Return material every rule accepts, with the leaf key of the given kind."""
    ca_key = key_of("p256")
    ca = make_ca(ca_key)
    leaf_key = key_of(kind)
    return Material(
        ca=cert_pem(ca), cert=cert_pem(make_leaf(ca_key, ca, leaf_key)), key=key_pem(leaf_key)
    )
