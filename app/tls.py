"""Server certificate reading, the HTTPS listener's TLS context and mark, and `served_over_tls`."""

import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
from starlette.requests import HTTPConnection

EXPIRY_WARNING: Final = timedelta(days=30)
MAX_ALTERNATIVE_NAMES: Final = 256
KEY_MISMATCH_MARKER: Final = "KEY_VALUES_MISMATCH"
HTTP_ALPN: Final = "http/1.1"
TLS12_CIPHERS: Final = "ECDHE+AESGCM:ECDHE+CHACHA20"
TLS_LISTENER_SCOPE_KEY: Final = "dockmin.tls_listener"
TLS_SELF_SIGNED_SCOPE_KEY: Final = "dockmin.tls_self_signed"

type ExpiryState = Literal["valid", "expiring", "expired", "not_yet_valid"]


class CertificateUnusable(Exception):
    """The certificate or key cannot be served; the message is product text with no file content."""


@dataclass(frozen=True, slots=True)
class CertificateSummary:
    """The leaf certificate of a PEM file, reduced to what an operator inspects."""

    subject: str
    issuer: str
    serial_hex: str
    sha256_fingerprint: str
    subject_alt_names: tuple[str, ...]
    key_type: str
    key_bits: int | None
    not_before: datetime
    not_after: datetime
    chain_length: int
    self_signed: bool


def read_certificate(cert_file: Path) -> CertificateSummary:
    """Summarise the first certificate in a PEM file; no key material is read."""
    try:
        data = cert_file.read_bytes()
    except OSError as error:
        raise CertificateUnusable(
            f"The certificate file {cert_file} cannot be read: {error.strerror or 'I/O error'}"
        ) from None
    try:
        chain = x509.load_pem_x509_certificates(data)
    except ValueError:
        raise CertificateUnusable(
            f"The certificate file {cert_file} holds no PEM certificate"
        ) from None
    leaf = chain[0]
    try:
        key_type, key_bits = _describe_public_key(leaf)
        return CertificateSummary(
            subject=leaf.subject.rfc4514_string(),
            issuer=leaf.issuer.rfc4514_string(),
            serial_hex=format(leaf.serial_number, "X"),
            sha256_fingerprint=leaf.fingerprint(hashes.SHA256()).hex(":").upper(),
            subject_alt_names=_alternative_names(leaf),
            key_type=key_type,
            key_bits=key_bits,
            not_before=leaf.not_valid_before_utc,
            not_after=leaf.not_valid_after_utc,
            chain_length=len(chain),
            self_signed=_is_self_signed(leaf),
        )
    except (ValueError, x509.DuplicateExtension, x509.UnsupportedGeneralNameType):
        raise CertificateUnusable(
            f"The certificate in {cert_file} has fields that cannot be read"
        ) from None


def load_server_tls(cert_file: Path, key_file: Path) -> tuple[ssl.SSLContext, CertificateSummary]:
    """Build the TLS 1.2+ server context for a certificate and its unencrypted private key."""
    summary = read_certificate(cert_file)
    try:
        key_file.open("rb").close()
    except OSError as error:
        raise CertificateUnusable(
            f"The private key file {key_file} cannot be read: {error.strerror or 'I/O error'}"
        ) from None
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_NONE
    context.set_ciphers(TLS12_CIPHERS)
    context.set_alpn_protocols([HTTP_ALPN])
    try:
        context.load_cert_chain(cert_file, key_file, password=_refuse_passphrase)
    except ssl.SSLError as error:
        if KEY_MISMATCH_MARKER in str(error):
            raise CertificateUnusable(
                f"The private key in {key_file} does not match the first certificate in {cert_file}"
            ) from None
        raise CertificateUnusable(
            f"The private key in {key_file} is not an unencrypted PEM key that can be loaded"
        ) from None
    except OSError as error:
        raise CertificateUnusable(
            f"The certificate or key file cannot be read: {error.strerror or 'I/O error'}"
        ) from None
    return context, summary


def expiry_state(summary: CertificateSummary, now: datetime) -> ExpiryState:
    """Classify the validity window against a timezone-aware instant."""
    if now < summary.not_before:
        return "not_yet_valid"
    if now > summary.not_after:
        return "expired"
    if summary.not_after - now <= EXPIRY_WARNING:
        return "expiring"
    return "valid"


def _refuse_passphrase() -> bytes:
    # Without a callback OpenSSL prompts on the controlling terminal for an encrypted key.
    return b""


def _describe_public_key(certificate: x509.Certificate) -> tuple[str, int | None]:
    try:
        key = certificate.public_key()
    except (ValueError, UnsupportedAlgorithm):
        return "unknown", None
    if isinstance(key, rsa.RSAPublicKey):
        return "RSA", key.key_size
    if isinstance(key, ec.EllipticCurvePublicKey):
        return f"EC {key.curve.name}", key.key_size
    if isinstance(key, dsa.DSAPublicKey):
        return "DSA", key.key_size
    if isinstance(key, ed25519.Ed25519PublicKey):
        return "Ed25519", None
    if isinstance(key, ed448.Ed448PublicKey):
        return "Ed448", None
    return "unknown", None


def _alternative_names(certificate: x509.Certificate) -> tuple[str, ...]:
    try:
        extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except (x509.ExtensionNotFound, ValueError):
        return ()
    names: list[str] = []
    for name in extension.value:
        match name:
            case x509.DNSName():
                names.append(f"DNS:{name.value}")
            case x509.IPAddress():
                names.append(f"IP:{name.value}")
            case x509.RFC822Name():
                names.append(f"email:{name.value}")
            case x509.UniformResourceIdentifier():
                names.append(f"URI:{name.value}")
            case _:
                names.append(type(name).__name__)
        if len(names) >= MAX_ALTERNATIVE_NAMES:
            break
    return tuple(names)


def _is_self_signed(certificate: x509.Certificate) -> bool:
    if certificate.subject != certificate.issuer:
        return False
    try:
        certificate.verify_directly_issued_by(certificate)
    except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm):
        return False
    return True


def served_over_tls(connection: HTTPConnection) -> bool:
    """Report whether the request arrived on the HTTPS listener this process serves itself.

    The listener sets the mark; a request header, including a forwarded protocol, is chosen by
    the client and cannot.
    """
    return connection.scope.get(TLS_LISTENER_SCOPE_KEY) is True
