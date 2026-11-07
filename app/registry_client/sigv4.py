"""AWS Signature Version 4 request signing."""

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, unquote

from pydantic import SecretStr

ALGORITHM = "AWS4-HMAC-SHA256"
AMZ_DATE_FORMAT = "%Y%m%dT%H%M%SZ"
_SCOPE_TERMINATOR = "aws4_request"
_UNRESERVED = "-_.~"
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_PAYLOAD_HASH = re.compile(r"[0-9a-f]{64}", re.ASCII)


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    """The canonical request text and the signed header list it declares."""

    text: str
    signed_headers: str


def sha256_hex(data: bytes) -> str:
    """Return the lower-case hex SHA-256 of data."""
    return hashlib.sha256(data).hexdigest()


def signing_key(secret_access_key: str, date: str, region: str, service: str) -> bytes:
    """Derive the signing key for a date (YYYYMMDD), region and service."""
    key = _hmac(f"AWS4{secret_access_key}".encode(), date)
    for part in (region, service, _SCOPE_TERMINATOR):
        key = _hmac(key, part)
    return key


def canonical_request(
    *,
    method: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    payload_hash: str,
) -> CanonicalRequest:
    """Build the canonical request; headers must already hold every header to be signed."""
    normalised = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    names = sorted(normalised)
    block = "".join(f"{name}:{normalised[name]}\n" for name in names)
    signed = ";".join(names)
    text = "\n".join(
        (
            method,
            quote(_remove_dot_segments(path), safe="/~"),
            _canonical_query(query),
            block,
            signed,
            payload_hash,
        )
    )
    return CanonicalRequest(text, signed)


def string_to_sign(canonical: str, amz_date: str, scope: str) -> str:
    """Build the string that is signed."""
    return "\n".join((ALGORITHM, amz_date, scope, sha256_hex(canonical.encode())))


def sign_request(
    *,
    method: str,
    host: str,
    path: str,
    query: str = "",
    headers: Mapping[str, str],
    body: bytes,
    access_key_id: str,
    secret_access_key: SecretStr,
    region: str,
    service: str,
    moment: datetime,
    content_sha256_header: bool = False,
    payload_sha256: str | None = None,
) -> dict[str, str]:
    """Return the headers to send, other than Host: the given ones, X-Amz-Date and Authorization.

    Every given header is signed along with Host and X-Amz-Date. Header values must be
    single-line. payload_sha256 is the lower-case hex SHA-256 of a body that is sent separately;
    body must then be empty.
    """
    if payload_sha256 is not None and (body or _PAYLOAD_HASH.fullmatch(payload_sha256) is None):
        raise ValueError("payload_sha256 needs an empty body and a lower-case hex SHA-256")
    for name, value in headers.items():
        if not _HEADER_NAME.match(name):
            raise ValueError("header names must be tokens")
        if name.lower() in ("host", "x-amz-date", "authorization"):
            raise ValueError(f"header {name} is set by the signer")
        if any(c in value for c in "\r\n\0"):
            raise ValueError("header values must be single-line")
    stamp = moment.astimezone(UTC).strftime(AMZ_DATE_FORMAT)
    date = stamp[:8]
    payload_hash = payload_sha256 or sha256_hex(body)

    sent = dict(headers)
    sent["X-Amz-Date"] = stamp
    if content_sha256_header:
        sent["X-Amz-Content-Sha256"] = payload_hash
    request = canonical_request(
        method=method,
        path=path,
        query=query,
        headers={**sent, "Host": host},
        payload_hash=payload_hash,
    )
    scope = f"{date}/{region}/{service}/{_SCOPE_TERMINATOR}"
    key = signing_key(secret_access_key.get_secret_value(), date, region, service)
    signature = hmac.new(
        key, string_to_sign(request.text, stamp, scope).encode(), hashlib.sha256
    ).hexdigest()
    sent["Authorization"] = (
        f"{ALGORITHM} Credential={access_key_id}/{scope}, "
        f"SignedHeaders={request.signed_headers}, Signature={signature}"
    )
    return sent


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _remove_dot_segments(path: str) -> str:
    if not path:
        return "/"
    kept: list[str] = []
    for segment in path.split("/"):
        if segment and segment != ".":
            if segment == "..":
                if kept:
                    kept.pop()
            else:
                kept.append(segment)
    leading = "/" if path[0] == "/" else ""
    trailing = "/" if path[-1] == "/" and kept else ""
    return leading + "/".join(kept) + trailing


def _canonical_query(query: str) -> str:
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        if part:
            name, _, value = part.partition("=")
            pairs.append(
                (quote(unquote(name), safe=_UNRESERVED), quote(unquote(value), safe=_UNRESERVED))
            )
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))
