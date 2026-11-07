"""Server certificate and key files with one property varied at a time."""

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from tests.docker_engine.tls_material import KeyKind, cert_pem, key_of, key_pem

NOW = datetime(2030, 6, 1, tzinfo=UTC)
CERT_NAME = "server.pem"
KEY_NAME = "server.key"
DEFAULT_HOSTS = ("dockmin.example.test",)
DEFAULT_ADDRESSES = ("192.0.2.10",)
VALID_DAYS = 365
OTHER_KEY_VARIANT = 1


@dataclass(frozen=True, slots=True)
class ServerFiles:
    """Where a certificate and its key were written."""

    cert: Path
    key: Path


def _hash(key: Any) -> hashes.SHA256 | None:
    return None if isinstance(key, ed25519.Ed25519PrivateKey) else hashes.SHA256()


def build_certificate(
    *,
    key: Any,
    signer: Any | None = None,
    issuer: x509.Name | None = None,
    common_name: str = "dockmin.example.test",
    hosts: tuple[str, ...] = DEFAULT_HOSTS,
    addresses: tuple[str, ...] = DEFAULT_ADDRESSES,
    not_before: datetime = NOW - timedelta(days=1),
    not_after: datetime = NOW + timedelta(days=VALID_DAYS),
) -> x509.Certificate:
    """Build a certificate; self-signed unless a signer and issuer name are given."""
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    names: list[x509.GeneralName] = [x509.DNSName(host) for host in hosts]
    names += [x509.IPAddress(ipaddress.ip_address(item)) for item in addresses]
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer or subject)
        .public_key(key.public_key())
        .serial_number(0xABCDEF)
        .not_valid_before(not_before)
        .not_valid_after(not_after)
    )
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    signing_key = signer or key
    return builder.sign(signing_key, _hash(signing_key))


def write_server_files(
    directory: Path,
    *,
    kind: KeyKind = "p256",
    chain: int = 1,
    key_variant: int = 0,
    **options: Any,
) -> ServerFiles:
    """Write a leaf certificate (and `chain - 1` more), and its unencrypted key."""
    key = key_of(kind, key_variant)
    ca_key = key_of("p256", 7)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test issuing CA")])
    if chain > 1:
        options = {"signer": ca_key, "issuer": ca_name} | options
    leaf = build_certificate(key=key, **options)
    pem = cert_pem(leaf)
    for _ in range(chain - 1):
        pem += cert_pem(build_certificate(key=ca_key, common_name="test issuing CA"))
    cert_path, key_path = directory / CERT_NAME, directory / KEY_NAME
    cert_path.write_text(pem, encoding="ascii")
    key_path.write_text(key_pem(key), encoding="ascii")
    return ServerFiles(cert_path, key_path)
