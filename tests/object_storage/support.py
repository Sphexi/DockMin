"""Builders and a signature-checking fake for the object storage tests."""

import base64
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import SecretStr

from app.object_storage import ObjectStorageClient, StorageCredentials, StorageEndpoint
from app.registry_client import sigv4

SAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "object_storage"
NOW = datetime(2026, 10, 24, 14, 28, 32, tzinfo=UTC)
ACCESS = "AKIDEXAMPLE0123456789"
SECRET = "sEcReT-marker/k8vQ3zLwPq91xYtR+7"
BUCKET = "dockmin-e2e"
ENDPOINT = "http://127.0.0.1:29000"
REGION = "us-east-1"
KEY = "exports/rule-1/2026/01/01/00000000000000000001.csv.gz"
GZIP = "application/gzip"
BODY = b"\x1f\x8b" + b"payload-bytes" * 5
CREDENTIALS = StorageCredentials(ACCESS, SecretStr(SECRET))
SIGNATURE = re.compile(
    r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request, "
    r"SignedHeaders=([a-z0-9;-]+), Signature=([0-9a-f]{64})"
)
PUT_HEADERS = ["content-md5", "content-type", "host", "x-amz-content-sha256", "x-amz-date"]
STREAM_HEADERS = [
    "content-length",
    "content-md5",
    "content-type",
    "host",
    "x-amz-content-sha256",
    "x-amz-date",
]
HEAD_HEADERS = ["host", "x-amz-content-sha256", "x-amz-date"]
UNSIGNED = ("host", "x-amz-date", "x-amz-content-sha256")

type Handler = Callable[[httpx.Request], httpx.Response]


def endpoint(
    url: str = ENDPOINT,
    *,
    path_style: bool = True,
    bucket: str = BUCKET,
    region: str = REGION,
    verify: bool = True,
    ca: str | None = None,
) -> StorageEndpoint:
    return StorageEndpoint(url, region, bucket, path_style, verify, ca)


def make_client(handler: Handler, target: StorageEndpoint | None = None) -> ObjectStorageClient:
    return ObjectStorageClient(
        target or endpoint(),
        CREDENTIALS,
        transport=httpx.MockTransport(handler),
        clock=lambda: NOW,
    )


def verify_signature(request: httpx.Request, access: str = ACCESS, secret: str = SECRET) -> None:
    """Re-sign the request as the server would and assert every integrity header agrees."""
    header = request.headers["authorization"]
    found = SIGNATURE.fullmatch(header)
    assert found, header
    key_id, date, region, signed, _signature = found.groups()
    assert key_id == access
    names = signed.split(";")
    stamp = request.headers["x-amz-date"]
    assert stamp.startswith(date)
    streamed = request.method == "PUT" and "content-length" in names
    if request.method == "PUT":
        assert names == (STREAM_HEADERS if streamed else PUT_HEADERS)
    else:
        assert names == HEAD_HEADERS
    body = request.content
    declared = request.headers["x-amz-content-sha256"]
    assert declared == hashlib.sha256(body).hexdigest()
    if request.method == "PUT":
        expected_md5 = base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest())
        assert request.headers["content-md5"] == expected_md5.decode()
    if streamed:
        assert request.headers["content-length"] == str(len(body))
        assert "transfer-encoding" not in request.headers
    host = request.headers["host"]
    assert host == request.url.netloc.decode()
    assert not host.endswith((":80", ":443"))
    extra = {name: request.headers[name] for name in names if name not in UNSIGNED}
    moment = datetime.strptime(stamp, sigv4.AMZ_DATE_FORMAT).replace(tzinfo=UTC)
    again = sigv4.sign_request(
        method=request.method,
        host=host,
        path=request.url.raw_path.split(b"?")[0].decode(),
        query=request.url.query.decode(),
        headers=extra,
        body=b"" if streamed else body,
        access_key_id=access,
        secret_access_key=SecretStr(secret),
        region=region,
        service="s3",
        moment=moment,
        content_sha256_header=True,
        payload_sha256=declared if streamed else None,
    )
    assert again["Authorization"] == header
    assert request.headers["accept-encoding"] == "identity"
    assert secret not in str(request.headers)


@dataclass(frozen=True, slots=True)
class Sample:
    """A captured server reply: its status, content type and body."""

    status: int
    content_type: str | None
    body: bytes
    request_line: str
    host: str


def sample(name: str) -> Sample:
    """Read a captured exchange: the request line and Host as sent, the reply as received."""
    text = (SAMPLES / f"{name}.http").read_text(encoding="utf-8", errors="replace")
    request, _, reply = text.partition("\n\n")
    content_type = re.search(r"(?im)^content-type: (.*)$", reply)
    body = SAMPLES / f"{name}.body"
    return Sample(
        status=int(reply.split(" ", 2)[1]),
        content_type=content_type.group(1).strip() if content_type else None,
        body=body.read_bytes() if body.exists() else b"",
        request_line=request.splitlines()[0].rsplit(" ", 1)[0],
        host=re.search(r"(?im)^host: (.*)$", request).group(1).strip(),  # type: ignore[union-attr]
    )


def replay(name: str, *, with_body: bool = True) -> Handler:
    """Answer every request, after checking its signature, with a captured reply."""
    captured = sample(name)
    headers = {"content-type": captured.content_type} if captured.content_type else {}

    def handler(request: httpx.Request) -> httpx.Response:
        verify_signature(request)
        return httpx.Response(
            captured.status, content=captured.body if with_body else b"", headers=headers
        )

    return handler


class Requests:
    """A handler that checks each signature, keeps each request and answers 200."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        verify_signature(request)
        self.seen.append(request)
        return httpx.Response(200)


def _shape(
    method: str, target: str, host: str
) -> tuple[str, str, list[tuple[str, list[str]]], str]:
    parts = urlsplit(target)
    query = sorted(parse_qs(parts.query, keep_blank_values=True).items())
    return method, parts.path, query, host


class Script:
    """Answers each request in turn with the captured reply whose request it must match."""

    def __init__(self, *names: str) -> None:
        self.samples = [sample(name) for name in names]
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        verify_signature(request)
        captured = self.samples[len(self.seen)]
        method, target = captured.request_line.split(" ", 1)
        sent = _shape(
            request.method,
            request.url.raw_path.decode(),
            request.headers["host"],
        )
        assert sent == _shape(method, target, captured.host)
        self.seen.append(request)
        headers = {"content-type": captured.content_type} if captured.content_type else {}
        return httpx.Response(captured.status, content=captured.body, headers=headers)
