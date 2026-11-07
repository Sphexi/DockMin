"""The self-signed server certificate pair: reuse while valid, regenerate otherwise."""

import contextlib
import ipaddress
import os
import secrets
import ssl
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.config import SELF_SIGNED_CERT_FILENAME, SELF_SIGNED_KEY_FILENAME
from app.tls import CertificateSummary, CertificateUnusable, expiry_state, load_server_tls

SELF_SIGNED_VALIDITY: Final = timedelta(days=825)
COMMON_NAME: Final = "dockmin"
CLOCK_SKEW_ALLOWANCE: Final = timedelta(hours=1)
DIRECTORY_MODE: Final = 0o700
CERT_MODE: Final = 0o644
KEY_MODE: Final = 0o600
ALTERNATIVE_NAMES: Final[tuple[x509.GeneralName, ...]] = (
    x509.DNSName("localhost"),
    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    x509.IPAddress(ipaddress.IPv6Address("::1")),
)

type TlsMaterial = tuple[ssl.SSLContext, CertificateSummary]
type RegenerationReason = Literal["missing", "unusable", "expiring", "expired", "not_yet_valid"]


@dataclass(frozen=True, slots=True)
class SelfSignedPair:
    """The certificate and key files to serve, and whether this call wrote them."""

    cert_file: Path
    key_file: Path
    created: bool
    reason: RegenerationReason | None
    tls: TlsMaterial


def ensure_self_signed(directory: Path, now: datetime) -> SelfSignedPair:
    """Return the pair in `directory`, writing a new one when it is absent or not worth serving.

    `directory` is created when absent; its parent must exist. `now` must be timezone-aware.
    Raises CertificateUnusable when the directory or the files cannot be used or written.
    """
    cert_file, key_file = (
        directory / SELF_SIGNED_CERT_FILENAME,
        directory / SELF_SIGNED_KEY_FILENAME,
    )
    try:
        with _open_directory(directory) as directory_fd:
            reason, reused = _inspect(directory_fd, cert_file, key_file, now)
            if reused is None:
                _write_pair(directory_fd, _generate(now))
                tls = load_server_tls(cert_file, key_file)
            else:
                _tighten_key(directory_fd)
                tls = reused
    except OSError as error:
        raise CertificateUnusable(
            f"The self-signed certificate in {directory} cannot be stored: "
            f"{error.strerror or 'I/O error'}"
        ) from None
    return SelfSignedPair(cert_file, key_file, created=reason is not None, reason=reason, tls=tls)


@contextlib.contextmanager
def _open_directory(directory: Path) -> Iterator[int]:
    if directory.is_symlink():
        raise CertificateUnusable(f"The certificate directory {directory} is a symbolic link")
    if directory.exists() and not directory.is_dir():
        raise CertificateUnusable(f"The certificate directory {directory} is not a directory")
    directory.mkdir(mode=DIRECTORY_MODE, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != DIRECTORY_MODE:
            os.fchmod(descriptor, DIRECTORY_MODE)
        yield descriptor
    finally:
        os.close(descriptor)


def _inspect(
    directory_fd: int, cert_file: Path, key_file: Path, now: datetime
) -> tuple[RegenerationReason | None, TlsMaterial | None]:
    present = [_lstat(directory_fd, path.name) for path in (cert_file, key_file)]
    if any(entry is None for entry in present):
        return "missing", None
    if not all(entry is not None and stat.S_ISREG(entry.st_mode) for entry in present):
        return "unusable", None
    try:
        tls = load_server_tls(cert_file, key_file)
    except CertificateUnusable:
        return "unusable", None
    state = expiry_state(tls[1], now)
    return (None, tls) if state == "valid" else (state, None)


def _tighten_key(directory_fd: int) -> None:
    descriptor = os.open(
        SELF_SIGNED_KEY_FILENAME,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory_fd,
    )
    try:
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != KEY_MODE:
            os.fchmod(descriptor, KEY_MODE)
    finally:
        os.close(descriptor)


def _lstat(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _generate(now: datetime) -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    not_before = now - CLOCK_SKEW_ALLOWANCE
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + SELF_SIGNED_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(ALTERNATIVE_NAMES), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key_pem, certificate.public_bytes(serialization.Encoding.PEM)


def _write_pair(directory_fd: int, material: tuple[bytes, bytes]) -> None:
    key_pem, cert_pem = material
    staged: list[str] = []
    try:
        key_temp = _stage(directory_fd, key_pem, KEY_MODE, staged)
        cert_temp = _stage(directory_fd, cert_pem, CERT_MODE, staged)
        os.replace(
            key_temp, SELF_SIGNED_KEY_FILENAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
        )
        staged.remove(key_temp)
        os.replace(
            cert_temp, SELF_SIGNED_CERT_FILENAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
        )
        staged.remove(cert_temp)
        os.fsync(directory_fd)
    finally:
        for leftover in staged:
            with contextlib.suppress(OSError):
                os.unlink(leftover, dir_fd=directory_fd)


def _stage(directory_fd: int, data: bytes, mode: int, staged: list[str]) -> str:
    temp = f".{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
        dir_fd=directory_fd,
    )
    staged.append(temp)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return temp
