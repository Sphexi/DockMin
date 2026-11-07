"""Listing, pagination and deletion against captured replies and hostile bodies."""

import time

import httpx
import pytest

from app.object_storage import MAX_LIST_BODY, ObjectStorageClient, StorageFailure, validate_prefix
from app.object_storage.listing import parse_listing
from tests.object_storage.support import (
    Requests,
    Script,
    endpoint,
    make_client,
    replay,
    sample,
)

FOLDER = "backups/rule-1-20260101T000000/"
PAGE_SIZE = 2
PARSE_BUDGET_SECONDS = 5.0
EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
ENDPOINT_PATH_STYLE = ("http://127.0.0.1:29000", True)
ENDPOINT_VIRTUAL_HOST = ("http://s3.localhost:29000", False)
STORAGE_ERROR = ("storage_error", 200, None)
GOOD = "<Contents><Key>p/a.tar.gz</Key><Size>1</Size></Contents>"
MORE = "<IsTruncated>true</IsTruncated>"
DONE = "<IsTruncated>false</IsTruncated>"


def listing_xml(blocks: str, tail: str = DONE) -> bytes:
    return (
        f'<?xml version="1.0"?><ListBucketResult xmlns="x"><Name>b</Name>{tail}{blocks}'
        "</ListBucketResult>"
    ).encode()


def block(key: str, *, size: str = "1", modified: str = "2026-10-24T17:21:14Z") -> str:
    return (
        f"<Contents><Key>{key}</Key><Size>{size}</Size>"
        f"<LastModified>{modified}</LastModified></Contents>"
    )


def parsed(body: bytes, prefix: str = "p/", token: str | None = None) -> tuple[list[str], int]:
    page = parse_listing(body, prefix=prefix, sent_token=token)
    return [entry.key for entry in page.objects], page.skipped


def failure_of(body: bytes, token: str | None = None) -> tuple[str, int | None, str | None]:
    with pytest.raises(StorageFailure) as raised:
        parse_listing(body, prefix="p/", sent_token=token)
    return raised.value.kind, raised.value.status, raised.value.code


def listing_client(status: int, body: bytes) -> ObjectStorageClient:
    return make_client(lambda request: httpx.Response(status, content=body))


@pytest.mark.parametrize(
    ("tag", "target", "keys_expected"),
    [("signed", ENDPOINT_PATH_STYLE, 6), ("signed-vhost", ENDPOINT_VIRTUAL_HOST, 5)],
    ids=["path-style", "virtual-host"],
)
async def test_a_paginated_listing_is_followed_in_key_order_with_each_token_signed(
    tag: str, target: tuple[str, bool], keys_expected: int
) -> None:
    script = Script(f"{tag}-list-page1", f"{tag}-list-page2", f"{tag}-list-page3")
    url, path_style = target
    client = make_client(script, endpoint(url, path_style=path_style))
    keys: list[str] = []
    tokens: list[str | None] = []
    token = None

    while True:
        page = await client.list_objects(FOLDER, max_keys=PAGE_SIZE, continuation=token)
        keys += [entry.key for entry in page.objects]
        tokens.append(page.next_token)
        if not page.truncated:
            break
        token = page.next_token

    sent_queries = [request.url.query.decode() for request in script.seen]
    assert len(script.seen) == 3
    assert (len(keys), keys == sorted(keys)) == (keys_expected, True)
    assert tokens[:2] == [keys[PAGE_SIZE - 1], keys[2 * PAGE_SIZE - 1]] and tokens[2] is None
    assert sent_queries[0].startswith(f"list-type=2&prefix={FOLDER}&max-keys=2")
    assert f"continuation-token={tokens[0].replace('/', '%2F')}" in sent_queries[1]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("name", "prefix", "options", "count", "truncated", "has_token"),
    [
        ("list-empty", "backups/rule-99-20260101T000000/", {}, 0, False, False),
        ("signed-list-bogus-token", FOLDER, {"continuation": "zzz-not-a-token"}, 0, False, False),
        ("signed-list-max1", "backups/", {"max_keys": 1}, 1, True, True),
        ("list-all", FOLDER, {}, 6, False, False),
        ("list-rule1-noslash", "backups/rule-1", {}, 7, False, False),
    ],
    ids=["empty", "token-the-server-does-not-know", "one-key-page", "whole-folder", "no-slash"],
)
async def test_a_captured_listing_parses_to_the_pages_it_describes(
    name: str,
    prefix: str,
    options: dict[str, object],
    count: int,
    truncated: bool,
    has_token: bool,
) -> None:
    client = make_client(replay(name))

    page = await client.list_objects(prefix, **options)  # type: ignore[arg-type]

    assert (len(page.objects), page.truncated, page.next_token is not None) == (
        count,
        truncated,
        has_token,
    )
    assert page.skipped == 0


