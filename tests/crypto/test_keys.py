"""Tests for app/crypto/keys.py."""

import base64

import pytest

from app.config import SECRET_KEY_VAR, ConfigError
from app.crypto.envelope import SecretLocation
from app.crypto.keys import (
    FINGERPRINT_HEX_CHARS,
    Keyring,
    KeyScanner,
    MasterKeys,
    key_fingerprint,
    key_forms,
    load_master_keys,
    manifest_mac,
)
from app.crypto.vault import Vault
from tests.crypto.helpers import (
    FIRST_KEY,
    FIRST_VERSION,
    INSTANCE_ONE,
    PLUS_AND_SLASH_KEY,
    SECOND_KEY,
    SECOND_VERSION,
    THIRD_KEY,
    vault_with,
)
from tests.support import KEY_MATERIAL

SHORT_KEY = b"too-short"


def test_load_master_keys_decodes_the_configured_key_and_has_no_previous(
    env: dict[str, str],
) -> None:
    keys = load_master_keys(env)

    assert keys.current == KEY_MATERIAL
    assert keys.previous is None


def test_key_fingerprint_is_stable_per_key_and_differs_between_keys() -> None:
    first = key_fingerprint(FIRST_KEY)
    again = key_fingerprint(FIRST_KEY)
    second = key_fingerprint(SECOND_KEY)

    assert first == again
    assert first != second


def test_key_fingerprint_has_the_declared_length_and_contains_no_part_of_the_key() -> None:
    fingerprint = key_fingerprint(FIRST_KEY)

    assert len(fingerprint) == FINGERPRINT_HEX_CHARS
    assert fingerprint not in FIRST_KEY.hex()


def test_key_forms_covers_every_text_encoding_a_key_could_be_logged_in() -> None:
    forms = key_forms(PLUS_AND_SLASH_KEY)

    standard = base64.b64encode(PLUS_AND_SLASH_KEY).decode("ascii")
    url_safe = base64.urlsafe_b64encode(PLUS_AND_SLASH_KEY).decode("ascii")
    assert set(forms) == {
        standard,
        standard.rstrip("="),
        url_safe,
        url_safe.rstrip("="),
        PLUS_AND_SLASH_KEY.hex(),
        PLUS_AND_SLASH_KEY.hex().upper(),
    }
    assert len(forms) == len(set(forms))


@pytest.mark.parametrize(
    ("current", "previous"),
    [(SHORT_KEY, None), (FIRST_KEY, SHORT_KEY)],
    ids=["current", "previous"],
)
def test_master_keys_rejects_a_key_of_the_wrong_length(
    current: bytes, previous: bytes | None
) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        MasterKeys(current, previous)


def test_keyring_without_the_current_versions_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="current version"):
        Keyring(SECOND_VERSION, {FIRST_VERSION: FIRST_KEY})


def test_keyring_current_key_is_the_one_for_the_current_version() -> None:
    keyring = Keyring(SECOND_VERSION, {FIRST_VERSION: FIRST_KEY, SECOND_VERSION: SECOND_KEY})

    assert keyring.current_key == SECOND_KEY


def test_loading_keys_with_no_key_set_names_the_variable() -> None:
    with pytest.raises(ConfigError, match=SECRET_KEY_VAR):
        load_master_keys({})


PAYLOAD = b'{"format":"dockmin-backup"}'
LEADING = b"x" * 5000
TRAILING = b"y"
LOCATION = SecretLocation("registry_credential", "secret_ciphertext", 1)


def spellings(key: bytes) -> list[bytes]:
    texts = [*key_forms(key), key.hex().upper(), *(form.rstrip("=") for form in key_forms(key))]
    return [key, *(text.encode("ascii") for text in texts)]


def test_the_manifest_mac_is_hex_and_depends_on_both_the_key_and_the_payload() -> None:
    mac = manifest_mac(FIRST_KEY, PAYLOAD)

    assert len(mac) == 64 and set(mac) <= set("0123456789abcdef")
    assert mac == manifest_mac(FIRST_KEY, PAYLOAD)
    assert mac != manifest_mac(SECOND_KEY, PAYLOAD)
    assert mac != manifest_mac(FIRST_KEY, PAYLOAD + b" ")


def test_the_manifest_mac_is_not_a_plain_hash_of_the_key_or_the_payload() -> None:
    mac = manifest_mac(FIRST_KEY, PAYLOAD)

    assert key_fingerprint(FIRST_KEY) not in mac
    assert FIRST_KEY.hex() not in mac


@pytest.mark.parametrize("offset", [1, 20, 42])
def test_the_scanner_finds_a_key_spelled_in_base64_across_two_chunks(offset: int) -> None:
    stream = LEADING + base64.b64encode(SECOND_KEY) + TRAILING
    split = len(LEADING) + offset
    scanner = KeyScanner([SECOND_KEY])

    scanner.feed(stream[:split])
    assert not scanner.found
    scanner.feed(stream[split:])

    assert scanner.found


@pytest.mark.parametrize("spelling", spellings(FIRST_KEY), ids=range(len(spellings(FIRST_KEY))))
def test_the_scanner_finds_each_spelling_of_each_key_it_was_given(spelling: bytes) -> None:
    scanner = KeyScanner([SECOND_KEY, FIRST_KEY])

    scanner.feed(b"nothing here")
    scanner.feed(spelling)

    assert scanner.found


def test_the_scanner_stays_quiet_about_a_key_it_was_not_given() -> None:
    scanner = KeyScanner([FIRST_KEY])

    for spelling in spellings(THIRD_KEY):
        scanner.feed(spelling)

    assert not scanner.found


def test_the_scanner_never_reports_its_keys_in_a_repr() -> None:
    scanner = KeyScanner([FIRST_KEY])

    assert "redacted" in repr(scanner)
    assert not any(form in repr(scanner) for form in key_forms(FIRST_KEY))


def test_the_scanner_refuses_an_empty_key() -> None:
    with pytest.raises(ValueError, match="empty"):
        KeyScanner([b""])


def test_the_vault_reports_the_fingerprint_and_manifest_mac_of_its_current_key() -> None:
    vault = vault_with(FIRST_KEY)

    assert vault.key_fingerprint() == key_fingerprint(FIRST_KEY)
    assert vault.manifest_mac(PAYLOAD) == manifest_mac(FIRST_KEY, PAYLOAD)


def test_the_vault_scanner_covers_every_key_the_vault_holds() -> None:
    rotated = Keyring(SECOND_VERSION, {FIRST_VERSION: FIRST_KEY, SECOND_VERSION: SECOND_KEY})
    scanner = Vault(rotated, INSTANCE_ONE).key_scanner()

    scanner.feed(FIRST_KEY)

    assert scanner.found


def test_a_restore_vault_reads_the_archive_key_and_writes_the_running_key_one_version_up() -> None:
    archive_vault = vault_with(SECOND_KEY, version=SECOND_VERSION)
    sealed_in_archive = archive_vault.seal("value", LOCATION)

    restore = Vault.for_restore(
        running_key=THIRD_KEY,
        archive_key=SECOND_KEY,
        archive_version=SECOND_VERSION,
        instance_id=INSTANCE_ONE,
    )
    resealed = restore.seal(restore.open(sealed_in_archive, LOCATION), LOCATION)

    assert restore.current_version == SECOND_VERSION + 1
    assert resealed.startswith(f"v{SECOND_VERSION + 1}:")
    assert vault_with(THIRD_KEY, version=SECOND_VERSION + 1).open(resealed, LOCATION) == "value"
