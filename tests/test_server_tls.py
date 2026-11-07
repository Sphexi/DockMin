"""Tests for app/tls.py: certificate summaries, expiry states and the server context."""

import ssl
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.x509.oid import ObjectIdentifier

from app import tls
from app.tls import (
    EXPIRY_WARNING,
    CertificateUnusable,
    expiry_state,
    load_server_tls,
    read_certificate,
)
from tests.docker_engine.tls_material import key_of, key_pem
from tests.tls_support import NOW, OTHER_KEY_VARIANT, VALID_DAYS, write_server_files

DAY = timedelta(days=1)


def test_a_self_signed_certificate_is_summarised_with_its_names_validity_and_key(
    tmp_path: Path,
) -> None:
    files = write_server_files(tmp_path)

    summary = read_certificate(files.cert)

    assert summary.subject == "CN=dockmin.example.test"
    assert summary.issuer == summary.subject
    assert summary.self_signed
    assert summary.serial_hex == "ABCDEF"
    assert summary.subject_alt_names == ("DNS:dockmin.example.test", "IP:192.0.2.10")
    assert (summary.key_type, summary.key_bits) == ("EC secp256r1", 256)
    assert summary.not_after - summary.not_before == timedelta(days=VALID_DAYS + 1)
    assert summary.chain_length == 1
    assert len(summary.sha256_fingerprint.split(":")) == 32


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        pytest.param("rsa", ("RSA", 2048), id="rsa"),
        pytest.param("p384", ("EC secp384r1", 384), id="p384"),
        pytest.param("ed25519", ("Ed25519", None), id="ed25519"),
    ],
)
def test_the_key_type_and_size_are_reported_for_each_kind(
    tmp_path: Path, kind: str, expected: tuple[str, int | None]
) -> None:
    files = write_server_files(tmp_path, kind=kind)  # type: ignore[arg-type]

    summary = read_certificate(files.cert)

    assert (summary.key_type, summary.key_bits) == expected


def test_a_chain_reports_its_length_and_summarises_the_first_certificate(tmp_path: Path) -> None:
    files = write_server_files(tmp_path, chain=3)

    summary = read_certificate(files.cert)

    assert summary.chain_length == 3
    assert summary.subject == "CN=dockmin.example.test"
    assert summary.issuer == "CN=test issuing CA"
    assert not summary.self_signed


def test_a_certificate_with_no_names_has_an_empty_name_list(tmp_path: Path) -> None:
    files = write_server_files(tmp_path, hosts=(), addresses=())

    assert read_certificate(files.cert).subject_alt_names == ()


def test_alternative_names_are_cut_at_the_cap(tmp_path: Path) -> None:
    hosts = tuple(f"h{number}.example.test" for number in range(tls.MAX_ALTERNATIVE_NAMES + 20))
    files = write_server_files(tmp_path, hosts=hosts)

    assert len(read_certificate(files.cert).subject_alt_names) == tls.MAX_ALTERNATIVE_NAMES


@pytest.mark.parametrize(
    ("offset", "state"),
    [
        pytest.param(-2 * VALID_DAYS * DAY, "not_yet_valid", id="before-the-window"),
        pytest.param(timedelta(0), "valid", id="well-inside"),
        pytest.param(VALID_DAYS * DAY - EXPIRY_WARNING + DAY, "expiring", id="inside-warning"),
        pytest.param(VALID_DAYS * DAY - EXPIRY_WARNING - 2 * DAY, "valid", id="just-outside"),
        pytest.param(3 * VALID_DAYS * DAY, "expired", id="after-the-window"),
    ],
)
def test_validity_is_classified_against_a_given_instant(
    tmp_path: Path, offset: timedelta, state: str
) -> None:
    summary = read_certificate(write_server_files(tmp_path).cert)

    assert expiry_state(summary, NOW + offset) == state


def test_the_warning_boundary_is_inclusive(tmp_path: Path) -> None:
    summary = read_certificate(write_server_files(tmp_path).cert)

    assert expiry_state(summary, summary.not_after - EXPIRY_WARNING) == "expiring"
    assert expiry_state(summary, summary.not_after) == "expiring"
    assert expiry_state(summary, summary.not_after + timedelta(seconds=1)) == "expired"


def test_a_file_that_is_not_pem_is_unusable_and_the_message_holds_none_of_its_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "junk.pem"
    path.write_text("-----BEGIN NOTHING-----\nsecret-marker-text\n", encoding="ascii")

    with pytest.raises(CertificateUnusable) as raised:
        read_certificate(path)

    assert "secret-marker-text" not in str(raised.value)


def test_a_missing_certificate_file_is_unusable(tmp_path: Path) -> None:
    with pytest.raises(CertificateUnusable) as raised:
        read_certificate(tmp_path / "absent.pem")

    assert "absent.pem" in str(raised.value)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(x509.DuplicateExtension("duplicate", ObjectIdentifier("2.5.29.17")), id="dup"),
        pytest.param(
            x509.UnsupportedGeneralNameType("unsupported", 99), id="unsupported-general-name"
        ),
        pytest.param(ValueError("bad"), id="value-error"),
    ],
)
def test_a_certificate_whose_fields_cannot_be_parsed_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    files = write_server_files(tmp_path)

    def fail(_: x509.Certificate) -> tuple[str, ...]:
        raise error

    monkeypatch.setattr(tls, "_alternative_names", fail)

    with pytest.raises(CertificateUnusable) as raised:
        read_certificate(files.cert)

    assert "cannot be read" in str(raised.value)


def test_a_matching_pair_builds_a_tls12_server_context(tmp_path: Path) -> None:
    files = write_server_files(tmp_path)

    context, summary = load_server_tls(files.cert, files.key)

    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_NONE
    assert summary.subject == "CN=dockmin.example.test"


def test_a_key_that_does_not_match_the_certificate_is_unusable_and_named(tmp_path: Path) -> None:
    files = write_server_files(tmp_path)
    files.key.write_text(key_pem(key_of("p256", OTHER_KEY_VARIANT)), encoding="ascii")

    with pytest.raises(CertificateUnusable) as raised:
        load_server_tls(files.cert, files.key)

    assert "does not match" in str(raised.value)


def test_an_encrypted_key_is_refused_without_prompting(tmp_path: Path) -> None:
    files = write_server_files(tmp_path)
    files.key.write_text(key_pem(key_of("p256"), passphrase=b"pw"), encoding="ascii")

    with pytest.raises(CertificateUnusable) as raised:
        load_server_tls(files.cert, files.key)

    assert "unencrypted" in str(raised.value)


def test_an_unreadable_key_file_is_unusable(tmp_path: Path) -> None:
    files = write_server_files(tmp_path)
    files.key.unlink()

    with pytest.raises(CertificateUnusable) as raised:
        load_server_tls(files.cert, files.key)

    assert "private key file" in str(raised.value)


def test_a_key_file_holding_junk_is_unusable_and_the_message_holds_none_of_it(
    tmp_path: Path,
) -> None:
    files = write_server_files(tmp_path)
    files.key.write_text("junk-key-marker", encoding="ascii")

    with pytest.raises(CertificateUnusable) as raised:
        load_server_tls(files.cert, files.key)

    assert "junk-key-marker" not in str(raised.value)
