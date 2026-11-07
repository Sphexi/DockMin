"""Tests for app/crypto/envelope.py."""

from collections.abc import Callable

import pytest

from app.crypto.envelope import (
    MAX_KEY_VERSION,
    DecryptionError,
    SecretLocation,
    UnknownKeyVersionError,
    decrypt,
    encrypt,
    is_valid_instance_id,
    key_version_of,
    new_instance_id,
)
from tests.crypto.helpers import (
    FIRST_KEY,
    FIRST_ROW,
    FIRST_VERSION,
    INSTANCE_ONE,
    INSTANCE_TWO,
    PLAINTEXT,
    SECOND_KEY,
    SECOND_ROW,
    SECOND_VERSION,
    tamper,
)

TABLE = "registry_credential"
COLUMN = "secret_ciphertext"
LOCATION = SecretLocation(TABLE, COLUMN, FIRST_ROW)
KEYS = {FIRST_VERSION: FIRST_KEY}
GENERIC_FAILURE = "stored value could not be decrypted"
SEVEN = 7

TRANSPLANT_TARGETS = {
    "another row": SecretLocation(TABLE, COLUMN, SECOND_ROW),
    "another column in the same row": SecretLocation(TABLE, "derived_secret_ciphertext", FIRST_ROW),
    "another table at the same row id": SecretLocation("object_storage_target", COLUMN, FIRST_ROW),
}


def _sealed() -> str:
    return encrypt(PLAINTEXT, FIRST_KEY, FIRST_VERSION, LOCATION, INSTANCE_ONE)


@pytest.mark.parametrize("plaintext", [PLAINTEXT, "", "pässwörd"])
def test_decrypt_at_the_sealed_location_returns_the_plaintext(plaintext: str) -> None:
    token = encrypt(plaintext, FIRST_KEY, FIRST_VERSION, LOCATION, INSTANCE_ONE)

    opened = decrypt(token, KEYS, LOCATION, INSTANCE_ONE)

    assert opened == plaintext


def test_encrypt_serialises_the_version_a_nonce_and_a_ciphertext_that_hides_the_plaintext() -> None:
    token = encrypt(PLAINTEXT, FIRST_KEY, SEVEN, LOCATION, INSTANCE_ONE)

    version, nonce, body = token.split(":")

    assert (version, key_version_of(token)) == (f"v{SEVEN}", SEVEN)
    assert nonce
    assert PLAINTEXT not in body


def test_encrypt_twice_with_identical_input_yields_different_tokens() -> None:
    first = _sealed()
    second = _sealed()

    assert first != second


@pytest.mark.parametrize("target", TRANSPLANT_TARGETS.values(), ids=TRANSPLANT_TARGETS.keys())
def test_decrypt_of_a_ciphertext_moved_to_another_location_fails(target: SecretLocation) -> None:
    token = _sealed()

    with pytest.raises(DecryptionError):
        decrypt(token, KEYS, target, INSTANCE_ONE)


def test_decrypt_under_a_different_instance_id_fails_at_the_same_location() -> None:
    token = _sealed()

    with pytest.raises(DecryptionError):
        decrypt(token, KEYS, LOCATION, INSTANCE_TWO)


def _wrong_key(token: str) -> tuple[str, dict[int, bytes], SecretLocation]:
    return token, {FIRST_VERSION: SECOND_KEY}, LOCATION


def _wrong_location(token: str) -> tuple[str, dict[int, bytes], SecretLocation]:
    return token, KEYS, TRANSPLANT_TARGETS["another row"]


def _corrupt_body(token: str) -> tuple[str, dict[int, bytes], SecretLocation]:
    return tamper(token), KEYS, LOCATION


@pytest.mark.parametrize("break_it", [_wrong_key, _wrong_location, _corrupt_body])
def test_decrypt_failure_is_one_message_with_no_chained_cause(
    break_it: Callable[[str], tuple[str, dict[int, bytes], SecretLocation]],
) -> None:
    token, keys, location = break_it(_sealed())

    with pytest.raises(DecryptionError) as raised:
        decrypt(token, keys, location, INSTANCE_ONE)

    assert str(raised.value) == GENERIC_FAILURE
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.parametrize(
    "token",
    [
        "",
        "plain-text",
        "v0:AAAA:BBBB",
        "v1:AAAA",
        "v1:AAAA:BBBB:CCCC",
        "v1:!!!!:????",
        "v01:AAAAAAAAAAAAAAAA:BBBB",
        "v1234567890:AAAAAAAAAAAAAAAA:BBBB",
        "v1:AAAA:BBBB",
    ],
)
def test_decrypt_of_a_malformed_token_raises_decryption_error(token: str) -> None:
    with pytest.raises(DecryptionError):
        decrypt(token, KEYS, LOCATION, INSTANCE_ONE)


def test_decrypt_of_a_version_with_no_key_raises_an_error_naming_that_version() -> None:
    token = encrypt(PLAINTEXT, SECOND_KEY, SECOND_VERSION, LOCATION, INSTANCE_ONE)

    with pytest.raises(UnknownKeyVersionError) as raised:
        decrypt(token, KEYS, LOCATION, INSTANCE_ONE)

    assert raised.value.version == SECOND_VERSION
    assert f"key version {SECOND_VERSION}" in str(raised.value)


@pytest.mark.parametrize(
    ("table", "column", "row_id"),
    [
        ("Registry", COLUMN, FIRST_ROW),
        (TABLE, "secret;drop", FIRST_ROW),
        ("", COLUMN, FIRST_ROW),
        (TABLE, "col\0umn", FIRST_ROW),
        (TABLE, COLUMN, 0),
    ],
)
def test_secret_location_rejects_names_and_ids_that_could_blur_the_authenticated_data(
    table: str, column: str, row_id: int
) -> None:
    with pytest.raises(ValueError, match=r"identifiers|row_id"):
        SecretLocation(table, column, row_id)


@pytest.mark.parametrize("instance_id", ["", "A" * 32, "a" * 31, "g" * 32, "a" * 32 + "\0"])
def test_encrypt_rejects_a_malformed_instance_id(instance_id: str) -> None:
    with pytest.raises(ValueError, match="instance_id"):
        encrypt(PLAINTEXT, FIRST_KEY, FIRST_VERSION, LOCATION, instance_id)


@pytest.mark.parametrize(
    ("key", "version"),
    [(b"short", FIRST_VERSION), (FIRST_KEY, 0), (FIRST_KEY, MAX_KEY_VERSION + 1)],
)
def test_encrypt_rejects_a_wrong_length_key_and_an_out_of_range_version(
    key: bytes, version: int
) -> None:
    with pytest.raises(ValueError, match="key"):
        encrypt(PLAINTEXT, key, version, LOCATION, INSTANCE_ONE)


def test_new_instance_id_is_valid_and_not_repeated() -> None:
    first = new_instance_id()
    second = new_instance_id()

    assert is_valid_instance_id(first)
    assert first != second
