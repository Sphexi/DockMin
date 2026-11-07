"""Multipart upload grammars, reply checks and the four client calls against a stub bucket."""

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.backup.archive import hash_file
from app.backup.parts import PartDigest
from app.object_storage import StorageFailure, multipart
from app.object_storage import client as client_module
from tests.object_storage.support import BUCKET
from tests.object_storage.wire_support import XML, Wire, client_for, error_reply

KEY = "backups/rule-1/dockmin-backup-20260101T000000Z.tar.gz"
GZIP = "application/gzip"
UPLOAD_ID = "VXBsb2FkSWQ.-_~+/="
PART_BYTES = 4096
FILE_BYTES = 2 * PART_BYTES + 100
HTTP_ERROR = 500
SECOND_PART = 2
PART_COUNT = 3

INITIATED = (
    '<?xml version="1.0"?><InitiateMultipartUploadResult xmlns="x">'
    "<Bucket>b</Bucket><Key>k</Key><UploadId>{}</UploadId></InitiateMultipartUploadResult>"
)
COMPLETED = (
    '<?xml version="1.0"?><CompleteMultipartUploadResult xmlns="x">'
    "<Location>l</Location><ETag>{}</ETag></CompleteMultipartUploadResult>"
)
ERROR_IN_BODY = '<?xml version="1.0"?><Error><Code>{}</Code><Message>m</Message></Error>'


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def multipart_tag(etags: list[str]) -> str:
    joined = b"".join(bytes.fromhex(tag) for tag in etags)
    return f"{md5_hex(joined)}-{len(etags)}"


ETAGS = [md5_hex(b"one"), md5_hex(b"two")]
GOOD_TAG = multipart_tag(ETAGS)


def initiated(upload_id: str = UPLOAD_ID) -> httpx.Response:
    return httpx.Response(200, content=INITIATED.format(upload_id).encode(), headers=XML)


def completed(tag: str | None = GOOD_TAG) -> httpx.Response:
    body = COMPLETED.format(tag) if tag is not None else COMPLETED.replace("<ETag>{}</ETag>", "")
    return httpx.Response(200, content=body.encode(), headers=XML)


@pytest.fixture(scope="module")
def stored_file(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, bytes]:
    data = bytes(range(256)) * (FILE_BYTES // 256) + bytes(FILE_BYTES % 256)
    path = tmp_path_factory.mktemp("multipart") / "backup.tar.gz"
    path.write_bytes(data)
    return path, data


@pytest.mark.parametrize("upload_id", [UPLOAD_ID, "a", "A" * 1024, "2~abc.def_-"])
def test_an_upload_id_in_the_grammar_is_returned_from_a_create_reply(upload_id: str) -> None:
    body = INITIATED.format(upload_id).encode()

    assert multipart.parse_upload_id(body, 200) == upload_id


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not xml",
        INITIATED.format("").encode(),
        INITIATED.format("has space").encode(),
        INITIATED.format('quo"te').encode(),
        INITIATED.format("a&amp;b").encode(),
        INITIATED.format("A" * 1025).encode(),
        INITIATED.replace("InitiateMultipartUploadResult", "Other").format("abc").encode(),
        b"<InitiateMultipartUploadResult><Key>k</Key></InitiateMultipartUploadResult>",
    ],
)
def test_a_create_reply_without_a_usable_upload_id_is_a_storage_error(body: bytes) -> None:
    with pytest.raises(StorageFailure) as raised:
        multipart.parse_upload_id(body, 200)

    assert (raised.value.kind, raised.value.status) == ("storage_error", 200)


@pytest.mark.parametrize(
    ("header", "tag"),
    [
        ('"abc123"', "abc123"),
        ("abc123", "abc123"),
        (f'"{GOOD_TAG}"', GOOD_TAG),
        ("A" * 128, "A" * 128),
    ],
)
def test_an_entity_tag_is_accepted_with_or_without_quotes(header: str, tag: str) -> None:
    assert multipart.parse_etag(header, 200) == tag


@pytest.mark.parametrize(
    "header",
    [None, "", '""', "a b", 'a"b', "W/abc", "A" * 129, "abé"],
)
def test_an_entity_tag_outside_the_grammar_is_a_storage_error(header: str | None) -> None:
    with pytest.raises(StorageFailure) as raised:
        multipart.parse_etag(header, 200)

    assert raised.value.kind == "storage_error"


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("AccessDenied", "refused"),
        ("SignatureDoesNotMatch", "refused"),
        ("BadDigest", "digest_mismatch"),
        ("NoSuchBucket", "bucket_missing"),
        ("RequestTimeTooSkewed", "clock_skew"),
        ("InternalError", "storage_error"),
        ("EntityTooSmall", "storage_error"),
    ],
)
def test_a_success_status_with_an_error_body_fails_by_the_code_in_the_body(
    code: str, kind: str
) -> None:
    with pytest.raises(StorageFailure) as raised:
        multipart.check_completion(ERROR_IN_BODY.format(code).encode(), 200, ETAGS)

    assert (raised.value.kind, raised.value.code) == (kind, code)


