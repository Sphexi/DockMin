"""Request addressing for path-style and virtual-host S3 endpoints."""

import ipaddress
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from app.object_storage.errors import StorageFailure

MAX_ENDPOINT_CHARS: Final = 2048
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_LABEL: Final = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_HOST: Final = re.compile(rf"{_LABEL}(?:\.{_LABEL})*", re.ASCII)
_PATH_BUCKET: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", re.ASCII)
_VHOST_BUCKET: Final = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", re.ASCII)
_REGION: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", re.ASCII)


@dataclass(frozen=True, slots=True)
class Addressing:
    """Where requests go: the scheme, the Host header value, and how a key becomes a path."""

    scheme: str
    host: str
    bucket_path: str
    key_prefix: str

    def path(self, key: str | None) -> str:
        """Return the request path for a key, or for the bucket itself."""
        if key is None:
            return self.bucket_path
        return f"{self.key_prefix}{key}"

    def url(self, path: str) -> str:
        """Return the absolute URL for a path."""
        return f"{self.scheme}://{self.host}{path}"


def resolve(*, endpoint_url: str, bucket: str, region: str, use_path_style: bool) -> Addressing:
    """Return the addressing for a target, or raise StorageFailure('endpoint_invalid')."""
    scheme, hostname, authority_port = _split_endpoint(endpoint_url)
    if _REGION.fullmatch(region) is None:
        raise StorageFailure("endpoint_invalid")
    is_ip = _is_ip(hostname)
    if use_path_style:
        if _PATH_BUCKET.fullmatch(bucket) is None:
            raise StorageFailure("endpoint_invalid")
        host = _authority(hostname, authority_port, is_ip)
        return Addressing(scheme, host, f"/{bucket}", f"/{bucket}/")
    if is_ip or _VHOST_BUCKET.fullmatch(bucket) is None:
        raise StorageFailure("endpoint_invalid")
    host = _authority(f"{bucket}.{hostname}", authority_port, False)
    return Addressing(scheme, host, "/", "/")


def _split_endpoint(endpoint_url: str) -> tuple[str, str, int | None]:
    if len(endpoint_url) > MAX_ENDPOINT_CHARS or not endpoint_url.isascii():
        raise StorageFailure("endpoint_invalid")
    if not endpoint_url.isprintable() or any(c in endpoint_url for c in " \\?#@"):
        raise StorageFailure("endpoint_invalid")
    try:
        parts = urlsplit(endpoint_url)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise StorageFailure("endpoint_invalid") from None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not hostname or parts.path not in ("", "/"):
        raise StorageFailure("endpoint_invalid")
    hostname = hostname.lower()
    if port == 0:
        raise StorageFailure("endpoint_invalid")
    if port == _DEFAULT_PORTS[scheme]:
        port = None
    if not _is_ip(hostname) and not _is_dns_name(hostname):
        raise StorageFailure("endpoint_invalid")
    return scheme, hostname, port


def _is_ip(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _is_dns_name(hostname: str) -> bool:
    return (
        len(hostname) <= 253
        and _HOST.fullmatch(hostname) is not None
        and not hostname.rsplit(".", 1)[-1].isdigit()
    )


def _authority(hostname: str, port: int | None, is_ip: bool) -> str:
    host = f"[{hostname}]" if is_ip and ":" in hostname else hostname
    return host if port is None else f"{host}:{port}"
