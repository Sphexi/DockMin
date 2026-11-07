"""Verification of a platform token: what is accepted, and why each other token is refused."""

import base64
import time
from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.auth.jwt_identity import JwtConfig, JwtRefused, JwtVerifier
from app.auth.jwt_keys import KeyStore
from tests.jwt_support import (
    AUDIENCE,
    EMAIL,
    ISSUER,
    KID,
    LIFETIME_SECONDS,
    MAX_AGE,
    OTHER_KID,
    SKEW_SECONDS,
    TWINGATE_TYPE,
    USERNAME,
    Signer,
    ec_signer,
    hmac_token,
    public_pem,
    rsa_private,
    unsigned_token,
    verifier,
)

PAST = 3 * SKEW_SECONDS
OVER_LONG_HEADER = "x" * 9000
OVER_LONG_JOSE = base64.urlsafe_b64encode(b'{"alg":"ES256","pad":"' + b"a" * 1100 + b'"}').decode()
FIRST_SEGMENT = "eyJhbGciOiJFUzI1NiJ9"


@pytest.fixture
def signer() -> Signer:
    return ec_signer()


def _claims(**changes: Any) -> dict[str, Any]:
    now = int(time.time())
    return {"iat": now - 5, "exp": now + LIFETIME_SECONDS} | changes


def _case(
    build: Callable[[Signer], str],
    reason: str,
    *,
    algorithms: tuple[str, ...] = ("ES256",),
    extra_key: bool = False,
) -> tuple[Callable[[Signer], str], str, tuple[str, ...], bool]:
    return build, reason, algorithms, extra_key


NOW = int(time.time())
REFUSALS = {
    "alg none": _case(lambda s: unsigned_token("none"), "alg_not_allowed"),
    "alg None in another case": _case(lambda s: unsigned_token("None"), "alg_not_allowed"),
    "alg none without a signature": _case(
        lambda s: unsigned_token("none", signature=""), "malformed"
    ),
    "HS256 keyed with the public key": _case(
        lambda s: hmac_token(public_pem(s)), "alg_not_allowed"
    ),
    "HS256 listed by the operator": _case(
        lambda s: hmac_token(public_pem(s)), "alg_not_allowed", algorithms=("HS256", "ES256")
    ),
    "algorithm outside the allow-list": _case(
        lambda s: ec_signer(curve=ec.SECP384R1()).token(algorithm="ES384"), "alg_not_allowed"
    ),
    "key type that does not match the algorithm": _case(
        lambda s: _rsa_token(), "key_type_mismatch", algorithms=("ES256", "RS256")
    ),
    "signed by another key": _case(lambda s: ec_signer().token(), "bad_signature"),
    "wrong issuer": _case(lambda s: s.token({"iss": "elsewhere"}), "bad_issuer"),
    "wrong audience": _case(lambda s: s.token({"aud": "another-network"}), "bad_audience"),
    "missing issuer": _case(lambda s: s.token(drop=("iss",)), "missing_claim"),
    "expired beyond the skew": _case(lambda s: s.token(_claims(exp=NOW - PAST)), "expired"),
    "not yet valid": _case(lambda s: s.token(_claims(nbf=NOW + PAST)), "not_yet_valid"),
    "issued longer ago than the maximum age": _case(
        lambda s: s.token(_claims(iat=NOW - int(MAX_AGE) - 60)), "too_old"
    ),
    "issued in the future beyond the skew": _case(
        lambda s: s.token(_claims(iat=NOW + PAST)), "iat_future"
    ),
    "no issue time": _case(lambda s: s.token(drop=("iat",)), "missing_claim"),
    "no expiry": _case(lambda s: s.token(drop=("exp",)), "missing_claim"),
    "no key id with several keys": _case(
        lambda s: s.token(kid=None), "kid_required", extra_key=True
    ),
    "unknown key id": _case(lambda s: s.token(kid="not-published"), "unknown_kid"),
    "identity claim absent": _case(
        lambda s: s.token({"user": {"username": USERNAME}}), "identity_missing"
    ),
    "identity claim not a string": _case(
        lambda s: s.token({"user": {"email": 7}}), "identity_invalid"
    ),
    "identity claim with a control character": _case(
        lambda s: s.token({"user": {"email": "a\nb@example.test"}}), "identity_invalid"
    ),
    "identity claim empty": _case(lambda s: s.token({"user": {"email": ""}}), "identity_invalid"),
    "identity claim over 320 characters": _case(
        lambda s: s.token({"user": {"email": "a" * 321}}), "identity_invalid"
    ),
    "header that is not base64": _case(lambda s: "!!!.e30.AAAA", "malformed"),
    "two segments": _case(lambda s: f"{FIRST_SEGMENT}.e30", "malformed"),
    "four segments": _case(lambda s: f"{FIRST_SEGMENT}.e30.AAAA.AAAA", "malformed"),
    "header that is not an object": _case(lambda s: "W10.e30.AAAA", "malformed"),
    "header without alg": _case(lambda s: "e30.e30.AAAA", "malformed"),
    "over-long JOSE header": _case(lambda s: f"{OVER_LONG_JOSE}.e30.AAAA", "malformed"),
    "over-long header value": _case(lambda s: OVER_LONG_HEADER, "too_long"),
    "empty value": _case(lambda s: "", "malformed"),
}