@pytest.mark.parametrize(
    "body",
    [b"", b"<ok/>", b"<Other><ETag>x</ETag></Other>", b"plain text"],
)
def test_a_complete_reply_without_the_result_root_is_a_storage_error(body: bytes) -> None:
    with pytest.raises(StorageFailure) as raised:
        multipart.check_completion(body, 200, ETAGS)

    assert raised.value.kind == "storage_error"


@pytest.mark.parametrize(
    "tag",
    [
        GOOD_TAG,
        f"&quot;{GOOD_TAG}&quot;",
        GOOD_TAG.upper(),
        md5_hex(b"whole object"),
        "not-a-multipart-tag",
        None,
    ],
)
def test_a_complete_reply_with_a_matching_or_non_multipart_tag_is_accepted(
    tag: str | None,
) -> None:
    body = completed(tag).content

    multipart.check_completion(body, 200, ETAGS)


@pytest.mark.parametrize(
    "tag",
    [
        f"{md5_hex(b'other')}-{len(ETAGS)}",
        f"{md5_hex(b''.join(bytes.fromhex(t) for t in ETAGS))}-{len(ETAGS) + 1}",
    ],
)
def test_a_multipart_tag_that_is_not_the_md5_of_the_part_md5s_and_their_count_is_a_digest_mismatch(
    tag: str,
) -> None:
    with pytest.raises(StorageFailure) as raised:
        multipart.check_completion(completed(tag).content, 200, ETAGS)

    assert raised.value.kind == "digest_mismatch"


def test_part_tags_that_are_not_md5_digests_skip_the_multipart_tag_check() -> None:
    multipart.check_completion(completed(GOOD_TAG).content, 200, ["etag-one", "etag-two"])


def test_the_completion_body_lists_the_parts_in_order_with_quoted_tags() -> None:
    body = multipart.completion_body(["aa", "bb"]).decode()

    assert body == (
        "<CompleteMultipartUpload>"
        '<Part><PartNumber>1</PartNumber><ETag>"aa"</ETag></Part>'
        '<Part><PartNumber>2</PartNumber><ETag>"bb"</ETag></Part>'
        "</CompleteMultipartUpload>"
    )


async def test_creating_an_upload_posts_the_uploads_marker_and_returns_the_id() -> None:
    wire = Wire(initiated())

    upload_id = await client_for(wire).create_multipart_upload(KEY, content_type=GZIP)

    [sent] = wire.seen
    assert upload_id == UPLOAD_ID
    assert (sent.method, sent.path, sent.raw_query) == ("POST", f"/{BUCKET}/{KEY}", "uploads=")
    assert sent.headers["content-type"] == GZIP


async def test_creating_an_upload_with_a_bad_content_type_sends_nothing() -> None:
    wire = Wire(initiated())

    with pytest.raises(ValueError, match="content_type"):
        await client_for(wire).create_multipart_upload(KEY, content_type="not a type")

    assert wire.seen == []


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (error_reply(403, "AccessDenied"), "refused"),
        (error_reply(404, "NoSuchBucket"), "bucket_missing"),
        (httpx.Response(HTTP_ERROR), "storage_error"),
        (httpx.Response(200, content=b"<InitiateMultipartUploadResult/>"), "storage_error"),
    ],
)
async def test_a_create_that_fails_or_answers_without_an_id_raises_by_its_kind(
    reply: httpx.Response, kind: str
) -> None:
    with pytest.raises(StorageFailure) as raised:
        await client_for(Wire(reply)).create_multipart_upload(KEY, content_type=GZIP)

    assert raised.value.kind == kind


async def test_a_part_sends_exactly_its_byte_range_with_its_length_and_md5(
    stored_file: tuple[Path, bytes],
) -> None:
    path, data = stored_file
    part = hash_file(path, with_parts=True, part_bytes=PART_BYTES).parts[1]
    wire = Wire(httpx.Response(200, headers={"ETag": f'"{md5_hex(b"x")}"'}))

    etag = await client_for(wire).upload_part(KEY, UPLOAD_ID, SECOND_PART, path, part)

    [sent] = wire.seen
    assert etag == md5_hex(b"x")
    assert sent.body == data[PART_BYTES : 2 * PART_BYTES]
    assert sent.headers["content-md5"] == part.md5_b64
    assert sent.headers["content-length"] == str(PART_BYTES)
    assert sent.query["partNumber"] == [str(SECOND_PART)]
    assert sent.query["uploadId"] == [UPLOAD_ID]


