"""Aims a Compose invocation at one environment: DOCKER_HOST and per-invocation TLS files."""

import ipaddress
import os
import re
import shutil
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

TLS_DIRECTORY = "tls"
CA_FILE = "ca.pem"
CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"

_DEFAULT_TLS_PORT = 2376
_MAX_SOCKET_PATH_CHARS = 256
_MAX_HOSTNAME_CHARS = 253
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_HOSTNAME_LABEL = re.compile(r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0[xX][0-9a-fA-F]*")
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class InvalidTarget(ValueError):
    """The environment row or its material cannot be turned into a Compose target."""


@dataclass(frozen=True, slots=True, repr=False)
class TargetMaterial:
    """PEM text for mutual TLS, with the client key decrypted."""

    ca_pem: str
    cert_pem: str
    key_pem: str

    def __repr__(self) -> str:
        return "TargetMaterial(<redacted>)"


class Endpoint(Protocol):
    """The connection fields of an environment row that a target is built from."""

    @property
    def connection_kind(self) -> str:
        """Return `unix_socket` or `tcp_tls`."""
        ...

    @property
    def socket_path(self) -> str | None:
        """Return the socket path of a local environment."""
        ...

    @property
    def url(self) -> str | None:
        """Return the remote endpoint of a TLS environment."""
        ...


@dataclass(frozen=True, slots=True)
class ComposeTarget:
    """Where a Compose invocation connects, and the identity it presents."""

    docker_host: str
    tls: TargetMaterial | None = field(repr=False)


def target_for(environment: Endpoint, material: TargetMaterial | None) -> ComposeTarget:
    """Build the target of an environment row.

    Raises InvalidTarget for TLS material on a socket, a socket path that is not a plain path,
    a remote endpoint without material, or a malformed remote endpoint.
    """
    if environment.connection_kind == "unix_socket":
        if material is not None:
            raise InvalidTarget("A local socket environment takes no TLS material.")
        return ComposeTarget(_unix_host(environment.socket_path), None)
    if material is None:
        raise InvalidTarget("A remote environment requires TLS material.")
    if not (material.ca_pem.strip() and material.cert_pem.strip() and material.key_pem.strip()):
        raise InvalidTarget("The TLS material is incomplete.")
    return ComposeTarget(_tcp_host(environment.url), material)


def materialise(target: ComposeTarget, workdir: Path) -> dict[str, str]:
    """Return the connection variables, writing the TLS files beneath workdir for a TLS target.

    The caller owns workdir and removes it, key included, once the process has exited. Raises
    InvalidTarget when workdir is not a private directory of this user, and FileExistsError when
    anything already sits at its `tls` entry.
    """
    variables = {"DOCKER_HOST": target.docker_host}
    material = target.tls
    if material is None:
        return variables

    _require_private_directory(workdir)
    tls_dir = workdir / TLS_DIRECTORY
    tls_dir.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    try:
        tls_dir.chmod(_PRIVATE_DIRECTORY_MODE)
        _write_private(tls_dir / CA_FILE, material.ca_pem)
        _write_private(tls_dir / CERT_FILE, material.cert_pem)
        _write_private(tls_dir / KEY_FILE, material.key_pem)
    except BaseException:
        shutil.rmtree(tls_dir, ignore_errors=True)
        raise
    variables["DOCKER_TLS_VERIFY"] = "1"
    variables["DOCKER_CERT_PATH"] = str(tls_dir)
    return variables


def _unix_host(socket_path: str | None) -> str:
    refusal = InvalidTarget("The socket path is not a plain absolute path.")
    if (
        not socket_path
        or len(socket_path) > _MAX_SOCKET_PATH_CHARS
        or not socket_path.startswith("/")
        or socket_path.startswith("//")
        or ".." in socket_path.split("/")
        or any(c in socket_path for c in "?#")
        or _has_hidden_characters(socket_path)
    ):
        raise refusal
    url = f"unix://{socket_path}"
    parts = urlsplit(url)
    if parts.scheme != "unix" or parts.netloc or parts.path != socket_path:
        raise refusal
    return url


def _tcp_host(url: str | None) -> str:
    refusal = InvalidTarget("The remote endpoint is not a tcp://host:port address.")
    if not url:
        raise refusal
    try:
        parts = urlsplit(url)
        host = _canonical_host(parts.hostname)
        port = _DEFAULT_TLS_PORT if parts.port is None else parts.port
    except ValueError:
        raise refusal from None
    if (
        parts.scheme != "tcp"
        or parts.username is not None
        or parts.password is not None
        or parts.path
        or parts.query
        or parts.fragment
        or not 0 < port <= 65535
    ):
        raise refusal
    authority = f"[{host}]" if ":" in host else host
    return f"tcp://{authority}:{port}"


def _canonical_host(host: str | None) -> str:
    if not host or "%" in host or _has_hidden_characters(host):
        raise ValueError(host)
    if ":" in host:
        return ipaddress.IPv6Address(host).compressed
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass
    labels = host.split(".")
    if (
        len(host) > _MAX_HOSTNAME_CHARS
        or not all(_HOSTNAME_LABEL.fullmatch(label) for label in labels)
        or _NUMERIC_LABEL.fullmatch(labels[-1])
    ):
        raise ValueError(host)
    return host


def _has_hidden_characters(text: str) -> bool:
    return any(unicodedata.category(c) in _HIDDEN_CATEGORIES for c in text)


def _require_private_directory(path: Path) -> None:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise InvalidTarget("The Compose work directory is not a directory.")
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise InvalidTarget("The Compose work directory is not private to this user.")


def _write_private(path: Path, text: str) -> None:
    # O_EXCL | O_NOFOLLOW: a pre-planted file or symlink at this name is an error, never a target.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    data = text if text.endswith("\n") else f"{text}\n"
    with os.fdopen(os.open(path, flags, _PRIVATE_FILE_MODE), "wb") as handle:
        # The umask can narrow the creation mode; pin it so the mode is the same everywhere.
        os.fchmod(handle.fileno(), _PRIVATE_FILE_MODE)
        handle.write(data.encode("utf-8"))
