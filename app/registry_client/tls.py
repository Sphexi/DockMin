"""TLS contexts for registry connections: the system trust store plus an optional CA bundle."""

import ssl
from pathlib import Path
from typing import Final

_FALLBACK_CA_FILES: Final = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)


class SystemTrustUnavailable(ssl.SSLError):
    """Raised when no operating system root certificates can be found."""


def system_trust_context() -> ssl.SSLContext:
    """Verify against the operating system's roots, ignoring SSL_CERT_FILE and SSL_CERT_DIR.

    Uses the locations compiled into the TLS library, then a fixed list of common ones. Raises
    SystemTrustUnavailable when none exists.
    """
    paths = ssl.get_default_verify_paths()
    cafile = paths.openssl_cafile if Path(paths.openssl_cafile).is_file() else None
    capath = paths.openssl_capath if Path(paths.openssl_capath).is_dir() else None
    if cafile is None and capath is None:
        cafile = next((path for path in _FALLBACK_CA_FILES if Path(path).is_file()), None)
    if cafile is None and capath is None:
        raise SystemTrustUnavailable("no operating system root certificates were found")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=cafile, capath=capath)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def no_trust_context() -> ssl.SSLContext:
    """Build a context that trusts nothing, so every verified handshake fails."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def registry_context(*, verify_tls: bool, ca_bundle: str | None) -> ssl.SSLContext:
    """Build the client context for a registry; raise ssl.SSLError if it cannot be built.

    The bundle is added to the system roots; SystemTrustUnavailable, a subclass of the error, means
    there are none. Without verification no bundle is consulted.
    """
    if verify_tls:
        context = system_trust_context()
        if ca_bundle:
            context.load_verify_locations(cadata=ca_bundle)
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(["http/1.1"])
    return context
