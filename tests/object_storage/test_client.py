"""The S3-compatible client: addressing, signing, error classification, caps and secrecy."""

import asyncio
import gzip
import logging
import socket
import ssl
import traceback
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
from pydantic import SecretStr

from app.object_storage import (
    MAX_ERROR_BODY,
    ObjectStorageClient,
    StorageCredentials,
    StorageFailure,
    validate_key,
)
from app.object_storage import client as client_module
from tests.object_storage.support import (
    ACCESS,
    BODY,
    BUCKET,
    CREDENTIALS,
    GZIP,
    KEY,
    NOW,
    SECRET,
    Handler,
    Requests,
    endpoint,
    make_client,
    replay,
    sample,
)
from tests.registry_client.loopback import (
    Certificates,
    LoopbackServer,
    build_certificates,
    closed_port,
    http_reply,
    serve,
    serve_not_tls,
)

STREAM_CHUNK = 4096
LARGE_BODY = 8 * 1024 * 1024
SHORT_TIMEOUT = 0.05
ALL_ALPHABET_KEY = (
    "exports/ABCDEFGHIJKLMNOPQRSTUVWXYZ/abcdefghijklmnopqrstuvwxyz/0123456789/"
    ".dot-_under.x..y/-_.end.csv.gz"
)
ERROR = b"<Error><Code>%s</Code></Error>"


async def put(client: ObjectStorageClient) -> None:
    await client.put_object(KEY, BODY, content_type=GZIP)


def failure_of(raised: pytest.ExceptionInfo[StorageFailure]) -> tuple[str, int | None, str | None]:
    return raised.value.kind, raised.value.status, raised.value.code


class CountingStream(httpx.AsyncByteStream):
    """An endless-looking body that counts how much of it was pulled."""

    def __init__(self, head: bytes, total: int) -> None:
        self.head = head
        self.total = total
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        pending = self.head
        while self.sent < self.total:
            piece = (pending + b"x" * STREAM_CHUNK)[:STREAM_CHUNK]
            pending = b""
            self.sent += len(piece)
            yield piece


