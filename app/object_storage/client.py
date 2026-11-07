"""Async S3-compatible client: SigV4-signed requests over httpx with bounded responses."""

import asyncio
import base64
import hashlib
import os
import re
import ssl
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Final, Self
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from app import netpolicy
from app.logging import get_logger
from app.net_transport import policy_transport
from app.object_storage import addressing
from app.object_storage.addressing import Addressing
from app.object_storage.errors import (
    MAX_ERROR_BODY,
    StorageFailure,
    classify_response,
    error_code,
)
from app.object_storage.keys import validate_key, validate_listing_prefix
from app.object_storage.listing import MAX_LIST_BODY, TOKEN_TEXT, ObjectListing, parse_listing
from app.object_storage.multipart import (
    MAX_MULTIPART_BODY,
    MAX_PARTS,
    UPLOAD_ID,
    UploadPartRange,
    check_completion,
    completion_body,
    normalise_etags,
    parse_etag,
    parse_upload_id,
)
from app.registry_client import sigv4
from app.registry_client.tls import registry_context

type Clock = Callable[[], datetime]

CONNECT_TIMEOUT: Final = 10.0
REQUEST_TIMEOUT: Final = 120.0
MAX_PUT_FILE_BYTES: Final = 4 * 1024**3
MAX_LIST_KEYS: Final = 1000
UPLOAD_CHUNK: Final = 1024 * 1024
UPLOAD_BASE_TIMEOUT: Final = 120.0
UPLOAD_MIN_RATE: Final = 1024 * 1024
UPLOAD_MAX_TIMEOUT: Final = 7200.0
DOWNLOAD_IDLE_TIMEOUT: Final = 120.0

_log = get_logger(__name__)
_SERVICE: Final = "s3"
_ACCESS_KEY_ID: Final = re.compile(r"[A-Za-z0-9._@+=-]{1,128}", re.ASCII)
_CONTENT_TYPE: Final = re.compile(
    r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:; ?[a-z-]+=[A-Za-z0-9._-]+)?", re.ASCII
)
_MD5_BASE64: Final = re.compile(r"[A-Za-z0-9+/]{22}==", re.ASCII)
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}", re.ASCII)
_CONTENT_LENGTH: Final = re.compile(r"[0-9]{1,19}", re.ASCII)
_MAX_CAUSE_DEPTH: Final = 8


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class StorageEndpoint:
    """Where a bucket lives and how to reach it."""

    endpoint_url: str = field(repr=False)
    region: str
    bucket: str
    use_path_style: bool
    verify_tls: bool
    ca_bundle: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class StorageCredentials:
    """An access key pair; neither half appears in a repr."""

    access_key_id: str = field(repr=False)
    secret_access_key: SecretStr = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    body: bytes
    overflow: bool
    headers: httpx.Headers


