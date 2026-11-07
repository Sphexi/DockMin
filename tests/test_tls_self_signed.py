"""Tests for app/tls_self_signed.py: the generated certificate pair and when it is replaced."""

import ipaddress
import os
import ssl
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.config import SELF_SIGNED_CERT_FILENAME, SELF_SIGNED_KEY_FILENAME
from app.tls import CertificateUnusable, read_certificate
from app.tls_self_signed import (
    CLOCK_SKEW_ALLOWANCE,
    COMMON_NAME,
    SELF_SIGNED_VALIDITY,
    ensure_self_signed,
)

NOW = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
DIRECTORY_MODE = 0o700
CERT_MODE = 0o644
KEY_MODE = 0o600
LOOSE_DIRECTORY_MODE = 0o755
LOOSE_KEY_MODE = 0o644
EXPECTED_NAMES = [
    x509.DNSName("localhost"),
    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    x509.IPAddress(ipaddress.IPv6Address("::1")),
]


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    return tmp_path / "server-tls"


def modes_in(path: Path) -> dict[str, int]:
    return {entry.name: stat.S_IMODE(entry.stat().st_mode) for entry in path.iterdir()}


def parsed(directory: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate((directory / SELF_SIGNED_CERT_FILENAME).read_bytes())


def test_the_first_call_creates_the_directory_and_both_files_with_owner_only_secrets(
    directory: Path,
) -> None:
    pair = ensure_self_signed(directory, NOW)

    assert (pair.created, pair.reason) == (True, "missing")
    assert (pair.cert_file, pair.key_file) == (
        directory / SELF_SIGNED_CERT_FILENAME,
        directory / SELF_SIGNED_KEY_FILENAME,
    )
    assert stat.S_IMODE(directory.stat().st_mode) == DIRECTORY_MODE
    assert modes_in(directory) == {
        SELF_SIGNED_CERT_FILENAME: CERT_MODE,
        SELF_SIGNED_KEY_FILENAME: KEY_MODE,
    }


def test_the_certificate_names_only_the_loopback_hosts_and_is_not_a_certificate_authority(
    directory: Path,
) -> None:
    ensure_self_signed(directory, NOW)

    certificate = parsed(directory)

    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert list(names.value) == EXPECTED_NAMES
    assert (constraints.value.ca, constraints.critical) == (False, True)
    assert list(usage.value) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert certificate.subject == certificate.issuer
    assert certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == COMMON_NAME


def test_the_key_is_a_p256_key_that_matches_the_certificate_and_is_stored_unencrypted(
    directory: Path,
) -> None:
    pair = ensure_self_signed(directory, NOW)

    public_key = parsed(directory).public_key()
    context, summary = pair.tls
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    assert isinstance(public_key.curve, ec.SECP256R1)
    assert isinstance(context, ssl.SSLContext)
    assert summary == read_certificate(pair.cert_file)
    assert b"BEGIN PRIVATE KEY" in pair.key_file.read_bytes()


def test_the_certificate_is_valid_for_exactly_the_documented_period_from_just_before_now(
    directory: Path,
) -> None:
    ensure_self_signed(directory, NOW)

    certificate = parsed(directory)

    assert certificate.not_valid_before_utc == NOW - CLOCK_SKEW_ALLOWANCE
    assert (
        certificate.not_valid_after_utc - certificate.not_valid_before_utc == SELF_SIGNED_VALIDITY
    )


def test_a_valid_pair_is_reused_unchanged_and_returned_as_loaded(directory: Path) -> None:
    first = ensure_self_signed(directory, NOW)
    before = (first.cert_file.read_bytes(), first.key_file.read_bytes())

    second = ensure_self_signed(directory, NOW + DAY)

    assert (second.created, second.reason) == (False, None)
    assert (second.cert_file.read_bytes(), second.key_file.read_bytes()) == before
    assert second.tls[1] == first.tls[1]
    assert isinstance(second.tls[0], ssl.SSLContext)


def test_reusing_a_pair_tightens_a_key_and_a_directory_that_had_been_loosened(
    directory: Path,
) -> None:
    pair = ensure_self_signed(directory, NOW)
    pair.key_file.chmod(LOOSE_KEY_MODE)
    directory.chmod(LOOSE_DIRECTORY_MODE)

    again = ensure_self_signed(directory, NOW)

    assert again.created is False
    assert stat.S_IMODE(directory.stat().st_mode) == DIRECTORY_MODE
    assert stat.S_IMODE(again.key_file.stat().st_mode) == KEY_MODE


def remove_the_key(directory: Path) -> None:
    (directory / SELF_SIGNED_KEY_FILENAME).unlink()


def remove_the_certificate(directory: Path) -> None:
    (directory / SELF_SIGNED_CERT_FILENAME).unlink()


def overwrite_the_certificate_with_text(directory: Path) -> None:
    (directory / SELF_SIGNED_CERT_FILENAME).write_text("not a certificate", encoding="ascii")


def overwrite_the_key_with_another_pairs(directory: Path) -> None:
    other = directory.parent / "other"
    ensure_self_signed(other, NOW)
    (directory / SELF_SIGNED_KEY_FILENAME).write_bytes(
        (other / SELF_SIGNED_KEY_FILENAME).read_bytes()
    )


def replace_the_key_with_a_link(directory: Path) -> None:
    key = directory / SELF_SIGNED_KEY_FILENAME
    copy = directory.parent / "key-copy.pem"
    copy.write_bytes(key.read_bytes())
    key.unlink()
    key.symlink_to(copy)


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        pytest.param(remove_the_key, "missing", id="key-missing"),
        pytest.param(remove_the_certificate, "missing", id="certificate-missing"),
        pytest.param(overwrite_the_certificate_with_text, "unusable", id="certificate-garbage"),
        pytest.param(overwrite_the_key_with_another_pairs, "unusable", id="mismatched-pair"),
        pytest.param(replace_the_key_with_a_link, "unusable", id="key-is-a-link"),
    ],
)
def test_a_pair_that_is_damaged_is_replaced_with_the_reason_given(
    directory: Path, damage: Callable[[Path], None], reason: str
) -> None:
    first = ensure_self_signed(directory, NOW)
    old_fingerprint = first.tls[1].sha256_fingerprint
    damage(directory)

    again = ensure_self_signed(directory, NOW)

    assert (again.created, again.reason) == (True, reason)
    assert again.tls[1].sha256_fingerprint != old_fingerprint
    assert not (directory / SELF_SIGNED_KEY_FILENAME).is_symlink()
    assert modes_in(directory) == {
        SELF_SIGNED_CERT_FILENAME: CERT_MODE,
        SELF_SIGNED_KEY_FILENAME: KEY_MODE,
    }