class Hang(httpx.AsyncBaseTransport):
    """A transport that never answers."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        return httpx.Response(200)


def raising(error: BaseException) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


def chained(outer: Exception, inner: BaseException) -> Exception:
    outer.__cause__ = inner
    return outer


async def test_put_and_head_are_signed_over_the_headers_a_server_verifies() -> None:
    requests = Requests()
    client = make_client(requests)

    await put(client)
    await client.head_bucket()
    await client.aclose()

    put_request, head_request = requests.seen
    assert (put_request.method, put_request.url.raw_path) == ("PUT", f"/{BUCKET}/{KEY}".encode())
    assert put_request.headers["content-type"] == GZIP
    assert (head_request.method, head_request.url.raw_path) == ("HEAD", f"/{BUCKET}".encode())
    assert put_request.headers["x-amz-date"] == "20261024T142832Z"


@pytest.mark.parametrize("name", ["put-one", "head-bucket"])
async def test_a_captured_success_reply_is_accepted(name: str) -> None:
    client = make_client(replay(name, with_body=False))

    await (put(client) if name == "put-one" else client.head_bucket())

    await client.aclose()


PUT_FAILURES = [
    ("put-wrong-secret", "refused", 403, "SignatureDoesNotMatch"),
    ("put-unknown-key", "refused", 403, "InvalidAccessKeyId"),
    ("put-skewed-date", "clock_skew", 403, "RequestTimeTooSkewed"),
    ("put-wrong-md5", "digest_mismatch", 400, "BadDigest"),
    ("put-malformed-md5", "digest_mismatch", 400, "InvalidDigest"),
    ("put-missing-bucket-scoped", "refused", 403, "AccessDenied"),
    ("put-missing-bucket-unscoped", "refused", 403, "AccessDenied"),
    ("put-missing-bucket-admin", "storage_error", 500, "InternalError"),
    ("put-missing-bucket-admin-noauto", "bucket_missing", 404, "NoSuchBucket"),
    ("put-missing-bucket-unscoped-noauto", "bucket_missing", 404, "NoSuchBucket"),
    ("vhost-put-nodomain-host", "refused", 403, "AccessDenied"),
]


@pytest.mark.parametrize(("name", "kind", "status", "code"), PUT_FAILURES)
async def test_a_captured_put_failure_is_classified_by_its_code_then_its_status(
    name: str, kind: str, status: int, code: str
) -> None:
    client = make_client(replay(name))

    with pytest.raises(StorageFailure) as raised:
        await put(client)

    await client.aclose()
    assert failure_of(raised) == (kind, status, code)
    assert raised.value.message == str(raised.value)


HEAD_FAILURES = [
    ("head-bucket-unknown-key", "refused"),
    ("head-bucket-wrong-secret", "refused"),
    ("head-missing-bucket-scoped", "refused"),
    ("head-missing-bucket-unscoped", "bucket_missing"),
    ("head-missing-bucket-admin-noauto", "bucket_missing"),
]


@pytest.mark.parametrize(("name", "kind"), HEAD_FAILURES)
async def test_a_captured_head_failure_is_classified_from_the_status_alone(
    name: str, kind: str
) -> None:
    client = make_client(replay(name, with_body=False))

    with pytest.raises(StorageFailure) as raised:
        await client.head_bucket()

    await client.aclose()
    assert failure_of(raised) == (kind, sample(name).status, None)


STATUS_AND_BODY = [
    pytest.param(401, b"", "refused", id="401-without-code"),
    pytest.param(403, ERROR % b"Weird", "refused", id="403-unknown-code"),
    pytest.param(429, ERROR % b"SlowDown", "storage_error", id="429"),
    pytest.param(503, b"", "storage_error", id="503"),
    pytest.param(404, ERROR % b"NoSuchKey", "storage_error", id="404-no-such-key"),
    pytest.param(404, b"", "storage_error", id="404-put-without-code"),
    pytest.param(400, ERROR % b"RequestTimeTooSkewed", "clock_skew", id="code-beats-status-skew"),
    pytest.param(403, ERROR % b"BadDigest", "digest_mismatch", id="code-beats-status-digest"),
    pytest.param(500, ERROR % b"NoSuchBucket", "bucket_missing", id="code-beats-status-bucket"),
    pytest.param(403, ERROR % b"Bad Code", "refused", id="code-with-a-space-ignored"),
    pytest.param(500, ERROR % (b"A" * 65), "storage_error", id="code-longer-than-64-ignored"),
]


@pytest.mark.parametrize(("status", "body", "kind"), STATUS_AND_BODY)
async def test_the_status_decides_only_when_no_recognised_code_does(
    status: int, body: bytes, kind: str
) -> None:
    client = make_client(lambda request: httpx.Response(status, content=body))

    with pytest.raises(StorageFailure) as raised:
        await put(client)

    assert raised.value.kind == kind


async def test_a_plain_text_400_from_a_tls_port_is_a_storage_error_without_a_code() -> None:
    reply = b"Client sent an HTTP request to an HTTPS server."
    client = make_client(
        lambda request: httpx.Response(400, content=reply, headers={"content-type": "text/plain"})
    )

    with pytest.raises(StorageFailure) as raised:
        await put(client)

    assert failure_of(raised) == ("storage_error", 400, None)


@pytest.mark.parametrize("status", [200, 201, 204])
async def test_any_2xx_reply_is_success(status: int) -> None:
    client = make_client(lambda request: httpx.Response(status))

    await put(client)


ADDRESSING = [
    pytest.param(
        "http://127.0.0.1:29000",
        True,
        "dockmin-e2e",
        f"http://127.0.0.1:29000/dockmin-e2e/{KEY}",
        "http://127.0.0.1:29000/dockmin-e2e",
        id="path-style-ip",
    ),
    pytest.param(
        "https://S3.Example.com:443/",
        True,
        "bkt",
        f"https://s3.example.com/bkt/{KEY}",
        "https://s3.example.com/bkt",
        id="path-style-default-port-and-case",
    ),
    pytest.param(
        "http://s3.example.com:80",
        True,
        "bkt",
        f"http://s3.example.com/bkt/{KEY}",
        "http://s3.example.com/bkt",
        id="path-style-default-http-port",
    ),
    pytest.param(
        "https://s3.example.com:9443",
        True,
        "bkt",
        f"https://s3.example.com:9443/bkt/{KEY}",
        "https://s3.example.com:9443/bkt",
        id="path-style-other-port",
    ),
    pytest.param(
        "http://[::1]:9000",
        True,
        "bkt",
        f"http://[::1]:9000/bkt/{KEY}",
        "http://[::1]:9000/bkt",
        id="path-style-ipv6",
    ),
    pytest.param(
        "http://minio:9000",
        True,
        "my.bucket",
        f"http://minio:9000/my.bucket/{KEY}",
        "http://minio:9000/my.bucket",
        id="path-style-dotted-bucket",
    ),
    pytest.param(
        "http://s3.localhost:29000",
        False,
        "dockmin-e2e",
        f"http://dockmin-e2e.s3.localhost:29000/{KEY}",
        "http://dockmin-e2e.s3.localhost:29000/",
        id="virtual-host",
    ),
    pytest.param(
        "https://s3.example.com:8443/",
        False,
        "bkt",
        f"https://bkt.s3.example.com:8443/{KEY}",
        "https://bkt.s3.example.com:8443/",
        id="virtual-host-other-port",
    ),
]


@pytest.mark.parametrize(("url", "path_style", "bucket", "put_url", "head_url"), ADDRESSING)
async def test_the_url_follows_the_addressing_style_and_drops_a_default_port(
    url: str, path_style: bool, bucket: str, put_url: str, head_url: str
) -> None:
    requests = Requests()
    client = make_client(requests, endpoint(url, path_style=path_style, bucket=bucket))

    await put(client)
    await client.head_bucket()

    assert [str(request.url) for request in requests.seen] == [put_url, head_url]


async def test_a_virtual_host_put_and_a_path_style_head_match_the_captured_requests() -> None:
    vhost_put = sample("signed-vhost-put")
    path_head = sample("signed-head-bucket")
    vhost = Requests()
    path_style = Requests()

    await put(make_client(vhost, endpoint("http://s3.localhost:29000", path_style=False)))
    await make_client(path_style).head_bucket()

    [sent_put] = vhost.seen
    [sent_head] = path_style.seen
    assert f"{sent_put.method} {sent_put.url.raw_path.decode()}" == vhost_put.request_line
    assert sent_put.headers["host"] == vhost_put.host
    assert f"{sent_head.method} {sent_head.url.raw_path.decode()}" == path_head.request_line
    assert sent_head.headers["host"] == path_head.host


BAD_ENDPOINTS = [
    "",
    "ftp://host",
    "host:9000",
    "//host",
    "http://",
    "http://user@host",
    "http://user:pw@host",
    "http://host?x=1",
    "http://host#frag",
    "http://host/minio",
    "http://host/a/b",
    "http://host:0",
    "http://host:99999",
    "http://host:abc",
    "http://ho st",
    "http://hóst",
    "http://host\\x",
    "http://-bad.example",
    "http://127.1",
    "http://bad..dots",
    "http://[::1",
    "http://a" + "b" * 300,
    "HTTP:/host",
    "http://host\n",
    "http://host.example.com.",
    "http://ho$t",
    "http://" + "x" * 2100,
]


@pytest.mark.parametrize("url", BAD_ENDPOINTS)
def test_an_endpoint_that_is_not_a_plain_host_address_is_invalid_and_not_echoed(url: str) -> None:
    with pytest.raises(StorageFailure) as raised:
        ObjectStorageClient(endpoint(url), CREDENTIALS)

    assert raised.value.kind == "endpoint_invalid"
    assert url.strip() == "" or url not in raised.value.message


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:9000", "https://[::1]", "http://10.0.0.5", "https://[2001:db8::1]:9443"],
)
def test_virtual_host_addressing_with_an_ip_host_is_invalid(url: str) -> None:
    with pytest.raises(StorageFailure) as raised:
        ObjectStorageClient(endpoint(url, path_style=False), CREDENTIALS)

    assert raised.value.kind == "endpoint_invalid"


BAD_BUCKETS = [
    pytest.param("", True, id="empty"),
    pytest.param("a/b", True, id="slash"),
    pytest.param("..", True, id="dots"),
    pytest.param(".x", True, id="leading-dot"),
    pytest.param("b ucket", True, id="space"),
    pytest.param("buc\nket", True, id="newline"),
    pytest.param("é", True, id="non-ascii"),
    pytest.param("Upper", False, id="vhost-upper"),
    pytest.param("a.b", False, id="vhost-dot"),
    pytest.param("ab", False, id="vhost-too-short"),
    pytest.param("-ab", False, id="vhost-leading-dash"),
    pytest.param("ab-", False, id="vhost-trailing-dash"),
    pytest.param("a" * 64, False, id="vhost-too-long"),
    pytest.param("x_y", False, id="vhost-underscore"),
]


@pytest.mark.parametrize(("bucket", "path_style"), BAD_BUCKETS)
def test_a_bucket_name_the_style_cannot_carry_is_invalid(bucket: str, path_style: bool) -> None:
    target = endpoint("https://s3.example.com", path_style=path_style, bucket=bucket)

    with pytest.raises(StorageFailure) as raised:
        ObjectStorageClient(target, CREDENTIALS)

    assert raised.value.kind == "endpoint_invalid"


@pytest.mark.parametrize("region", ["", "a b", "us/east", "-x", "x" * 64, "é", "a\n"])
def test_a_region_outside_the_credential_scope_alphabet_is_invalid(region: str) -> None:
    with pytest.raises(StorageFailure) as raised:
        ObjectStorageClient(endpoint(region=region), CREDENTIALS)

    assert raised.value.kind == "endpoint_invalid"


@pytest.mark.parametrize("region", ["us-east-1", "auto", "default", "minio", "cn-north-1"])
async def test_the_region_is_signed_into_the_credential_scope(region: str) -> None:
    requests = Requests()

    await make_client(requests, endpoint(region=region)).head_bucket()

    assert f"/{region}/s3/aws4_request" in requests.seen[0].headers["authorization"]


@pytest.mark.parametrize(
    ("key_id", "secret"),
    [("", "s"), ("a/b", "s"), ("a b", "s"), ("a\nb", "s"), ("x" * 129, "s"), (ACCESS, "")],
)
def test_credentials_that_cannot_be_signed_are_refused_before_any_request(
    key_id: str, secret: str
) -> None:
    with pytest.raises(StorageFailure) as raised:
        ObjectStorageClient(endpoint(), StorageCredentials(key_id, SecretStr(secret)))

    assert raised.value.kind == "refused"


GOOD_KEYS = [
    KEY,
    "a",
    "a/b",
    "ABCxyz/0123456789/.dot-_under.x..y/-_.end.csv.gz",
    "..x/y..",
    "a" * 1024,
]
BAD_KEYS = [
    "",
    "/",
    "/a",
    "a/",
    "a//b",
    ".",
    "..",
    "a/./b",
    "a/../b",
    "./a",
    "a/..",
    "a b",
    "a%2Fb",
    "a?b",
    "a#b",
    "a+b",
    "é",
    "a\nb",
    "a\x00b",
    "a\\b",
    "a" * 1025,
    "a:b",
    "a*b",
    "a\n",
    "~a",
    "a/~",
]


@pytest.mark.parametrize("key", GOOD_KEYS)
def test_a_key_in_the_allowed_alphabet_is_returned_unchanged(key: str) -> None:
    assert validate_key(key) == key


@pytest.mark.parametrize("key", BAD_KEYS)
def test_a_key_outside_the_alphabet_or_with_a_dot_segment_is_invalid(key: str) -> None:
    with pytest.raises(StorageFailure) as raised:
        validate_key(key)

    assert raised.value.kind == "key_invalid"


async def test_a_put_with_an_invalid_key_sends_nothing() -> None:
    requests = Requests()

    with pytest.raises(StorageFailure) as raised:
        await make_client(requests).put_object("../x", BODY, content_type=GZIP)

    assert raised.value.kind == "key_invalid"
    assert requests.seen == []


async def test_a_key_using_every_allowed_character_is_sent_without_encoding() -> None:
    requests = Requests()

    await make_client(requests).put_object(ALL_ALPHABET_KEY, BODY, content_type=GZIP)

    assert requests.seen[0].url.raw_path == f"/{BUCKET}/{ALL_ALPHABET_KEY}".encode()


@pytest.mark.parametrize("content_type", ["", "gzip", "a/b\r\nX: y", "a/b c", "text/csv; x y"])
async def test_a_content_type_that_is_not_a_media_type_is_a_value_error(content_type: str) -> None:
    client = make_client(lambda request: httpx.Response(200))

    with pytest.raises(ValueError, match="media type"):
        await client.put_object(KEY, BODY, content_type=content_type)


@pytest.mark.parametrize(
    "content_type", [GZIP, "text/csv", "text/csv; charset=utf-8", "application/octet-stream"]
)
async def test_a_media_type_is_sent_as_given(content_type: str) -> None:
    requests = Requests()

    await make_client(requests).put_object(KEY, BODY, content_type=content_type)

    assert requests.seen[0].headers["content-type"] == content_type


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_not_followed_and_its_target_is_not_reported(status: int) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            status,
            headers={"location": "http://evil.example/steal"},
            content=ERROR % b"PermanentRedirect",
        )

    with pytest.raises(StorageFailure) as raised:
        await put(make_client(handler))

    assert failure_of(raised) == ("storage_error", status, "PermanentRedirect")
    assert len(calls) == 1
    assert "evil" not in str(raised.value)


async def test_the_client_ignores_proxy_and_certificate_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")

    client = ObjectStorageClient(endpoint("https://s3.example.com"), CREDENTIALS)

    assert (client._wire.trust_env, client._wire.follow_redirects) == (False, False)
    await client.aclose()


async def test_an_error_body_is_read_only_to_the_cap_and_a_code_before_the_cap_is_found() -> None:
    stream = CountingStream(b"<Error><Code>AccessDenied</Code>", LARGE_BODY)

    with pytest.raises(StorageFailure) as raised:
        await put(make_client(lambda request: httpx.Response(403, stream=stream)))

    assert failure_of(raised) == ("refused", 403, "AccessDenied")
    assert stream.sent <= MAX_ERROR_BODY + STREAM_CHUNK


async def test_a_code_after_the_cap_is_not_read() -> None:
    padded = b"x" * MAX_ERROR_BODY + b"<Code>AccessDenied</Code>"

    with pytest.raises(StorageFailure) as raised:
        await put(make_client(lambda request: httpx.Response(500, content=padded)))

    assert failure_of(raised) == ("storage_error", 500, None)


async def test_a_large_success_body_is_not_read_to_the_end() -> None:
    stream = CountingStream(b"", LARGE_BODY)

    await put(make_client(lambda request: httpx.Response(200, stream=stream)))

    assert stream.sent <= MAX_ERROR_BODY + STREAM_CHUNK


async def test_an_encoded_error_body_is_not_decompressed() -> None:
    packed = gzip.compress(ERROR % b"AccessDenied")

    class Packed(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield packed

    client = make_client(
        lambda request: httpx.Response(500, stream=Packed(), headers={"content-encoding": "gzip"})
    )

    with pytest.raises(StorageFailure) as raised:
        await put(client)

    assert failure_of(raised) == ("storage_error", 500, None)


TRANSPORT_FAILURES = [
    pytest.param(
        chained(httpx.ConnectError("x"), ssl.SSLCertVerificationError("v")), "tls_failed", id="cert"
    ),
    pytest.param(chained(httpx.ConnectError("x"), ssl.SSLError("w")), "tls_failed", id="ssl"),
    pytest.param(
        chained(httpx.ConnectError("x"), chained(OSError("mid"), ssl.SSLError("deep"))),
        "tls_failed",
        id="ssl-two-causes-down",
    ),
    pytest.param(chained(httpx.ReadError("x"), ssl.SSLError("eof")), "tls_failed", id="read-ssl"),
    pytest.param(
        chained(httpx.ConnectError("x"), ConnectionRefusedError("r")), "unreachable", id="refused"
    ),
    pytest.param(httpx.ConnectError("no cause"), "unreachable", id="no-cause"),
    pytest.param(httpx.ReadTimeout("slow"), "unreachable", id="read-timeout"),
    pytest.param(httpx.ConnectTimeout("slow"), "unreachable", id="connect-timeout"),
    pytest.param(httpx.RemoteProtocolError("closed"), "unreachable", id="protocol"),
    pytest.param(OSError("boom"), "unreachable", id="os-error"),
]


@pytest.mark.parametrize(("error", "kind"), TRANSPORT_FAILURES)
async def test_a_transport_failure_is_tls_failed_only_when_an_ssl_error_is_in_its_causes(
    error: Exception, kind: str
) -> None:
    with pytest.raises(StorageFailure) as raised:
        await put(make_client(raising(error)))

    assert failure_of(raised) == (kind, None, None)


async def test_a_cause_cycle_terminates() -> None:
    first, second = httpx.ConnectError("a"), OSError("b")
    first.__cause__, second.__cause__ = second, first

    with pytest.raises(StorageFailure) as raised:
        await make_client(raising(first)).head_bucket()

    assert raised.value.kind == "unreachable"


async def test_a_request_that_never_answers_ends_at_the_total_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT", SHORT_TIMEOUT)
    client = ObjectStorageClient(endpoint(), CREDENTIALS, transport=Hang(), clock=lambda: NOW)

    with pytest.raises(StorageFailure) as raised:
        await client.head_bucket()

    assert raised.value.kind == "unreachable"


async def test_cancelling_a_request_propagates_the_cancellation() -> None:
    client = ObjectStorageClient(endpoint(), CREDENTIALS, transport=Hang(), clock=lambda: NOW)
    task = asyncio.create_task(client.head_bucket())
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_connect_and_total_timeouts_are_configured() -> None:
    client = ObjectStorageClient(endpoint(), CREDENTIALS)

    timeout = client._wire.timeout
    assert (timeout.connect, timeout.read) == (client_module.CONNECT_TIMEOUT, 120.0)
    assert client_module.REQUEST_TIMEOUT == 120.0


def assert_without_credentials(text: str) -> None:
    assert SECRET not in text
    assert ACCESS not in text
    assert "sEcReT" not in text


SECRET_LEAKS: list[Handler] = [
    raising(httpx.ConnectError(f"leak {SECRET} {ACCESS}")),
    raising(chained(httpx.ConnectError("x"), ssl.SSLError(f"leak {SECRET}"))),
    lambda request: httpx.Response(
        403,
        content=(
            f"<Error><Code>AccessDenied</Code><Message>{SECRET} {ACCESS} "
            f"{request.headers['authorization']}</Message></Error>"
        ).encode(),
    ),
    lambda request: httpx.Response(500, content=SECRET.encode()),
    lambda request: httpx.Response(307, headers={"location": f"http://x/{SECRET}"}),
]


@pytest.mark.parametrize("handler", SECRET_LEAKS, ids=range(len(SECRET_LEAKS)))
async def test_neither_credential_reaches_an_exception_a_traceback_or_a_log(
    handler: Handler, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(StorageFailure) as raised:
        await put(make_client(handler))

    assert_without_credentials(
        "".join(traceback.format_exception(raised.value))
        + repr(raised.value)
        + repr(vars(raised.value))
        + repr(caplog.records)
        + "".join(record.getMessage() for record in caplog.records)
    )


def test_the_endpoint_the_credentials_and_the_client_hide_their_values_in_a_repr() -> None:
    client = ObjectStorageClient(endpoint(), CREDENTIALS)

    shown = repr(CREDENTIALS) + repr(endpoint()) + repr(client)

    assert_without_credentials(shown)
    assert "127.0.0.1" not in shown


def test_an_endpoint_with_a_user_name_does_not_echo_it(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    target = endpoint("https://admin:hunter2@s3.example.com")

    with pytest.raises(StorageFailure) as raised:
        ObjectStorageClient(target, CREDENTIALS)

    shown = repr(raised.value) + "".join(traceback.format_exception(raised.value)) + repr(target)
    assert "hunter2" not in shown + repr(caplog.records)
    assert "admin" not in shown + repr(caplog.records)


async def test_a_transport_failure_is_raised_without_the_signed_request_as_its_context() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with make_client(refuse) as client:
        with pytest.raises(StorageFailure) as raised:
            await put(client)

    assert (raised.value.__context__, raised.value.__cause__) == (None, None)


def test_a_target_that_does_not_verify_certificates_is_warned_about_at_each_build(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    for verify in (False, False, True):
        ObjectStorageClient(endpoint("https://s3.example.com", verify=verify), CREDENTIALS)

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["object_storage_tls_unverified"] * 2
    assert {record.levelno for record in caplog.records} == {logging.WARNING}


@pytest.mark.parametrize(
    ("url", "logged"), [("http://127.0.0.1:29000", True), ("https://s3.example.com", False)]
)
def test_a_plain_http_endpoint_is_warned_about_and_an_https_one_is_not(
    caplog: pytest.LogCaptureFixture, url: str, logged: bool
) -> None:
    caplog.set_level(logging.DEBUG)

    ObjectStorageClient(endpoint(url), CREDENTIALS)

    assert ("object_storage_plaintext" in [r.getMessage() for r in caplog.records]) is logged


@pytest.fixture(scope="module")
def certificates() -> Certificates:
    return build_certificates()


@pytest.fixture
async def secure(certificates: Certificates) -> AsyncIterator[LoopbackServer]:
    server = await serve(lambda request: http_reply(200), identity=certificates.server)
    yield server
    await server.close()


@pytest.fixture
async def plain() -> AsyncIterator[LoopbackServer]:
    server = await serve_not_tls(b"Client sent an HTTP request to an HTTPS server.")
    yield server
    await server.close()


@pytest.mark.usefixtures("permit_loopback")
async def test_a_private_ca_given_as_a_bundle_is_trusted_for_put_and_head(
    secure: LoopbackServer, certificates: Certificates
) -> None:
    target = endpoint(f"https://127.0.0.1:{secure.port}", ca=certificates.ca_pem)

    async with ObjectStorageClient(target, CREDENTIALS) as client:
        await put(client)
        await client.head_bucket()

    assert secure.requests[0].header("host") == f"127.0.0.1:{secure.port}"


@pytest.mark.usefixtures("permit_loopback")
async def test_a_target_that_does_not_verify_certificates_connects_to_an_untrusted_server(
    secure: LoopbackServer,
) -> None:
    target = endpoint(f"https://127.0.0.1:{secure.port}", verify=False)

    async with ObjectStorageClient(target, CREDENTIALS) as client:
        await client.head_bucket()

    assert len(secure.requests) == 1


UNVERIFIABLE = {
    "no-ca": lambda server, certificates: endpoint(f"https://127.0.0.1:{server.port}"),
    "wrong-ca": lambda server, certificates: endpoint(
        f"https://127.0.0.1:{server.port}", ca=certificates.rogue_ca_pem
    ),
}


@pytest.mark.parametrize("case", UNVERIFIABLE)
@pytest.mark.usefixtures("permit_loopback")
async def test_a_server_the_ca_does_not_vouch_for_is_a_tls_failure(
    secure: LoopbackServer, certificates: Certificates, case: str
) -> None:
    target = UNVERIFIABLE[case](secure, certificates)

    async with ObjectStorageClient(target, CREDENTIALS) as client:
        with pytest.raises(StorageFailure) as raised:
            await client.head_bucket()

    assert raised.value.kind == "tls_failed"


@pytest.mark.usefixtures("permit_loopback")
async def test_https_to_a_port_that_speaks_plain_http_is_a_tls_failure(
    plain: LoopbackServer,
) -> None:
    async with ObjectStorageClient(endpoint(f"https://127.0.0.1:{plain.port}"), CREDENTIALS) as c:
        with pytest.raises(StorageFailure) as raised:
            await c.head_bucket()

    assert raised.value.kind == "tls_failed"


@pytest.mark.usefixtures("permit_loopback")
async def test_a_closed_port_is_unreachable() -> None:
    async with ObjectStorageClient(endpoint(f"http://127.0.0.1:{closed_port()}"), CREDENTIALS) as c:
        with pytest.raises(StorageFailure) as raised:
            await c.head_bucket()

    assert raised.value.kind == "unreachable"


def test_a_ca_bundle_that_is_not_a_certificate_is_a_tls_failure_only_over_https() -> None:
    with pytest.raises(StorageFailure) as raised:
        ObjectStorageClient(endpoint("https://s3.example.com", ca="not a certificate"), CREDENTIALS)

    assert raised.value.kind == "tls_failed"
    ObjectStorageClient(endpoint("http://s3.example.com", ca="not a certificate"), CREDENTIALS)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "[::1]", "169.254.169.254", "0.0.0.0", "[::ffff:127.0.0.1]"],
)
async def test_an_internal_endpoint_is_refused_at_connect_with_the_permit_named(host: str) -> None:
    async with ObjectStorageClient(endpoint(f"http://{host}:{closed_port()}"), CREDENTIALS) as c:
        with pytest.raises(StorageFailure) as raised:
            await c.head_bucket()

    assert raised.value.kind == "address_refused"
    assert "DOCKMIN_PERMITTED_INTERNAL_NETWORKS" in str(raised.value)


async def test_a_refused_endpoint_is_never_dialled() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.setblocking(False)
    try:
        async with ObjectStorageClient(
            endpoint(f"http://127.0.0.1:{listener.getsockname()[1]}"), CREDENTIALS
        ) as c:
            with pytest.raises(StorageFailure):
                await c.head_bucket()
        with pytest.raises(BlockingIOError):
            listener.accept()
    finally:
        listener.close()


async def test_a_name_that_resolves_to_an_internal_address_is_refused(
    fake_hosts: dict[str, str],
) -> None:
    fake_hosts["storage.internal.example"] = "169.254.169.254"

    async with ObjectStorageClient(
        endpoint("http://storage.internal.example:9000"), CREDENTIALS
    ) as c:
        with pytest.raises(StorageFailure) as raised:
            await c.head_bucket()

    assert raised.value.kind == "address_refused"


async def test_a_permitted_internal_endpoint_is_dialled_and_fails_as_a_plain_connection_error(
    address_policy: Callable[..., None],
) -> None:
    address_policy("127.0.0.0/8")

    async with ObjectStorageClient(endpoint(f"http://127.0.0.1:{closed_port()}"), CREDENTIALS) as c:
        with pytest.raises(StorageFailure) as raised:
            await c.head_bucket()

    assert raised.value.kind == "unreachable"
