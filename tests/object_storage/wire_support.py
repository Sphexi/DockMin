"""A recording bucket stub for requests of any method, with a signature check that fits them all."""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
from pydantic import SecretStr

from app.object_storage import ObjectStorageClient
from app.registry_client import sigv4
from tests.object_storage.support import (
    ACCESS,
    CREDENTIALS,
    NOW,
    SECRET,
    SIGNATURE,
    endpoint,
)

UNSIGNED = ("host", "x-amz-date", "x-amz-content-sha256")
XML = {"content-type": "application/xml"}

type Reply = httpx.Response | Exception
type Answer = Callable[["Seen"], Reply] | Reply


@dataclass(frozen=True, slots=True)
class Seen:
    """One request the stub received, with its body read."""

    method: str
    path: str
    query: dict[str, list[str]]
    raw_query: str
    headers: httpx.Headers
    body: bytes


def check_signature(request: httpx.Request, body: bytes) -> None:
    """Re-sign the request as the server would and assert the signature and payload hash agree."""
    found = SIGNATURE.fullmatch(request.headers["authorization"])
    assert found, request.headers["authorization"]
    _key, _date, region, signed, _signature = found.groups()
    names = signed.split(";")
    streamed = "content-length" in names
    declared = request.headers["x-amz-content-sha256"]
    assert declared == hashlib.sha256(body).hexdigest()
    stamp = request.headers["x-amz-date"]
    again = sigv4.sign_request(
        method=request.method,
        host=request.headers["host"],
        path=request.url.raw_path.split(b"?")[0].decode(),
        query=request.url.query.decode(),
        headers={name: request.headers[name] for name in names if name not in UNSIGNED},
        body=b"" if streamed else body,
        access_key_id=ACCESS,
        secret_access_key=SecretStr(SECRET),
        region=region,
        service="s3",
        moment=datetime.strptime(stamp, sigv4.AMZ_DATE_FORMAT).replace(tzinfo=UTC),
        content_sha256_header=True,
        payload_sha256=declared if streamed else None,
    )
    assert again["Authorization"] == request.headers["authorization"]


class Wire:
    """Answers each request, in order, with the next answer; the last answer repeats."""

    def __init__(self, *answers: Answer) -> None:
        self.answers = list(answers) or [httpx.Response(200)]
        self.seen: list[Seen] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        check_signature(request, body)
        seen = Seen(
            request.method,
            request.url.path,
            parse_qs(request.url.query.decode(), keep_blank_values=True),
            request.url.query.decode(),
            request.headers,
            body,
        )
        self.seen.append(seen)
        answer = self.answers[min(len(self.seen), len(self.answers)) - 1]
        reply = answer(seen) if callable(answer) else answer
        if isinstance(reply, Exception):
            raise reply
        return reply


def error_reply(status: int, code: str) -> httpx.Response:
    body = f'<?xml version="1.0"?><Error><Code>{code}</Code><Message>m</Message></Error>'
    return httpx.Response(status, content=body.encode(), headers=XML)


def client_for(wire: "Wire") -> ObjectStorageClient:
    return ObjectStorageClient(
        endpoint(), CREDENTIALS, transport=httpx.MockTransport(wire), clock=lambda: NOW
    )