class ObjectStorageClient:
    """Reads, writes and deletes objects in one bucket; connects only to the configured endpoint."""

    def __init__(
        self,
        endpoint: StorageEndpoint,
        credentials: StorageCredentials,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self._addressing: Addressing = addressing.resolve(
            endpoint_url=endpoint.endpoint_url,
            bucket=endpoint.bucket,
            region=endpoint.region,
            use_path_style=endpoint.use_path_style,
        )
        if (
            _ACCESS_KEY_ID.fullmatch(credentials.access_key_id) is None
            or not credentials.secret_access_key.get_secret_value()
        ):
            raise StorageFailure("refused")
        self._credentials = credentials
        self._region = endpoint.region
        self._clock = clock
        if self._addressing.scheme == "http":
            _log.warning("object_storage_plaintext", extra={"host": self._addressing.host})
        if not endpoint.verify_tls:
            _log.warning("object_storage_tls_unverified", extra={"host": self._addressing.host})
        verify = _tls_context(endpoint, self._addressing)
        if transport is None:
            transport = policy_transport(
                verify=verify, limits=httpx.Limits(max_connections=4, max_keepalive_connections=0)
            )
        self._wire = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
        )

    async def put_object(self, key: str, body: bytes, *, content_type: str) -> None:
        """Store body under key, overwriting any object there; raise StorageFailure otherwise."""
        validate_key(key)
        if _CONTENT_TYPE.fullmatch(content_type) is None:
            raise ValueError("content_type is not a media type")
        digest = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()
        await self._send(
            "PUT",
            self._addressing.path(key),
            {"Content-Type": content_type, "Content-MD5": digest},
            body,
            bucket_probe=False,
        )

    async def head_bucket(self) -> None:
        """Succeed when the bucket exists and the credentials may reach it."""
        await self._send("HEAD", self._addressing.path(None), {}, b"", bucket_probe=True)

    async def put_object_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str,
        size: int,
        md5_b64: str,
        sha256_hex: str,
    ) -> None:
        """Stream a file to key with a declared length and precomputed digests.

        The caller hashed the file; size, md5_b64 and sha256_hex must describe its current bytes.
        """
        validate_key(key)
        if _CONTENT_TYPE.fullmatch(content_type) is None:
            raise ValueError("content_type is not a media type")
        if not 0 <= size <= MAX_PUT_FILE_BYTES:
            raise ValueError("size is outside the allowed range")
        if _MD5_BASE64.fullmatch(md5_b64) is None or _SHA256_HEX.fullmatch(sha256_hex) is None:
            raise ValueError("md5_b64 or sha256_hex is not a digest")
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            if (await asyncio.to_thread(os.fstat, handle.fileno())).st_size != size:
                raise ValueError("size does not match the file")
            await self._send(
                "PUT",
                self._addressing.path(key),
                {
                    "Content-Type": content_type,
                    "Content-MD5": md5_b64,
                    "Content-Length": str(size),
                },
                stream=_FileChunks(handle, size),
                payload_sha256=sha256_hex,
                time_limit=upload_timeout(size),
            )
        finally:
            await asyncio.to_thread(handle.close)

    async def create_multipart_upload(self, key: str, *, content_type: str) -> str:
        """Start a multipart upload to key and return its upload id; the id is never logged."""
        validate_key(key)
        if _CONTENT_TYPE.fullmatch(content_type) is None:
            raise ValueError("content_type is not a media type")
        reply = await self._send(
            "POST",
            self._addressing.path(key),
            {"Content-Type": content_type},
            query="uploads=",
            read_limit=MAX_MULTIPART_BODY,
            whole_body=True,
        )
        return parse_upload_id(reply.body, reply.status)

    async def upload_part(
        self, key: str, upload_id: str, number: int, path: Path, part: UploadPartRange
    ) -> str:
        """Stream one part of a file and return the entity tag the server gave it.

        The caller hashed the file; part must describe the bytes at its offset.
        """
        validate_key(key)
        _check_upload_id(upload_id)
        if not 1 <= number <= MAX_PARTS:
            raise ValueError("number is outside the allowed range")
        if not 1 <= part.size <= MAX_PUT_FILE_BYTES or part.offset < 0:
            raise ValueError("part is outside the allowed range")
        if (
            _MD5_BASE64.fullmatch(part.md5_b64) is None
            or _SHA256_HEX.fullmatch(part.sha256_hex) is None
        ):
            raise ValueError("part digests are not digests")
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            if (
                await asyncio.to_thread(os.fstat, handle.fileno())
            ).st_size < part.offset + part.size:
                raise ValueError("part extends beyond the file")
            await asyncio.to_thread(handle.seek, part.offset)
            reply = await self._send(
                "PUT",
                self._addressing.path(key),
                {"Content-MD5": part.md5_b64, "Content-Length": str(part.size)},
                query=f"partNumber={number}&uploadId={quote(upload_id, safe='')}",
                stream=_FileChunks(handle, part.size),
                payload_sha256=part.sha256_hex,
                time_limit=upload_timeout(part.size),
            )
        finally:
            await asyncio.to_thread(handle.close)
        return parse_etag(reply.headers.get("etag"), reply.status)

    async def complete_multipart_upload(
        self, key: str, upload_id: str, etags: Sequence[str], *, total_bytes: int
    ) -> None:
        """Assemble the uploaded parts, given their entity tags in part order, into the object.

        total_bytes is the size of the whole object; it sets how long the server may take to
        assemble it.
        """
        validate_key(key)
        _check_upload_id(upload_id)
        if not 1 <= len(etags) <= MAX_PARTS:
            raise ValueError("etags is outside the allowed range")
        if total_bytes < 1:
            raise ValueError("total_bytes is outside the allowed range")
        tags = normalise_etags(etags)
        reply = await self._send(
            "POST",
            self._addressing.path(key),
            {"Content-Type": "application/xml"},
            completion_body(tags),
            query=f"uploadId={quote(upload_id, safe='')}",
            read_limit=MAX_MULTIPART_BODY,
            whole_body=True,
            time_limit=upload_timeout(total_bytes),
        )
        check_completion(reply.body, reply.status, tags)

    async def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        """Discard an upload's parts; an upload that is already gone counts as discarded."""
        validate_key(key)
        _check_upload_id(upload_id)
        await self._send(
            "DELETE",
            self._addressing.path(key),
            {},
            query=f"uploadId={quote(upload_id, safe='')}",
            absent_code="NoSuchUpload",
        )

    @asynccontextmanager
    async def get_object(self, key: str, *, max_bytes: int) -> AsyncIterator["ObjectBody"]:
        """Open key for streaming; the body is readable only inside the block, once.

        Raises StorageFailure when the object is missing, has no Content-Length, or is larger
        than max_bytes, all before any body byte is read.
        """
        validate_key(key)
        if max_bytes < 0:
            raise ValueError("max_bytes is negative")
        url, headers = self._signed("GET", self._addressing.path(key), {})
        failure: StorageFailure | None = None
        body: ObjectBody | None = None
        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    response = await stack.enter_async_context(
                        self._wire.stream("GET", url, headers=headers)
                    )
                    body = await _open_body(response, max_bytes)
            except (httpx.HTTPError, TimeoutError, OSError, netpolicy.AddressRefused) as exc:
                failure = _log_transport_failure(exc)
            if failure is not None:
                raise failure
            if body is None:
                raise StorageFailure("storage_error")
            yield body

    async def list_objects(
        self,
        prefix: str,
        *,
        max_keys: int = MAX_LIST_KEYS,
        continuation: str | None = None,
    ) -> ObjectListing:
        """Return one page of the keys under prefix; follow next_token for the rest."""
        validate_listing_prefix(prefix)
        if not 1 <= max_keys <= MAX_LIST_KEYS:
            raise ValueError("max_keys is outside the allowed range")
        if continuation is not None and TOKEN_TEXT.fullmatch(continuation) is None:
            raise ValueError("continuation is not a token this client accepts")
        query = f"list-type=2&prefix={quote(prefix, safe='/')}&max-keys={max_keys}"
        if continuation is not None:
            query += f"&continuation-token={quote(continuation, safe='')}"
        reply = await self._send(
            "GET",
            self._addressing.path(None),
            {},
            query=query,
            read_limit=MAX_LIST_BODY,
            whole_body=True,
        )
        return parse_listing(reply.body, prefix=prefix, sent_token=continuation)

    async def delete_object(self, key: str) -> None:
        """Remove key; an object that is already gone counts as removed."""
        validate_key(key)
        await self._send("DELETE", self._addressing.path(key), {}, absent_code="NoSuchKey")

    async def aclose(self) -> None:
        """Release the connections."""
        await self._wire.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes = b"",
        *,
        bucket_probe: bool = False,
        query: str = "",
        stream: AsyncIterator[bytes] | None = None,
        payload_sha256: str | None = None,
        read_limit: int = MAX_ERROR_BODY,
        whole_body: bool = False,
        absent_code: str | None = None,
        time_limit: float | None = None,
    ) -> _Reply:
        url, sent = self._signed(
            method, path, headers, body, query=query, payload_sha256=payload_sha256
        )
        transport_failure: StorageFailure | None = None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT if time_limit is None else time_limit):
                reply = await self._exchange(
                    method, url, sent, stream if stream is not None else body, read_limit
                )
        except (httpx.HTTPError, TimeoutError, OSError, netpolicy.AddressRefused) as exc:
            transport_failure = _log_transport_failure(exc)
        if transport_failure is not None:
            # Raised outside the handler so the signed request is not kept as the failure's context.
            raise transport_failure
        if 200 <= reply.status < 300:
            if reply.overflow and whole_body:
                raise StorageFailure("storage_error", status=reply.status)
            return reply
        code = error_code(reply.body)
        if absent_code is not None and reply.status == 404 and code == absent_code:
            return reply
        failure = classify_response(reply.status, code, bucket_probe=bucket_probe)
        _log.info(
            "object_storage_request_failed",
            extra={"kind": failure.kind, "status": reply.status, "code": failure.code},
        )
        raise failure

    def _signed(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes = b"",
        *,
        query: str = "",
        payload_sha256: str | None = None,
    ) -> tuple[str, dict[str, str]]:
        host = self._addressing.host
        sent = sigv4.sign_request(
            method=method,
            host=host,
            path=path,
            query=query,
            headers=headers,
            body=body,
            access_key_id=self._credentials.access_key_id,
            secret_access_key=self._credentials.secret_access_key,
            region=self._region,
            service=_SERVICE,
            moment=self._clock(),
            content_sha256_header=True,
            payload_sha256=payload_sha256,
        )
        sent["Host"] = host
        sent["Accept-Encoding"] = "identity"
        return self._addressing.url(path) + (f"?{query}" if query else ""), sent

    async def _exchange(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: bytes | AsyncIterator[bytes],
        read_limit: int,
    ) -> _Reply:
        async with self._wire.stream(method, url, headers=headers, content=content) as response:
            received = bytearray()
            async for chunk in _raw_chunks(response):
                received += chunk[: read_limit + 1 - len(received)]
                if len(received) > read_limit:
                    break
            overflow = len(received) > read_limit
            return _Reply(
                response.status_code, bytes(received[:read_limit]), overflow, response.headers
            )


