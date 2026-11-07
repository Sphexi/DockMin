"""Validation of uploaded mutual-TLS material in app/docker_engine/tls_validation.py."""

from collections.abc import Callable
from typing import Any

import pytest

from app.docker_engine.tls_validation import (
    MAX_PEM_BYTES,
    TlsField,
    TlsValidation,
    describe_certificate,
    normalise_pem,
    validate_tls_material,
)
from tests.docker_engine.tls_material import (
    DAY,
    FAR_AHEAD,
    NOW,
    SERVER_AUTH,
    KeyKind,
    Material,
    cert_pem,
    key_of,
    key_pem,
    make_ca,
    make_leaf,
    valid_material,
)

PASSPHRASE = b"correct horse battery"
SOON = NOW + 10 * DAY


def validate(material: Material) -> TlsValidation:
    return validate_tls_material(material.ca, material.cert, material.key, now=NOW)


def leaf_variant(**options: Any) -> Material:
    ca_key = key_of("p256")
    ca = make_ca(ca_key)
    leaf_key = key_of("p256", 1)
    certificate = make_leaf(ca_key, ca, leaf_key, **options)
    return Material(cert_pem(ca), cert_pem(certificate), key_pem(leaf_key))


def ca_variant(**options: Any) -> Material:
    ca_key = key_of("p256")
    ca = make_ca(ca_key, **options)
    leaf_key = key_of("p256", 1)
    return Material(cert_pem(ca), cert_pem(make_leaf(ca_key, ca, leaf_key)), key_pem(leaf_key))


def key_variant(kind: KeyKind, *, passphrase: bytes | None = None) -> Material:
    ca_key = key_of("p256")
    ca = make_ca(ca_key)
    leaf_key = key_of(kind, 1)
    return Material(
        cert_pem(ca),
        cert_pem(make_leaf(ca_key, ca, leaf_key)),
        key_pem(leaf_key, passphrase=passphrase),
    )


def mismatched_key() -> Material:
    material = valid_material()
    return Material(material.ca, material.cert, key_pem(key_of("p256", 2)))


def swapped_into_wrong_fields(**fields: str) -> Material:
    material = valid_material()
    return Material(
        fields.get("ca", material.ca),
        fields.get("cert", material.cert),
        fields.get("key", material.key),
    )


def body_lines(*pems: str) -> list[str]:
    return [
        line
        for pem in pems
        for line in pem.splitlines()
        if line and not line.startswith("-----") and len(line) > 10
    ]


@pytest.mark.parametrize("kind", ["rsa", "p256", "p384", "ed25519"])
def test_material_with_a_supported_key_type_validates_without_warnings(kind: KeyKind) -> None:
    result = validate(valid_material(kind))

    assert result.ok
    assert result.warnings == ()