async def test_the_last_short_part_sends_only_the_bytes_that_remain(
    stored_file: tuple[Path, bytes],
) -> None:
    path, data = stored_file
    part = hash_file(path, with_parts=True, part_bytes=PART_BYTES).parts[-1]
    wire = Wire(httpx.Response(200, headers={"ETag": '"abc"'}))

    await client_for(wire).upload_part(KEY, UPLOAD_ID, PART_COUNT, path, part)

    assert wire.seen[0].body == data[2 * PART_BYTES :]
    assert wire.seen[0].headers["content-length"] == str(len(data) - 2 * PART_BYTES)


async def test_an_upload_id_is_percent_encoded_in_the_query(
    stored_file: tuple[Path, bytes],
) -> None:
    path, _ = stored_file
    part = hash_file(path, with_parts=True, part_bytes=PART_BYTES).parts[0]
    wire = Wire(httpx.Response(200, headers={"ETag": '"abc"'}))

    await client_for(wire).upload_part(KEY, UPLOAD_ID, 1, path, part)

    assert wire.seen[0].raw_query == "partNumber=1&uploadId=VXBsb2FkSWQ.-_~%2B%2F%3D"


async def test_a_part_reply_without_a_usable_tag_is_a_storage_error(
    stored_file: tuple[Path, bytes],
) -> None:
    path, _ = stored_file
    part = hash_file(path, with_parts=True, part_bytes=PART_BYTES).parts[0]

    with pytest.raises(StorageFailure) as raised:
        await client_for(Wire(httpx.Response(200))).upload_part(KEY, UPLOAD_ID, 1, path, part)

    assert raised.value.kind == "storage_error"


async def test_a_part_refused_by_the_server_raises_its_kind(
    stored_file: tuple[Path, bytes],
) -> None:
    path, _ = stored_file
    part = hash_file(path, with_parts=True, part_bytes=PART_BYTES).parts[0]

    with pytest.raises(StorageFailure) as raised:
        await client_for(Wire(error_reply(400, "BadDigest"))).upload_part(
            KEY, UPLOAD_ID, 1, path, part
        )

    assert raised.value.kind == "digest_mismatch"


def part_beyond_file(part: PartDigest) -> PartDigest:
    return PartDigest(part.offset + 1, part.size, part.sha256_hex, part.md5_b64)


def part_without_digest(part: PartDigest) -> PartDigest:
    return PartDigest(part.offset, part.size, part.sha256_hex, "short")


@pytest.mark.parametrize("number", [0, 10_001])
async def test_a_part_number_outside_the_range_is_refused_before_any_request(
    stored_file: tuple[Path, bytes], number: int
) -> None:
    path, _ = stored_file
    part = hash_file(path, with_parts=True, part_bytes=PART_BYTES).parts[0]
    wire = Wire()

    with pytest.raises(ValueError, match="number"):
        await client_for(wire).upload_part(KEY, UPLOAD_ID, number, path, part)

    assert wire.seen == []


@pytest.mark.parametrize("damage", [part_beyond_file, part_without_digest])
async def test_a_part_that_does_not_describe_the_file_is_refused_before_any_request(
    stored_file: tuple[Path, bytes], damage: Callable[[PartDigest], PartDigest]
) -> None:
    path, _ = stored_file
    part = hash_file(path, with_parts=True, part_bytes=PART_BYTES).parts[-1]
    wire = Wire()

    with pytest.raises(ValueError):
        await client_for(wire).upload_part(KEY, UPLOAD_ID, 1, path, damage(part))

    assert wire.seen == []


@pytest.mark.parametrize("upload_id", ["", "has space", "a<b", "A" * 1025])
async def test_an_upload_id_outside_the_grammar_is_refused_by_every_call(upload_id: str) -> None:
    wire = Wire()
    client = client_for(wire)

    with pytest.raises(ValueError, match="upload_id"):
        await client.abort_multipart_upload(KEY, upload_id)
    with pytest.raises(ValueError, match="upload_id"):
        await client.complete_multipart_upload(KEY, upload_id, ETAGS, total_bytes=1)

    assert wire.seen == []


async def test_completing_posts_the_tags_in_part_order_and_accepts_a_matching_reply() -> None:
    wire = Wire(completed())

    await client_for(wire).complete_multipart_upload(KEY, UPLOAD_ID, ETAGS, total_bytes=10)

    [sent] = wire.seen
    assert (sent.method, sent.query) == ("POST", {"uploadId": [UPLOAD_ID]})
    assert sent.headers["content-type"] == "application/xml"
    assert sent.body == multipart.completion_body(ETAGS)


