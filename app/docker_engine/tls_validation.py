"""Validation of uploaded mutual-TLS material before it is stored."""

import base64
import binascii
import contextlib
import re
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.docker_engine.transport import TlsMaterial
from app.netcheck import clean_text as clean_text

MAX_PEM_BYTES = 64 * 1024
MAX_CA_CERTIFICATES = 10
MAX_CLIENT_CHAIN_CERTIFICATES = 5
MIN_RSA_BITS = 2048
EXPIRY_WARNING_WINDOW = timedelta(days=30)

_CERTIFICATE_LABEL = "CERTIFICATE"
_KEY_LABELS = frozenset({"PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY"})
_IGNORED_KEY_LABELS = frozenset({"EC PARAMETERS"})
_PEM_LABEL = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----")
_PEM_BLOCK = re.compile(
    r"-----BEGIN ([A-Z0-9 ]+)-----[ \t]*\r?\n([A-Za-z0-9+/=\s]*?)-----END \1-----"
)
_PEM_LINE_WIDTH = 64
_LEGACY_ENCRYPTION_HEADER = re.compile(r"^Proc-Type:.*ENCRYPTED", re.MULTILINE)
_EC_CURVES = {"secp256r1": "P-256", "secp384r1": "P-384", "secp521r1": "P-521"}
_INSPECTION_ERRORS = (ValueError, TypeError, UnsupportedAlgorithm, x509.DuplicateExtension)


type SupportedKey = ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey

_ENCRYPTED_KEY = (
    "The client key is passphrase-protected. Remove the passphrase and upload it again."
)


class TlsField(StrEnum):
    """The input a message belongs to; `MATERIAL` covers the three together."""

    CA_CERTIFICATE = "ca_certificate"
    CLIENT_CERTIFICATE = "client_certificate"
    CLIENT_KEY = "client_key"
    MATERIAL = "material"


@dataclass(frozen=True, slots=True)
class TlsMetadata:
    """Non-secret facts about a client certificate."""

    leaf_subject: str
    leaf_issuer: str
    not_after: datetime
    sha256_fingerprint: str
    key_type: str
    expired: bool
    expires_soon: bool