async def test_a_listing_carries_whole_second_timestamps_and_sizes_from_the_server() -> None:
    client = make_client(replay("list-all"))

    page = await client.list_objects(FOLDER)

    assert page.objects[0].last_modified == "2026-10-24T17:21:14Z"
    assert {entry.size for entry in page.objects} == {16}


@pytest.mark.parametrize(
    ("body", "kept", "skipped"),
    [
        (listing_xml(GOOD), ["p/a.tar.gz"], 0),
        (listing_xml(block("p/f", modified="2026-10-24T17:21:14.123Z")), ["p/f"], 0),
        (
            listing_xml(
                "<Contents><LastModified>2026-10-24T17:21:14Z</LastModified>"
                "<Size>3</Size><Key>p/b</Key></Contents>"
            ),
            ["p/b"],
            0,
        ),
        (
            listing_xml("<Contents><Key>p/n</Key><Size>1</Size></Contents>" + GOOD),
            ["p/n", "p/a.tar.gz"],
            0,
        ),
        (listing_xml(block("q/x") + GOOD), ["p/a.tar.gz"], 1),
        (listing_xml(block("p/a&amp;b") + GOOD), ["p/a.tar.gz"], 1),
        (listing_xml(block("p/a b") + GOOD), ["p/a.tar.gz"], 1),
        (listing_xml(block("p/é") + GOOD), ["p/a.tar.gz"], 1),
        (listing_xml(block("p/../x") + GOOD), ["p/a.tar.gz"], 1),
        (listing_xml(block("p//x") + GOOD), ["p/a.tar.gz"], 1),
        (listing_xml(block("p/dir/") + GOOD), ["p/a.tar.gz"], 1),
        (listing_xml(block("p/" + "a" * 1100) + GOOD), ["p/a.tar.gz"], 1),
        (listing_xml(block("p/<b>") + GOOD), ["p/a.tar.gz"], 1),
        (
            listing_xml(block("p/s", size="12x") + block("p/t", size="9" * 21) + GOOD),
            ["p/a.tar.gz"],
            2,
        ),
        (listing_xml(block("p/d", modified="yesterday") + GOOD), ["p/a.tar.gz"], 1),
        (
            listing_xml("<Contents><Key>p/a</Key><Key>p/b</Key><Size>1</Size></Contents>" + GOOD),
            ["p/a.tar.gz"],
            1,
        ),
        (
            listing_xml(
                "<Contents><Key>p/z</Key><Contents><Key>p/y</Key><Size>1</Size></Contents>"
                "<Size>1</Size></Contents>" + GOOD
            ),
            ["p/y", "p/a.tar.gz"],
            1,
        ),
        (listing_xml("<Contents><Key>p/unclosed</Key>" + GOOD), ["p/a.tar.gz"], 1),
    ],
    ids=[
        "plain",
        "fractional-seconds",
        "children-in-any-order",
        "no-timestamp",
        "outside-the-prefix",
        "entity-in-key",
        "space-in-key",
        "non-ascii-key",
        "dot-segment",
        "empty-segment",
        "folder-marker",
        "key-too-long",
        "markup-in-key",
        "bad-sizes",
        "bad-timestamp",
        "two-keys",
        "nested-block",
        "unclosed-block",
    ],
)
def test_a_listing_entry_outside_the_key_alphabet_or_shape_is_counted_and_never_returned(
    body: bytes, kept: list[str], skipped: int
) -> None:
    assert parsed(body) == (kept, skipped)