def _rsa_token() -> str:
    return Signer(rsa_private(), KID).token(algorithm="RS256")


def _store(signer: Signer, algorithms: tuple[str, ...], *, extra_key: bool) -> JwtVerifier:
    other = [ec_signer(OTHER_KID)] if extra_key else []
    keys = [signer.verification_key(), *(s.verification_key() for s in other)]
    config = JwtConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=algorithms,
        identity_claim="user.email",
        username_claim="user.username",
        max_age=MAX_AGE,
    )
    return JwtVerifier(config, KeyStore(fixed=keys))


@pytest.mark.parametrize("case", REFUSALS.values(), ids=REFUSALS.keys())
async def test_a_token_that_breaks_a_rule_is_refused_with_that_reason(
    signer: Signer, case: tuple[Callable[[Signer], str], str, tuple[str, ...], bool]
) -> None:
    build, reason, algorithms, extra_key = case
    verifying = _store(signer, algorithms, extra_key=extra_key)

    with pytest.raises(JwtRefused) as refused:
        await verifying.verify(build(signer))

    assert refused.value.reason == reason


async def test_a_key_on_another_curve_than_the_algorithm_names_is_refused() -> None:
    published = ec_signer(curve=ec.SECP384R1())
    signing = ec_signer()

    with pytest.raises(JwtRefused) as refused:
        await verifier(published).verify(signing.token())

    assert refused.value.reason == "key_type_mismatch"


async def test_a_token_with_the_configured_issuer_audience_and_fresh_times_is_accepted(
    signer: Signer,
) -> None:
    assertion = await verifier(signer).verify(signer.token())

    assert (assertion.identity, assertion.username) == (EMAIL, USERNAME)


async def test_a_twingate_shaped_token_verifies_with_the_identity_under_user(
    signer: Signer,
) -> None:
    token = signer.token(drop=("nbf", "sub"))

    assertion = await verifier(signer, identity_claim="user.email").verify(token)

    assert assertion.identity == EMAIL
    assert TWINGATE_TYPE in base64.urlsafe_b64decode(token.split(".")[0] + "==").decode()


async def test_the_bearer_prefix_is_stripped_in_any_case(signer: Signer) -> None:
    assertion = await verifier(signer).verify(f"bEaReR {signer.token()}")

    assert assertion.identity == EMAIL


async def test_a_token_just_inside_the_skew_is_accepted(signer: Signer) -> None:
    now = int(time.time())
    token = signer.token(_claims(exp=now - SKEW_SECONDS + 10, iat=now + SKEW_SECONDS - 10))

    assertion = await verifier(signer).verify(token)

    assert assertion.identity == EMAIL


async def test_a_single_key_may_omit_the_key_id_and_a_pem_key_accepts_any(signer: Signer) -> None:
    keyless = Signer(signer.private, None)

    assertion = await verifier(keyless).verify(signer.token(kid=None))

    assert assertion.identity == EMAIL


async def test_a_second_key_is_chosen_by_its_key_id(signer: Signer) -> None:
    second = ec_signer(OTHER_KID)

    assertion = await verifier(signer, second).verify(second.token(kid=OTHER_KID))

    assert assertion.identity == EMAIL


async def test_a_verified_token_is_not_checked_again_until_it_expires(signer: Signer) -> None:
    now = [time.time()]
    config = JwtConfig(ISSUER, AUDIENCE, ("ES256",), "user.email", "user.username", MAX_AGE)
    selections: list[str | None] = []

    class CountingStore(KeyStore):
        async def select(self, kid: str | None):  # type: ignore[no-untyped-def]
            selections.append(kid)
            return await super().select(kid)

    verifying = JwtVerifier(
        config, CountingStore(fixed=[signer.verification_key()]), clock=lambda: now[0]
    )
    token = signer.token(_claims(iat=int(now[0]) - 5, exp=int(now[0]) + 3600))

    await verifying.verify(token)
    await verifying.verify(token)
    now[0] += 301
    await verifying.verify(token)

    assert selections == [KID, KID]