class ObjectBody:
    """A response body being streamed: its declared size, and one pass over its bytes."""

    def __init__(self, response: httpx.Response, size: int, deadline: float) -> None:
        self.size = size
        self._response = response
        self._deadline = deadline
        self._started = False

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield the raw bytes; fail when more or fewer than `size` arrive or a read stalls."""
        if self._started:
            raise RuntimeError("the body can be read once")
        self._started = True
        loop = asyncio.get_running_loop()
        source = _raw_chunks(self._response)
        received = 0
        while True:
            remaining = self._deadline - loop.time()
            failure: StorageFailure | None = None
            chunk: bytes | None = None
            if remaining <= 0:
                failure = _log_transport_failure(TimeoutError())
            else:
                try:
                    async with asyncio.timeout(min(DOWNLOAD_IDLE_TIMEOUT, remaining)):
                        chunk = await anext(source, None)
                except (httpx.HTTPError, TimeoutError, OSError, netpolicy.AddressRefused) as exc:
                    failure = _log_transport_failure(exc)
            if failure is not None:
                raise failure
            if chunk is None:
                break
            received += len(chunk)
            if received > self.size:
                raise StorageFailure("storage_error")
            if chunk:
                yield chunk
        if received != self.size:
            raise StorageFailure("storage_error")


async def _open_body(response: httpx.Response, max_bytes: int) -> ObjectBody:
    if 200 < response.status_code < 300:
        raise StorageFailure("storage_error", status=response.status_code)
    if response.status_code != 200:
        received = bytearray()
        async for chunk in _raw_chunks(response):
            received += chunk[: MAX_ERROR_BODY - len(received)]
            if len(received) >= MAX_ERROR_BODY:
                break
        code = error_code(bytes(received))
        failure = classify_response(response.status_code, code, bucket_probe=False, reading=True)
        _log.info(
            "object_storage_request_failed",
            extra={"kind": failure.kind, "status": response.status_code, "code": failure.code},
        )
        raise failure
    declared = response.headers.get("content-length", "")
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if _CONTENT_LENGTH.fullmatch(declared) is None or encoding not in ("", "identity"):
        raise StorageFailure("storage_error", status=response.status_code)
    size = int(declared)
    if size > max_bytes:
        raise StorageFailure("storage_error", status=response.status_code)
    return ObjectBody(response, size, asyncio.get_running_loop().time() + upload_timeout(size))


class _FileChunks:
    """Reads exactly `size` bytes of an open file in thread-sized chunks."""

    def __init__(self, handle: BinaryIO, size: int) -> None:
        self._handle = handle
        self._remaining = size

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        if self._remaining <= 0:
            raise StopAsyncIteration
        chunk = await asyncio.to_thread(self._handle.read, min(UPLOAD_CHUNK, self._remaining))
        if not chunk:
            raise StopAsyncIteration
        self._remaining -= len(chunk)
        return chunk


def upload_timeout(size: int) -> float:
    """Return the time allowed for a whole upload of size bytes."""
    return min(size / UPLOAD_MIN_RATE + UPLOAD_BASE_TIMEOUT, UPLOAD_MAX_TIMEOUT)


def _tls_context(endpoint: StorageEndpoint, target: Addressing) -> ssl.SSLContext | bool:
    if target.scheme != "https":
        return False
    try:
        return registry_context(verify_tls=endpoint.verify_tls, ca_bundle=endpoint.ca_bundle)
    except (ssl.SSLError, ValueError):
        raise StorageFailure("tls_failed") from None


async def _raw_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    if response.is_stream_consumed:
        yield response.content
        return
    async for chunk in response.aiter_raw():
        yield chunk


def _check_upload_id(upload_id: str) -> None:
    if UPLOAD_ID.fullmatch(upload_id) is None:
        raise ValueError("upload_id is not an upload id")


def _log_transport_failure(exc: BaseException) -> StorageFailure:
    failure = _transport_failure(exc)
    _log.info(
        "object_storage_request_failed", extra={"kind": failure.kind, "cause": type(exc).__name__}
    )
    return failure


def _transport_failure(exc: BaseException) -> StorageFailure:
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            break
        if isinstance(current, netpolicy.AddressRefused):
            return StorageFailure("address_refused")
        if isinstance(current, ssl.SSLError):
            return StorageFailure("tls_failed")
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return StorageFailure("unreachable")