@pytest.mark.parametrize(
    ("token", "accepted"),
    [
        ("a=+/", True),
        ("p/a.tar.gz", True),
        ("a/../b", True),
        ("a&amp;b", False),
        ("", False),
        ("a b", False),
        ("a" * 1025, False),
        ("a%2Fb", False),
    ],
    ids=[
        "padding-and-plus",
        "a-key",
        "dot-segment",
        "entity",
        "empty",
        "space",
        "too-long",
        "percent",
    ],
)
def test_a_continuation_token_is_followed_only_inside_the_token_alphabet(
    token: str, accepted: bool
) -> None:
    body = listing_xml(GOOD, f"{MORE}<NextContinuationToken>{token}</NextContinuationToken>")

    if accepted:
        assert parse_listing(body, prefix="p/", sent_token=None).next_token == token
    else:
        assert failure_of(body) == STORAGE_ERROR


@pytest.mark.parametrize(
    ("body", "sent"),
    [
        (listing_xml(GOOD, MORE), None),
        (listing_xml(GOOD, f"{MORE}<ContinuationToken>old</ContinuationToken>"), None),
        (
            listing_xml(GOOD, f"{MORE}<NextContinuationToken>p/a.tar.gz</NextContinuationToken>"),
            "p/a.tar.gz",
        ),
        (b"<html><body>captive</body></html>", None),
        (b"", None),
        (b"<Error><Code>AccessDenied</Code></Error>", None),
        (b"<ListBucketResult></ListBucketResult>", None),
        (listing_xml(GOOD, MORE + DONE), None),
    ],
    ids=[
        "truncated-without-token",
        "echoed-token-only",
        "token-that-makes-no-progress",
        "html-page",
        "empty-body",
        "error-document",
        "no-truncation-flag",
        "two-truncation-flags",
    ],
)
def test_a_reply_that_is_not_a_usable_page_is_a_storage_error(
    body: bytes, sent: str | None
) -> None:
    assert failure_of(body, sent) == STORAGE_ERROR


def test_a_token_on_the_final_page_is_ignored() -> None:
    body = listing_xml(GOOD, f"{DONE}<NextContinuationToken>junk&amp;</NextContinuationToken>")

    page = parse_listing(body, prefix="p/", sent_token=None)

    assert (page.truncated, page.next_token) == (False, None)


@pytest.mark.parametrize(
    "body",
    [
        listing_xml("<Contents>" * 200_000),
        listing_xml("<Contents>" * 150_000 + "</Contents>"),
        listing_xml("<Contents></Contents>" * 90_000),
        listing_xml(("<Contents><Key>p/" + "a" * 1022 + "</Key><Size>1</Size></Contents>") * 1700),
    ],
    ids=["unclosed-openers", "nested-openers", "empty-blocks", "maximum-length-entries"],
)
def test_a_hostile_body_inside_the_cap_is_parsed_in_bounded_time(body: bytes) -> None:
    started = time.perf_counter()

    parse_listing(body[:MAX_LIST_BODY], prefix="p/", sent_token=None)

    assert time.perf_counter() - started < PARSE_BUDGET_SECONDS


async def test_a_body_over_the_cap_is_refused_and_one_at_the_cap_is_parsed() -> None:
    exact = listing_xml(GOOD)
    exact += b" " * (MAX_LIST_BODY - len(exact))
    over = listing_xml(GOOD) + b" " * (MAX_LIST_BODY + 10)

    accepted = await listing_client(200, exact).list_objects("p/")
    with pytest.raises(StorageFailure) as raised:
        await listing_client(200, over).list_objects("p/")

    assert len(accepted.objects) == 1
    assert (raised.value.kind, raised.value.status) == ("storage_error", 200)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (404, b"<Error><Code>NoSuchBucket</Code></Error>", ("bucket_missing", 404, "NoSuchBucket")),
        (500, b"", ("storage_error", 500, None)),
    ],
)
async def test_a_listing_error_status_is_classified_by_its_code(
    status: int, body: bytes, expected: tuple[str, int, str | None]
) -> None:
    with pytest.raises(StorageFailure) as raised:
        await listing_client(status, body).list_objects("p/")

    assert (raised.value.kind, raised.value.status, raised.value.code) == expected


