"""Public verification keys read from a PEM file or a JWKS document."""

import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Final

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa

PERMITTED_ALGORITHMS: Final = (
    "ES256",
    "ES384",
    "ES512",
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "EdDSA",
)
MAX_KEYS: Final = 20
MAX_KEY_ENTRIES_READ: Final = 100
MAX_KID_CHARS: Final = 256

type PublicKey = (
    ec.EllipticCurvePublicKey | rsa.RSAPublicKey | ed25519.Ed25519PublicKey | ed448.Ed448PublicKey
)

_PUBLIC_KEY_TYPES: Final = (
    ec.EllipticCurvePublicKey,
    rsa.RSAPublicKey,
    ed25519.Ed25519PublicKey,
    ed448.Ed448PublicKey,
)
_CURVE_OF: Final = {"ES256": "secp256r1", "ES384": "secp384r1", "ES512": "secp521r1"}
_RSA_ALGORITHMS: Final = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512"})


class KeyDocumentError(ValueError):
    """A key document that cannot be read; the text is product-written and names no key material."""


@dataclass(frozen=True, slots=True)
class VerificationKey:
    """One public key, the `kid` it is published under, and the algorithm it declares."""

    kid: str | None
    key: PublicKey
    alg: str | None = None

    def fits(self, algorithm: str) -> bool:
        """Report whether a signature made with `algorithm` can be checked by this key."""
        if self.alg is not None and self.alg != algorithm:
            return False
        if algorithm in _CURVE_OF:
            return (
                isinstance(self.key, ec.EllipticCurvePublicKey)
                and self.key.curve.name == _CURVE_OF[algorithm]
            )
        if algorithm in _RSA_ALGORITHMS:
            return isinstance(self.key, rsa.RSAPublicKey)
        if algorithm == "EdDSA":
            return isinstance(self.key, ed25519.Ed25519PublicKey | ed448.Ed448PublicKey)
        return False


def parse_key_document(data: bytes, allowed: Collection[str]) -> list[VerificationKey]:
    """Return the usable keys of a PEM public key or a JWKS document; raise KeyDocumentError."""
    text = data.strip()
    if text.startswith(b"{"):
        return _from_jwks(text, allowed)
    return _from_pem(text)


def _from_pem(data: bytes) -> list[VerificationKey]:
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise KeyDocumentError("not a PEM public key or a JWKS document") from None
    if not isinstance(key, _PUBLIC_KEY_TYPES):
        raise KeyDocumentError("the key type cannot verify a permitted algorithm")
    return [VerificationKey(None, key)]


def _from_jwks(data: bytes, allowed: Collection[str]) -> list[VerificationKey]:
    try:
        document = json.loads(data)
    except ValueError:
        raise KeyDocumentError("the document is not valid JSON") from None
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise KeyDocumentError("the document has no keys list")
    found: list[VerificationKey] = []
    for entry in entries[:MAX_KEY_ENTRIES_READ]:
        key = _usable(entry, allowed)
        if key is not None:
            found.append(key)
        if len(found) == MAX_KEYS:
            break
    return found


def _usable(entry: Any, allowed: Collection[str]) -> VerificationKey | None:
    if not isinstance(entry, dict):
        return None
    use, declared, kid = entry.get("use"), entry.get("alg"), entry.get("kid")
    if use is not None and use != "sig":
        return None
    if declared is not None and declared not in allowed:
        return None
    if kid is not None and (not isinstance(kid, str) or len(kid) > MAX_KID_CHARS):
        return None
    try:
        key = jwt.PyJWK(entry).key
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, UnsupportedAlgorithm):
        return None
    if not isinstance(key, _PUBLIC_KEY_TYPES):
        return None
    return VerificationKey(kid, key, declared)
