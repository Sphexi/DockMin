"""Throwaway certificate authorities and leaf certificates, held in memory as PEM text."""

import ipaddress
import ssl
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

LOOPBACK = "127.0.0.1"

# Fixed validity bounds keep certificate checks independent of the wall clock.
_NOT_BEFORE = datetime(2020, 1, 1, tzinfo=UTC)
_NOT_AFTER = datetime(2100, 1, 1, tzinfo=UTC)

type PrivateKey = ec.EllipticCurvePrivateKey


@dataclass(frozen=True, slots=True)
class Identity:
    """A certificate and its unencrypted private key, both PEM."""

    cert_pem: str
    key_pem: str


@dataclass(frozen=True, slots=True)
class Pki:
    """A trusted CA with a server and client identity, and an unrelated CA with its own server."""

    ca_pem: str
    server: Identity
    client: Identity
    rogue_ca_pem: str
    rogue_server: Identity


def build_pki() -> Pki:
    """Generate the full set of authorities and identities."""
    ca_key, ca_cert = _authority("dockmin test CA")
    rogue_key, rogue_cert = _authority("dockmin rogue CA")
    return Pki(
        ca_pem=_cert_pem(ca_cert),
        server=_leaf(ca_key, ca_cert, "engine", server=True),
        client=_leaf(ca_key, ca_cert, "dockmin", server=False),
        rogue_ca_pem=_cert_pem(rogue_cert),
        rogue_server=_leaf(rogue_key, rogue_cert, "engine", server=True),
    )


def server_ssl_context(identity: Identity, client_ca_pem: str) -> ssl.SSLContext:
    """Build a server context that demands a client certificate signed by the given CA."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=client_ca_pem)
    # load_cert_chain takes paths only; the directory is gone before the function returns.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "identity.pem"
        path.write_text(identity.cert_pem + identity.key_pem, encoding="utf-8")
        context.load_cert_chain(path)
    return context


def key_fragments(key_pem: str) -> list[str]:
    """Return the base64 body lines of a PEM key, which must never appear in output."""
    return [line for line in key_pem.splitlines() if line and not line.startswith("-----")]


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _builder(
    subject: x509.Name, issuer: x509.Name, public_key: ec.EllipticCurvePublicKey
) -> x509.CertificateBuilder:
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOT_BEFORE)
        .not_valid_after(_NOT_AFTER)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    )


def _authority(common_name: str) -> tuple[PrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = _name(common_name)
    cert = (
        _builder(name, name, key.public_key())
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _leaf(
    ca_key: PrivateKey, ca_cert: x509.Certificate, common_name: str, *, server: bool
) -> Identity:
    key = ec.generate_private_key(ec.SECP256R1())
    usage = ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH
    builder = (
        _builder(_name(common_name), ca_cert.subject, key.public_key())
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
    )
    if server:
        san = x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(LOOPBACK))])
        builder = builder.add_extension(san, critical=False)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return Identity(_cert_pem(builder.sign(ca_key, hashes.SHA256())), key_pem)


def _cert_pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