@pytest.mark.parametrize("prefix", ["../x", "a//b", "a b", "/", "a/" * 600])
async def test_a_listing_prefix_outside_the_key_alphabet_is_refused_before_any_request(
    prefix: str,
) -> None:
    requests = Requests()

    with pytest.raises(StorageFailure) as raised:
        await make_client(requests).list_objects(prefix)

    assert (raised.value.kind, requests.seen) == ("key_invalid", [])


@pytest.mark.parametrize(
    "options", [{"max_keys": 0}, {"max_keys": 1001}, {"continuation": "a b"}, {"continuation": ""}]
)
async def test_a_listing_argument_outside_its_range_is_a_value_error_before_any_request(
    options: dict[str, object],
) -> None:
    requests = Requests()

    with pytest.raises(ValueError):
        await make_client(requests).list_objects("p/", **options)  # type: ignore[arg-type]

    assert requests.seen == []


@pytest.mark.parametrize(
    ("prefix", "folder"),
    [("", ""), ("a/b", "a/b"), ("a/b/", "a/b"), ("backups", "backups")],
)
def test_a_configured_prefix_loses_one_trailing_slash(prefix: str, folder: str) -> None:
    assert validate_prefix(prefix) == folder


@pytest.mark.parametrize("prefix", ["/", "a//", "../a", "a b"])
def test_a_configured_prefix_outside_the_key_alphabet_is_refused(prefix: str) -> None:
    with pytest.raises(StorageFailure) as raised:
        validate_prefix(prefix)

    assert raised.value.kind == "key_invalid"


@pytest.mark.parametrize(
    "name", ["signed-delete-existing", "signed-delete-again", "signed-delete-never-existed"]
)
async def test_deleting_a_key_succeeds_on_every_status_the_server_answers_for_it(name: str) -> None:
    captured = sample(name)
    key = captured.request_line.split(" ")[1].removeprefix("/dockmin-e2e/")
    script = Script(name)

    await make_client(script).delete_object(key)

    request = script.seen[0]
    assert request.method == "DELETE"
    assert request.headers["x-amz-content-sha256"] == EMPTY_BODY_SHA256
    assert captured.status == 204


async def test_deleting_through_a_virtual_host_matches_the_captured_request() -> None:
    captured = sample("signed-vhost-delete-existing")
    key = captured.request_line.split(" ")[1].lstrip("/")
    script = Script("signed-vhost-delete-existing")
    client = make_client(script, endpoint("http://s3.localhost:29000", path_style=False))

    await client.delete_object(key)

    assert script.seen[0].headers["host"] == "dockmin-e2e.s3.localhost:29000"


@pytest.mark.parametrize(
    "name", ["signed-readonly-delete", "delete-wrong-secret", "delete-missing-bucket"]
)
async def test_a_refused_delete_is_a_refusal_with_the_status_the_server_sent(name: str) -> None:
    client = make_client(replay(name))

    with pytest.raises(StorageFailure) as raised:
        await client.delete_object("backups/x.tar.gz")

    assert (raised.value.kind, raised.value.status) == ("refused", 403)


@pytest.mark.parametrize(
    ("status", "body", "removed"),
    [
        (404, b"<Error><Code>NoSuchKey</Code></Error>", True),
        (404, b"<Error><Code>NoSuchBucket</Code></Error>", False),
        (404, b"", False),
        (200, b"", True),
        (202, b"", True),
        (204, b"", True),
    ],
)
async def test_a_delete_counts_as_done_for_any_success_status_and_for_a_missing_key(
    status: int, body: bytes, removed: bool
) -> None:
    client = listing_client(status, body)

    if removed:
        await client.delete_object("backups/x")
    else:
        with pytest.raises(StorageFailure):
            await client.delete_object("backups/x")


@pytest.mark.parametrize("key", ["../a", "a/", "a b", ""])
async def test_deleting_a_key_outside_the_alphabet_is_refused_before_any_request(key: str) -> None:
    requests = Requests()

    with pytest.raises(StorageFailure) as raised:
        await make_client(requests).delete_object(key)

    assert (raised.value.kind, requests.seen) == ("key_invalid", [])
