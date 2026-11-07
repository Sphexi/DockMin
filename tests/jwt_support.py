"""Keys, signed tokens and settings for the access platform sign-in tests."""

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.auth.jwt_identity import JwtConfig, JwtVerifier
from app.auth.jwt_key_documents import PublicKey, VerificationKey
from app.auth.jwt_keys import KeyStore
from app.config import ENV_PREFIX

ISSUER = "twingate"
AUDIENCE = "example-network"
HEADER = "X-Twingate-Jwt"
KID = "key-1"
OTHER_KID = "key-2"
EMAIL = "pat@example.test"
USERNAME = "pat"
MAX_AGE = 86400.0
FRESH_SECONDS = 5
LIFETIME_SECONDS = 3600
SKEW_SECONDS = 30
TWINGATE_TYPE = "GAT"
CLAIM_PATH = "user.email"


@dataclass(frozen=True, slots=True)
class Signer:
    """A signing key and the `kid` its tokens name."""

    private: Any
    kid: str | None = KID

    @property
    def public(self) -> PublicKey:
        public: PublicKey = self.private.public_key()
        return public

    def verification_key(self) -> VerificationKey:
        return VerificationKey(self.kid, self.public)

    def token(
        self,
        claims: Mapping[str, Any] | None = None,
        *,
        algorithm: str = "ES256",
        kid: str | None = KID,
        drop: tuple[str, ...] = (),
    ) -> str:
        payload = {**platform_claims(), **(claims or {})}
        for name in drop:
            payload.pop(name, None)
        headers: dict[str, Any] = {"typ": TWINGATE_TYPE}
        if kid is not None:
            headers["kid"] = kid
        return jwt.encode(payload, self.private, algorithm=algorithm, headers=headers)

    def jwk(self) -> dict[str, Any]:
        assert isinstance(self.public, ec.EllipticCurvePublicKey)
        entry: dict[str, Any] = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self.public))
        return {**entry, "kid": self.kid, "use": "sig", "alg": "ES256"}


def ec_signer(kid: str | None = KID, curve: ec.EllipticCurve | None = None) -> Signer:
    return Signer(ec.generate_private_key(curve or ec.SECP256R1()), kid)


def rsa_private() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def platform_claims(
    *,
    email: str = EMAIL,
    username: str = USERNAME,
    issued_ago: float = FRESH_SECONDS,
    lifetime: float = LIFETIME_SECONDS,
) -> dict[str, Any]:
    """Return claims shaped like the platform's: nested under `user`, no `sub`, no `nbf`."""
    now = int(time.time())
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now - int(issued_ago),
        "exp": now + int(lifetime),
        "jti": uuid.uuid4().hex,
        "user": {"id": "user-1", "username": username, "email": email},
    }


def verifier(
    *signers: Signer,
    algorithms: tuple[str, ...] = ("ES256",),
    identity_claim: str = CLAIM_PATH,
    username_claim: str = "user.username",
) -> JwtVerifier:
    config = JwtConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=algorithms,
        identity_claim=identity_claim,
        username_claim=username_claim,
        max_age=MAX_AGE,
    )
    return JwtVerifier(config, KeyStore(fixed=[s.verification_key() for s in signers]))


def _segment(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unsigned_token(algorithm: str, *, signature: str = "AAAA") -> str:
    """Return a token that claims `algorithm` and carries a signature nobody made."""
    return f"{_segment({'alg': algorithm, 'kid': KID})}.{_segment(platform_claims())}.{signature}"


def hmac_token(secret: bytes) -> str:
    """Return an HS256 token keyed with `secret`, as an attacker holding the public key would."""
    signing = f"{_segment({'alg': 'HS256', 'kid': KID})}.{_segment(platform_claims())}"
    digest = hmac.new(secret, signing.encode(), hashlib.sha256).digest()
    return f"{signing}.{base64.urlsafe_b64encode(digest).rstrip(b'=').decode()}"


def public_pem(signer: Signer) -> bytes:
    return signer.public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def write_public_pem(path: Path, signer: Signer) -> Path:
    path.write_bytes(public_pem(signer))
    return path


def write_jwks(path: Path, *signers: Signer) -> Path:
    path.write_text(json.dumps({"keys": [s.jwk() for s in signers]}), encoding="utf-8")
    return path


def jwks_body(*signers: Signer) -> bytes:
    return json.dumps({"keys": [s.jwk() for s in signers]}).encode()


def jwt_env(key_file: Path, **overrides: str) -> dict[str, str]:
    """Return the access platform settings for a Twingate-shaped token and a key file."""
    values = {
        "HEADER": HEADER,
        "ISSUER": ISSUER,
        "AUDIENCE": AUDIENCE,
        "KEY_FILE": str(key_file),
        "IDENTITY_CLAIM": CLAIM_PATH,
        "USERNAME_CLAIM": "user.username",
    }
    values.update({name.upper(): value for name, value in overrides.items()})
    return {f"{ENV_PREFIX}JWT_{name}": value for name, value in values.items()}