async def test_completing_sends_quoted_tags_as_unquoted_pairs_in_quotes() -> None:
    wire = Wire(completed())

    await client_for(wire).complete_multipart_upload(
        KEY, UPLOAD_ID, [f'"{tag}"' for tag in ETAGS], total_bytes=10
    )

    assert wire.seen[0].body == multipart.completion_body(ETAGS)


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (
            httpx.Response(200, content=ERROR_IN_BODY.format("InternalError").encode()),
            "storage_error",
        ),
        (httpx.Response(200, content=ERROR_IN_BODY.format("AccessDenied").encode()), "refused"),
        (httpx.Response(200, content=b"<Unrelated/>"), "storage_error"),
        (completed(f"{md5_hex(b'x')}-{len(ETAGS)}"), "digest_mismatch"),
        (error_reply(400, "InvalidPart"), "storage_error"),
        (error_reply(404, "NoSuchUpload"), "storage_error"),
        (httpx.Response(HTTP_ERROR), "storage_error"),
    ],
)
async def test_a_complete_that_fails_in_any_form_raises_by_its_kind(
    reply: httpx.Response, kind: str
) -> None:
    with pytest.raises(StorageFailure) as raised:
        await client_for(Wire(reply)).complete_multipart_upload(
            KEY, UPLOAD_ID, ETAGS, total_bytes=10
        )

    assert raised.value.kind == kind


@pytest.mark.parametrize(
    ("etags", "total"),
    [([], 10), (["a b"], 10), (ETAGS, 0), (["aa"] * 10_001, 10)],
)
async def test_a_complete_with_unusable_arguments_is_refused_before_any_request(
    etags: list[str], total: int
) -> None:
    wire = Wire()

    with pytest.raises(ValueError):
        await client_for(wire).complete_multipart_upload(KEY, UPLOAD_ID, etags, total_bytes=total)

    assert wire.seen == []


async def test_the_complete_deadline_is_computed_from_the_size_of_the_whole_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[int] = []
    real = client_module.upload_timeout

    def spy(size: int) -> float:
        asked.append(size)
        return real(size)

    monkeypatch.setattr(client_module, "upload_timeout", spy)
    total = 5 * 1024**3

    await client_for(Wire(completed())).complete_multipart_upload(
        KEY, UPLOAD_ID, ETAGS, total_bytes=total
    )

    assert asked == [total]


async def test_a_part_deadline_is_computed_from_the_size_of_the_part(
    stored_file: tuple[Path, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = stored_file
    part = hash_file(path, with_parts=True, part_bytes=PART_BYTES).parts[0]
    asked: list[int] = []
    real = client_module.upload_timeout

    def spy(size: int) -> float:
        asked.append(size)
        return real(size)

    monkeypatch.setattr(client_module, "upload_timeout", spy)

    await client_for(Wire(httpx.Response(200, headers={"ETag": '"abc"'}))).upload_part(
        KEY, UPLOAD_ID, 1, path, part
    )

    assert asked == [PART_BYTES]


@pytest.mark.parametrize("success", [httpx.Response(204), httpx.Response(200)])
async def test_aborting_deletes_the_upload_and_any_success_status_counts(
    success: httpx.Response,
) -> None:
    wire = Wire(success)

    await client_for(wire).abort_multipart_upload(KEY, UPLOAD_ID)

    [sent] = wire.seen
    assert (sent.method, sent.query) == ("DELETE", {"uploadId": [UPLOAD_ID]})


async def test_aborting_an_upload_that_is_already_gone_counts_as_aborted() -> None:
    await client_for(Wire(error_reply(404, "NoSuchUpload"))).abort_multipart_upload(KEY, UPLOAD_ID)


@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (error_reply(404, "NoSuchKey"), "storage_error"),
        (error_reply(403, "AccessDenied"), "refused"),
        (httpx.Response(HTTP_ERROR), "storage_error"),
    ],
)
async def test_an_abort_that_fails_otherwise_raises_by_its_kind(
    reply: httpx.Response, kind: str
) -> None:
    with pytest.raises(StorageFailure) as raised:
        await client_for(Wire(reply)).abort_multipart_upload(KEY, UPLOAD_ID)

    assert raised.value.kind == kind


async def test_a_failed_request_logs_its_kind_status_and_code_but_not_the_upload_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    with pytest.raises(StorageFailure):
        await client_for(Wire(error_reply(403, "AccessDenied"))).abort_multipart_upload(
            KEY, UPLOAD_ID
        )

    [record] = [r for r in caplog.records if r.getMessage() == "object_storage_request_failed"]
    assert (record.kind, record.status, record.code) == (  # type: ignore[attr-defined]
        "refused",
        403,
        "AccessDenied",
    )
    assert UPLOAD_ID not in str(record.__dict__)
