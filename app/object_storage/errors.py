"""Failures of the object storage client; every message is product-written and carries no input."""

import re
from typing import Final, Literal

from app.netpolicy import REFUSAL_TEXT

type StorageFailureKind = Literal[
    "endpoint_invalid",
    "key_invalid",
    "unreachable",
    "address_refused",
    "tls_failed",
    "refused",
    "bucket_missing",
    "digest_mismatch",
    "clock_skew",
    "object_missing",
    "storage_error",
]

MAX_ERROR_BODY: Final = 65_536

MESSAGES: Final[dict[StorageFailureKind, str]] = {
    "endpoint_invalid": (
        "The storage endpoint, region or bucket is not usable. The endpoint must be an http or "
        "https address with a host name and no user name, path, query or fragment; virtual-host "
        "addressing needs a host name rather than an IP address, and a bucket name that is a "
        "single lower-case DNS label."
    ),
    "key_invalid": (
        "The object key is not allowed. Use letters, digits, '.', '_', '-' and '/', with no empty "
        "or dot-only segment, up to 1024 characters."
    ),
    "unreachable": (
        "The storage endpoint could not be reached. Check the address, the port and this "
        "server's outbound access."
    ),
    "address_refused": REFUSAL_TEXT,
    "tls_failed": (
        "The TLS connection to the storage endpoint failed. Check the endpoint's certificate and "
        "the CA bundle, and that the endpoint's scheme matches the port."
    ),
    "refused": (
        "Access was refused or the bucket does not exist. Check the access key, the secret, the "
        "bucket name and the credential's permissions."
    ),
    "bucket_missing": "The bucket does not exist on the storage endpoint.",
    "digest_mismatch": (
        "The storage endpoint reported that the uploaded content did not match its checksum."
    ),
    "clock_skew": (
        "The storage endpoint rejected the request because this server's clock differs from the "
        "endpoint's. Correct the clock on this server."
    ),
    "object_missing": "The archive is no longer in the bucket.",
    "storage_error": "The storage endpoint answered with an error.",
}

_CODE: Final = re.compile(r"<Code>([A-Za-z0-9.]{1,64})</Code>")
_REFUSED_CODES: Final = frozenset({"AccessDenied", "SignatureDoesNotMatch", "InvalidAccessKeyId"})
_DIGEST_CODES: Final = frozenset({"BadDigest", "InvalidDigest"})
_SKEW_CODES: Final = frozenset({"RequestTimeTooSkewed"})
_BUCKET_CODES: Final = frozenset({"NoSuchBucket"})


class StorageFailure(Exception):
    """A storage operation that did not succeed; `message` is safe to show and to record."""

    def __init__(
        self,
        kind: StorageFailureKind,
        *,
        status: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(MESSAGES[kind])
        self.kind = kind
        self.status = status
        self.code = code
        self.message = MESSAGES[kind]


def error_code(body: bytes) -> str | None:
    """Return the first S3 error code in the capped body, or None."""
    match = _CODE.search(body[:MAX_ERROR_BODY].decode("utf-8", errors="replace"))
    return match.group(1) if match else None


def classify_response(
    status: int, code: str | None, *, bucket_probe: bool, reading: bool = False
) -> StorageFailure:
    """Map a non-success reply to a failure; the code decides, the status only when no code does.

    NoSuchKey means a missing object only for a read.
    """
    kind: StorageFailureKind
    if code in _SKEW_CODES:
        kind = "clock_skew"
    elif code in _DIGEST_CODES:
        kind = "digest_mismatch"
    elif code in _BUCKET_CODES:
        kind = "bucket_missing"
    elif reading and code == "NoSuchKey":
        kind = "object_missing"
    elif code in _REFUSED_CODES or status in (401, 403):
        kind = "refused"
    elif bucket_probe and status == 404:
        kind = "bucket_missing"
    else:
        kind = "storage_error"
    return StorageFailure(kind, status=status, code=code)