@pytest.mark.parametrize(
    ("later", "reason"),
    [
        pytest.param(SELF_SIGNED_VALIDITY - 10 * DAY, "expiring", id="expiring"),
        pytest.param(SELF_SIGNED_VALIDITY + 10 * DAY, "expired", id="expired"),
        pytest.param(-10 * DAY, "not_yet_valid", id="not-yet-valid"),
    ],
)
def test_a_pair_outside_its_good_window_is_replaced_with_a_pair_good_at_that_time(
    directory: Path, later: timedelta, reason: str
) -> None:
    first = ensure_self_signed(directory, NOW)
    when = NOW + later

    again = ensure_self_signed(directory, when)

    assert (again.created, again.reason) == (True, reason)
    assert again.tls[1].sha256_fingerprint != first.tls[1].sha256_fingerprint
    assert again.tls[1].not_before <= when <= again.tls[1].not_after


def test_a_pair_with_more_than_thirty_days_left_is_not_replaced(directory: Path) -> None:
    first = ensure_self_signed(directory, NOW)

    again = ensure_self_signed(directory, NOW + SELF_SIGNED_VALIDITY - 60 * DAY)

    assert again.created is False
    assert again.tls[1] == first.tls[1]


def test_a_directory_that_is_a_symbolic_link_is_refused_and_nothing_is_written_through_it(
    directory: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    directory.symlink_to(target)

    with pytest.raises(CertificateUnusable) as raised:
        ensure_self_signed(directory, NOW)

    assert str(raised.value) == f"The certificate directory {directory} is a symbolic link"
    assert list(target.iterdir()) == []


def test_a_directory_that_is_a_file_is_refused(directory: Path) -> None:
    directory.write_text("x", encoding="utf-8")

    with pytest.raises(CertificateUnusable) as raised:
        ensure_self_signed(directory, NOW)

    assert str(raised.value) == f"The certificate directory {directory} is not a directory"


def test_a_directory_whose_parent_is_missing_is_reported_as_unable_to_store_the_pair(
    tmp_path: Path,
) -> None:
    orphan = tmp_path / "absent" / "server-tls"

    with pytest.raises(CertificateUnusable) as raised:
        ensure_self_signed(orphan, NOW)

    assert str(raised.value) == (
        f"The self-signed certificate in {orphan} cannot be stored: {os.strerror(2)}"
    )


def test_a_write_that_fails_leaves_no_temporary_file_and_the_next_call_succeeds(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, os.strerror(13))

    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", denied)
        with pytest.raises(CertificateUnusable) as raised:
            ensure_self_signed(directory, NOW)

    assert str(raised.value).startswith(f"The self-signed certificate in {directory} cannot")
    assert list(directory.iterdir()) == []
    assert ensure_self_signed(directory, NOW).reason == "missing"