INVALID_CASES = {
    "key does not match certificate": (
        mismatched_key,
        TlsField.CLIENT_KEY,
        "does not match",
    ),
    "client certificate expired": (
        lambda: leaf_variant(not_after=NOW - DAY),
        TlsField.CLIENT_CERTIFICATE,
        "expired",
    ),
    "client certificate not yet valid": (
        lambda: leaf_variant(not_before=NOW + DAY),
        TlsField.CLIENT_CERTIFICATE,
        "not valid until",
    ),
    "CA expired": (
        lambda: ca_variant(not_after=NOW - DAY),
        TlsField.CA_CERTIFICATE,
        "expired",
    ),
    "CA not marked as authority": (
        lambda: ca_variant(is_ca=False),
        TlsField.CA_CERTIFICATE,
        "not marked as a certificate authority",
    ),
    "client certificate server-only usage": (
        lambda: leaf_variant(usage=SERVER_AUTH),
        TlsField.CLIENT_CERTIFICATE,
        "clientAuth",
    ),
    "encrypted key": (
        lambda: key_variant("p256", passphrase=PASSPHRASE),
        TlsField.CLIENT_KEY,
        "passphrase",
    ),
    "RSA key below 2048 bits": (
        lambda: key_variant("weak-rsa"),
        TlsField.CLIENT_KEY,
        "at least 2048",
    ),
    "unsupported curve": (
        lambda: key_variant("secp256k1"),
        TlsField.CLIENT_KEY,
        "P-256, P-384 or P-521",
    ),
    "key pasted into the certificate field": (
        lambda: swapped_into_wrong_fields(cert=valid_material().key),
        TlsField.CLIENT_CERTIFICATE,
        "contains a private key",
    ),
    "key pasted into the CA field": (
        lambda: swapped_into_wrong_fields(ca=valid_material().key),
        TlsField.CA_CERTIFICATE,
        "contains a private key",
    ),
    "certificate pasted into the key field": (
        lambda: swapped_into_wrong_fields(key=valid_material().cert),
        TlsField.CLIENT_KEY,
        "contains a certificate",
    ),
    "oversize key": (
        lambda: swapped_into_wrong_fields(key="A" * (MAX_PEM_BYTES + 1)),
        TlsField.CLIENT_KEY,
        "larger than 64 KiB",
    ),
    "empty certificate": (
        lambda: swapped_into_wrong_fields(cert=""),
        TlsField.CLIENT_CERTIFICATE,
        "Provide the client certificate",
    ),
    "text that is not PEM": (
        lambda: swapped_into_wrong_fields(ca="not a certificate"),
        TlsField.CA_CERTIFICATE,
        "not in PEM format",
    ),
}


@pytest.mark.parametrize(
    ("build", "target", "fragment"), INVALID_CASES.values(), ids=INVALID_CASES.keys()
)
def test_material_that_breaks_a_rule_is_refused_on_the_field_it_belongs_to(
    build: Callable[[], Material], target: TlsField, fragment: str
) -> None:
    result = validate(build())

    assert not result.ok
    assert fragment in result.errors[target]


@pytest.mark.parametrize(
    "build", [case[0] for case in INVALID_CASES.values()], ids=list(INVALID_CASES)
)
def test_no_refusal_message_quotes_any_line_of_the_uploaded_pem(
    build: Callable[[], Material],
) -> None:
    material = build()

    result = validate(material)

    messages = " ".join([*result.errors.values(), *result.warnings])
    assert not any(
        line in messages for line in body_lines(material.ca, material.cert, material.key)
    )


def test_a_client_certificate_not_issued_by_the_supplied_ca_validates_with_a_warning() -> None:
    rogue_key = key_of("p256", 5)
    rogue = make_ca(rogue_key, common_name="other CA")
    leaf_key = key_of("p256", 1)
    material = Material(
        valid_material().ca,
        cert_pem(make_leaf(rogue_key, rogue, leaf_key)),
        key_pem(leaf_key),
    )

    result = validate(material)

    assert result.ok
    assert "not issued by any supplied CA" in result.warnings[0]


def test_a_client_certificate_expiring_within_thirty_days_validates_with_a_warning() -> None:
    result = validate(leaf_variant(not_after=SOON))

    assert result.ok
    assert "within 30 days" in result.warnings[0]


def test_a_certificate_with_no_extended_key_usage_is_accepted() -> None:
    result = validate(leaf_variant(usage=None))

    assert result.ok


def test_normalising_pem_drops_everything_between_the_blocks() -> None:
    material = valid_material()
    noisy = f"leading text\n{material.cert}trailing key material\n{material.ca}"

    cleaned = normalise_pem(noisy)

    assert cleaned == material.cert + material.ca


def test_describing_a_certificate_reports_names_expiry_and_key_type() -> None:
    material = valid_material("p384")

    described = describe_certificate(material.cert, NOW)

    assert described is not None
    assert (described.leaf_subject, described.leaf_issuer) == ("client", "test CA")
    assert described.not_after == FAR_AHEAD
    assert described.key_type == "EC secp384r1"
    assert (described.expired, described.expires_soon) == (False, False)


def test_describing_text_that_is_not_a_certificate_reports_nothing() -> None:
    assert describe_certificate("not a certificate", NOW) is None
