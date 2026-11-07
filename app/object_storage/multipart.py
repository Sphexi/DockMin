"""Request and response forms of S3 multipart uploads."""

import hashlib
import html
import re
from collections.abc import Sequence
from typing import Final, Protocol

from app.object_storage.errors import StorageFailure, classify_response, error_code

MAX_MULTIPART_BODY: Final = 65_536
MAX_PARTS: Final = 10_000
UPLOAD_ID: Final = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}", re.ASCII)
ETAG: Final = re.compile(r'"?([A-Za-z0-9-]{1,128})"?', re.ASCII)

_UPLOAD_ID_ELEMENT: Final = re.compile(r"<UploadId>([^<]{1,1024})</UploadId>")
_ETAG_ELEMENT: Final = re.compile(r"<ETag>([^<]{1,256})</ETag>")
_ERROR_ELEMENT: Final = re.compile(r"<Error[\s>]")
_MD5_HEX: Final = re.compile(r"[0-9a-f]{32}", re.ASCII)
_MULTIPART_ETAG: Final = re.compile(r"([0-9a-f]{32})-([0-9]{1,5})", re.ASCII)


class UploadPartRange(Protocol):
    """One part of a file: where it starts, how long it is, and its digests."""

    @property
    def offset(self) -> int: ...

    @property
    def size(self) -> int: ...

    @property
    def sha256_hex(self) -> str: ...

    @property
    def md5_b64(self) -> str: ...


def _text(body: bytes) -> str:
    return body[:MAX_MULTIPART_BODY].decode("utf-8", errors="replace")


def parse_upload_id(body: bytes, status: int) -> str:
    """Return the upload id in a create reply, or raise StorageFailure('storage_error')."""
    text = _text(body)
    found = _UPLOAD_ID_ELEMENT.search(text)
    if (
        "<InitiateMultipartUploadResult" not in text
        or found is None
        or UPLOAD_ID.fullmatch(found.group(1)) is None
    ):
        raise StorageFailure("storage_error", status=status)
    return found.group(1)


def parse_etag(value: str | None, status: int) -> str:
    """Return a part's entity tag without its quotes, or raise StorageFailure('storage_error')."""
    found = ETAG.fullmatch(value) if value is not None else None
    if found is None:
        raise StorageFailure("storage_error", status=status)
    return found.group(1)


def normalise_etags(etags: Sequence[str]) -> list[str]:
    """Return the tags without quotes; raise ValueError for a value that is not an entity tag."""
    found = [ETAG.fullmatch(etag) for etag in etags]
    if any(match is None for match in found):
        raise ValueError("etags holds a value that is not an entity tag")
    return [match.group(1) for match in found if match is not None]


def completion_body(etags: Sequence[str]) -> bytes:
    """Return the request body that completes an upload from its parts' tags, in part order."""
    parts = "".join(
        f'<Part><PartNumber>{number}</PartNumber><ETag>"{etag}"</ETag></Part>'
        for number, etag in enumerate(etags, start=1)
    )
    return f"<CompleteMultipartUpload>{parts}</CompleteMultipartUpload>".encode("ascii")


def check_completion(body: bytes, status: int, etags: Sequence[str]) -> None:
    """Raise StorageFailure unless a 2xx complete reply reports a finished object.

    A complete reply can be 200 and still carry an <Error>; the multipart tag, when the server
    returns one, must be the MD5 of the concatenated part MD5s and the part count.
    """
    text = _text(body)
    if _ERROR_ELEMENT.search(text):
        raise classify_response(status, error_code(body), bucket_probe=False)
    if "<CompleteMultipartUploadResult" not in text:
        raise StorageFailure("storage_error", status=status)
    found = _ETAG_ELEMENT.search(text)
    if found is None:
        return
    reported = _MULTIPART_ETAG.fullmatch(html.unescape(found.group(1)).strip('"').lower())
    if reported is None or not all(_MD5_HEX.fullmatch(etag.lower()) for etag in etags):
        return
    joined = b"".join(bytes.fromhex(etag) for etag in etags)
    expected = hashlib.md5(joined, usedforsecurity=False).hexdigest()
    if reported.group(1) != expected or int(reported.group(2)) != len(etags):
        raise StorageFailure("digest_mismatch", status=status)
