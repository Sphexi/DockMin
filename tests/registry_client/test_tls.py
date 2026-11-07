"""Registry TLS contexts: the system roots plus a bundle, with no ambient configuration."""

import ssl
from pathlib import Path

import pytest

from app.registry_client import tls
from app.registry_client.tls import (
    SystemTrustUnavailable,
    no_trust_context,
    registry_context,
    system_trust_context,
)
from tests.registry_client.loopback import Certificates

MISSING = "/nonexistent/dockmin-test"


@pytest.fixture
def no_compiled_in_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the TLS library report no default trust locations and the fallback list empty."""
    missing = ssl.DefaultVerifyPaths(
        cafile=None,  # type: ignore[arg-type]
        capath=None,  # type: ignore[arg-type]
        openssl_cafile_env="SSL_CERT_FILE",
        openssl_cafile=MISSING,
        openssl_capath_env="SSL_CERT_DIR",
        openssl_capath=MISSING,
    )
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: missing)
    monkeypatch.setattr(tls, "_FALLBACK_CA_FILES", (MISSING,))


def roots_of(context: ssl.SSLContext) -> int:
    return int(context.cert_store_stats()["x509_ca"])


@pytest.fixture
def ca_file(tmp_path: Path, certificates: Certificates) -> Path:
    path = tmp_path / "ca.pem"
    path.write_text(certificates.ca_pem, encoding="utf-8")
    return path


def test_the_environment_cannot_supply_roots_when_the_system_has_none(
    no_compiled_in_roots: None, ca_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    monkeypatch.setenv("SSL_CERT_DIR", str(ca_file.parent))

    with pytest.raises(SystemTrustUnavailable):
        system_trust_context()


def test_a_fixed_fallback_file_is_used_when_the_library_has_no_default_location(
    no_compiled_in_roots: None, ca_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tls, "_FALLBACK_CA_FILES", (MISSING, str(ca_file)))

    context = system_trust_context()

    assert roots_of(context) == 1


def test_a_system_context_verifies_with_a_modern_protocol_and_checks_host_names() -> None:
    context = system_trust_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2


def test_a_ca_bundle_is_added_to_the_system_roots_not_substituted_for_them(
    certificates: Certificates,
) -> None:
    system = roots_of(system_trust_context())

    context = registry_context(verify_tls=True, ca_bundle=certificates.ca_pem)

    assert roots_of(context) == system + 1


def test_verification_off_trusts_nothing_and_checks_nothing_and_ignores_a_bundle(
    certificates: Certificates,
) -> None:
    context = registry_context(verify_tls=False, ca_bundle=certificates.ca_pem)

    assert context.verify_mode is ssl.CERT_NONE
    assert context.check_hostname is False
    assert roots_of(context) == 0


def test_a_bundle_that_is_not_pem_is_refused_when_the_context_is_built() -> None:
    with pytest.raises(ssl.SSLError):
        registry_context(verify_tls=True, ca_bundle="not a certificate")


def test_a_context_that_trusts_nothing_still_requires_verification() -> None:
    context = no_trust_context()

    assert roots_of(context) == 0
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2