@dataclass(frozen=True, slots=True)
class TlsValidation:
    """Outcome of validating the three items; carries messages, never PEM text."""

    errors: Mapping[TlsField, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Report whether the material may be stored."""
        return not self.errors


def normalise_pem(text: str) -> str:
    """Re-emit only the well-formed PEM blocks of text, so nothing between blocks is kept.

    Oversized input is returned unchanged for the validator to refuse.
    """
    if len(text) > MAX_PEM_BYTES:
        return text
    blocks: list[str] = []
    for match in _PEM_BLOCK.finditer(text):
        label, body = match.group(1), "".join(match.group(2).split())
        try:
            base64.b64decode(body, validate=True)
        except binascii.Error:
            continue
        if not body:
            continue
        lines = textwrap.wrap(body, _PEM_LINE_WIDTH)
        blocks.append("\n".join([f"-----BEGIN {label}-----", *lines, f"-----END {label}-----"]))
    return "".join(f"{block}\n" for block in blocks)


def validate_tls_material(
    ca_certificate_pem: str,
    client_certificate_pem: str,
    client_key_pem: str,
    *,
    now: datetime | None = None,
) -> TlsValidation:
    """Validate the CA certificate, client certificate and client key together.

    Blocking: the final check writes and removes a short-lived file through the pinned TLS work
    directory, so async callers run this in a thread.
    """
    moment = now or datetime.now(UTC)
    errors: dict[TlsField, str] = {}
    warnings: list[str] = []

    cas = _load_certificates(
        ca_certificate_pem,
        TlsField.CA_CERTIFICATE,
        "CA certificate",
        MAX_CA_CERTIFICATES,
        errors,
    )
    chain = _load_certificates(
        client_certificate_pem,
        TlsField.CLIENT_CERTIFICATE,
        "client certificate",
        MAX_CLIENT_CHAIN_CERTIFICATES,
        errors,
    )
    accepted_key = _load_key(client_key_pem, errors)

    if cas is not None:
        _check_windows(cas, TlsField.CA_CERTIFICATE, moment, errors, warnings)
        _check_ca_flags(cas, errors)
    if chain is not None:
        _check_windows(chain, TlsField.CLIENT_CERTIFICATE, moment, errors, warnings)
        _check_client_usage(chain[0], errors)
    if chain is not None and accepted_key is not None:
        _check_pair(chain[0], accepted_key[0], errors)

    if errors or cas is None or chain is None or accepted_key is None:
        return TlsValidation(errors=errors, warnings=tuple(warnings))
    _warn_if_unissued(cas, chain, warnings)
    _check_context_loads(ca_certificate_pem, client_certificate_pem, client_key_pem, errors)
    if errors:
        return TlsValidation(errors=errors, warnings=tuple(warnings))
    return TlsValidation(warnings=tuple(warnings))


def _load_certificates(
    pem: str,
    target: TlsField,
    noun: str,
    maximum: int,
    errors: dict[TlsField, str],
) -> list[x509.Certificate] | None:
    labels = _PEM_LABEL.findall(pem)
    certificates: list[x509.Certificate] = []
    problem = _text_problem(pem, noun) or _certificate_label_problem(labels, noun)
    if problem is None:
        with contextlib.suppress(ValueError):
            certificates = x509.load_pem_x509_certificates(pem.encode("utf-8"))
        if not certificates or len(certificates) != len(labels):
            problem = f"The {noun} could not be read as a PEM certificate."
        elif len(certificates) > maximum:
            problem = f"The {noun} contains more than {maximum} certificates."
    if problem is not None:
        errors[target] = problem
        return None
    return certificates


def _text_problem(pem: str, noun: str) -> str | None:
    if not pem.strip():
        return f"Provide the {noun}."
    if len(pem) > MAX_PEM_BYTES or len(pem.encode("utf-8")) > MAX_PEM_BYTES:
        return f"The {noun} is larger than {MAX_PEM_BYTES // 1024} KiB."
    return None


def _certificate_label_problem(labels: list[str], noun: str) -> str | None:
    if not labels:
        return f"The {noun} is not in PEM format."
    if any(label.endswith("PRIVATE KEY") for label in labels):
        return f"The {noun} contains a private key. Provide only the certificate."
    if any(label != _CERTIFICATE_LABEL for label in labels):
        return f"The {noun} must contain only CERTIFICATE blocks."
    return None


def _load_key(pem: str, errors: dict[TlsField, str]) -> tuple[SupportedKey, str] | None:
    problem = _text_problem(pem, "client key") or _key_label_problem(_PEM_LABEL.findall(pem), pem)
    if problem is None:
        try:
            loaded = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        except TypeError:
            problem = _ENCRYPTED_KEY
        except (ValueError, UnsupportedAlgorithm):
            problem = "The client key could not be read as an unencrypted PEM private key."
        else:
            accepted = _key_policy(loaded)
            if not isinstance(accepted, str):
                return accepted
            problem = accepted
    errors[TlsField.CLIENT_KEY] = problem
    return None


def _key_label_problem(labels: list[str], pem: str) -> str | None:
    if "ENCRYPTED PRIVATE KEY" in labels or _LEGACY_ENCRYPTION_HEADER.search(pem):
        return _ENCRYPTED_KEY
    if _CERTIFICATE_LABEL in labels:
        return "The client key field contains a certificate. Provide only the private key."
    if not labels:
        return "The client key is not in PEM format."
    key_blocks = [label for label in labels if label in _KEY_LABELS]
    others = [label for label in labels if label not in _KEY_LABELS | _IGNORED_KEY_LABELS]
    if others or len(key_blocks) != 1:
        return "The client key must be a single PKCS#1, PKCS#8 or SEC1 PEM private key."
    return None


def _key_policy(key: PrivateKeyTypes) -> tuple[SupportedKey, str] | str:
    if isinstance(key, rsa.RSAPrivateKey):
        if key.key_size < MIN_RSA_BITS:
            return f"The RSA client key must be at least {MIN_RSA_BITS} bits."
        return key, f"RSA {key.key_size}"
    if isinstance(key, ec.EllipticCurvePrivateKey):
        curve = _EC_CURVES.get(key.curve.name)
        if curve is None:
            return "The elliptic-curve client key must use P-256, P-384 or P-521."
        return key, f"EC {curve}"
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return key, "Ed25519"
    return "The client key must be RSA, elliptic-curve (P-256, P-384, P-521) or Ed25519."


def _ordinal(noun: str, count: int, index: int) -> str:
    return f"The {noun}" if count == 1 else f"Certificate {index + 1} of the {noun}"


def _check_windows(
    certificates: list[x509.Certificate],
    target: TlsField,
    now: datetime,
    errors: dict[TlsField, str],
    warnings: list[str],
) -> None:
    noun = "CA certificate" if target is TlsField.CA_CERTIFICATE else "client certificate"
    for index, certificate in enumerate(certificates):
        label = _ordinal(noun, len(certificates), index)
        try:
            starts, ends = certificate.not_valid_before_utc, certificate.not_valid_after_utc
        except ValueError:
            errors.setdefault(target, f"{label} has an unreadable validity period.")
            continue
        if starts > now:
            errors.setdefault(target, f"{label} is not valid until {starts:%Y-%m-%d}.")
        elif ends < now:
            errors.setdefault(target, f"{label} expired on {ends:%Y-%m-%d}.")
        elif ends - now <= EXPIRY_WARNING_WINDOW:
            warnings.append(f"{label} expires on {ends:%Y-%m-%d}, within 30 days.")


def _check_ca_flags(certificates: list[x509.Certificate], errors: dict[TlsField, str]) -> None:
    for index, certificate in enumerate(certificates):
        try:
            constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
            is_ca = constraints.value.ca
        except x509.ExtensionNotFound:
            is_ca = False
        except _INSPECTION_ERRORS:
            errors.setdefault(
                TlsField.CA_CERTIFICATE,
                f"{_ordinal('CA certificate', len(certificates), index)} could not be inspected.",
            )
            continue
        if not is_ca:
            errors.setdefault(
                TlsField.CA_CERTIFICATE,
                f"{_ordinal('CA certificate', len(certificates), index)} is not marked as a "
                "certificate authority (Basic Constraints does not set CA).",
            )


def _check_client_usage(leaf: x509.Certificate, errors: dict[TlsField, str]) -> None:
    try:
        usage = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        return
    except _INSPECTION_ERRORS:
        errors.setdefault(
            TlsField.CLIENT_CERTIFICATE, "The client certificate could not be inspected."
        )
        return
    allowed = (
        ExtendedKeyUsageOID.CLIENT_AUTH in usage
        or ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE in usage
    )
    if not allowed:
        errors.setdefault(
            TlsField.CLIENT_CERTIFICATE,
            "The client certificate does not permit client authentication "
            "(Extended Key Usage lacks clientAuth); the daemon would refuse it.",
        )


def _check_pair(leaf: x509.Certificate, key: SupportedKey, errors: dict[TlsField, str]) -> None:
    spki = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    try:
        matches = leaf.public_key().public_bytes(*spki) == key.public_key().public_bytes(*spki)
    except _INSPECTION_ERRORS:
        errors.setdefault(
            TlsField.CLIENT_CERTIFICATE, "The client certificate has an unsupported public key."
        )
        return
    if not matches:
        errors.setdefault(
            TlsField.CLIENT_KEY, "The client key does not match the client certificate."
        )


def _warn_if_unissued(
    cas: list[x509.Certificate], chain: list[x509.Certificate], warnings: list[str]
) -> None:
    for certificate in chain:
        for authority in cas:
            try:
                certificate.verify_directly_issued_by(authority)
            except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm):
                continue
            return
    warnings.append(
        "The client certificate is not issued by any supplied CA certificate. The CA verifies "
        "the daemon's certificate; confirm the daemon trusts the client certificate's issuer."
    )


def _check_context_loads(
    ca_pem: str, certificate_pem: str, key_pem: str, errors: dict[TlsField, str]
) -> None:
    try:
        TlsMaterial(ca_pem, certificate_pem, key_pem).ssl_context()
    except ValueError:
        errors[TlsField.MATERIAL] = (
            "The connection layer could not load these three items together."
        )


def describe_certificate(pem: str, now: datetime) -> TlsMetadata | None:
    """Return the facts of the first certificate in pem, or None if it cannot be read."""
    try:
        leaf = x509.load_pem_x509_certificates(pem.encode("utf-8"))[0]
        ends = leaf.not_valid_after_utc.astimezone(UTC)
        public = leaf.public_key()
    except (ValueError, IndexError):
        return None
    if isinstance(public, rsa.RSAPublicKey):
        key_type = f"RSA {public.key_size}"
    elif isinstance(public, ec.EllipticCurvePublicKey):
        key_type = f"EC {public.curve.name}"
    elif isinstance(public, ed25519.Ed25519PublicKey):
        key_type = "Ed25519"
    else:
        key_type = "Unknown"
    return TlsMetadata(
        leaf_subject=_common_name(leaf.subject),
        leaf_issuer=_common_name(leaf.issuer),
        not_after=ends,
        sha256_fingerprint=leaf.fingerprint(hashes.SHA256()).hex(),
        key_type=key_type,
        expired=ends < now,
        expires_soon=now <= ends <= now + EXPIRY_WARNING_WINDOW,
    )


def _common_name(name: x509.Name) -> str:
    attributes = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not attributes:
        return "(no common name)"
    value = attributes[0].value
    return clean_text(value if isinstance(value, str) else value.decode("utf-8", "replace"))
