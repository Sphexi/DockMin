"""The connection test: three independent probes and the words used for each outcome."""

import asyncio

import httpx
import pytest

from app.object_storage import (
    PERMISSION_NOTE,
    ObjectStorageClient,
    StorageCheck,
    check_target,
    stage_message,
)
from app.object_storage import checks as checks_module
from tests.object_storage.support import (
    ACCESS,
    CREDENTIALS,
    NOW,
    SECRET,
    endpoint,
    make_client,
    sample,
)

STAGES = ["bucket", "backup_prefix", "activity_log_prefix"]
SHORT_DEADLINE = 0.2
LISTING_SAMPLE = "list-empty"


def summary(
    results: tuple[StorageCheck, ...],
) -> list[tuple[str, bool, str | None, int | None, str | None]]:
    return [(c.stage, c.passed, c.kind, c.status, c.code) for c in results]


def captured_server(tag: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        name = f"signed-{tag}-{'head-bucket' if request.method == 'HEAD' else 'list'}"
        captured = sample(name)
        headers = {"content-type": captured.content_type} if captured.content_type else {}
        return httpx.Response(captured.status, content=captured.body, headers=headers)

    return httpx.MockTransport(handler)


def client_over(transport: httpx.AsyncBaseTransport) -> ObjectStorageClient:
    return ObjectStorageClient(endpoint(), CREDENTIALS, transport=transport, clock=lambda: NOW)


async def test_a_credential_that_cannot_list_passes_the_bucket_and_is_refused_both_prefixes() -> (
    None
):
    client = client_over(captured_server("nolist"))

    results = await check_target(client, backup_prefix="backups", activity_log_prefix="activity")

    assert summary(results) == [
        ("bucket", True, None, None, None),
        ("backup_prefix", False, "refused", 403, "AccessDenied"),
        ("activity_log_prefix", False, "refused", 403, "AccessDenied"),
    ]


async def test_a_credential_that_can_list_but_not_write_passes_every_stage() -> None:
    client = client_over(captured_server("readonly"))

    results = await check_target(client, backup_prefix="backups", activity_log_prefix="activity")

    assert [c.stage for c in results] == STAGES
    assert all(c.passed for c in results)


async def test_every_stage_runs_when_the_first_is_refused() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(403, content=b"<Error><Code>AccessDenied</Code></Error>")

    results = await check_target(make_client(handler), backup_prefix="b", activity_log_prefix="a")

    assert seen == ["HEAD", "GET", "GET"]
    assert [c.kind for c in results] == ["refused"] * 3


async def test_a_missing_bucket_is_reported_by_every_stage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = b"" if request.method == "HEAD" else b"<Error><Code>NoSuchBucket</Code></Error>"
        return httpx.Response(404, content=body)

    results = await check_target(make_client(handler), backup_prefix="", activity_log_prefix="")

    assert [c.kind for c in results] == ["bucket_missing"] * 3


async def test_an_empty_prefix_lists_the_bucket_root_and_a_prefix_lists_its_folder() -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
        queries.append(request.url.query.decode())
        captured = sample(LISTING_SAMPLE)
        return httpx.Response(captured.status, content=captured.body)

    await check_target(make_client(handler), backup_prefix="", activity_log_prefix="x/y")

    assert queries == ["list-type=2&prefix=&max-keys=1", "list-type=2&prefix=x/y/&max-keys=1"]


async def test_an_invalid_configured_prefix_fails_its_own_stage_and_the_others_still_run() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured = sample(LISTING_SAMPLE)
        return httpx.Response(
            200 if request.method == "HEAD" else captured.status, content=captured.body
        )

    results = await check_target(
        make_client(handler), backup_prefix="../x", activity_log_prefix="ok"
    )

    assert [c.kind for c in results] == [None, "key_invalid", None]


async def test_a_transport_failure_is_reported_as_unreachable_on_every_stage() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    results = await check_target(make_client(down), backup_prefix="a", activity_log_prefix="b")

    assert [c.kind for c in results] == ["unreachable"] * 3


class Hanging(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError


async def test_a_server_that_never_answers_ends_the_test_at_the_shared_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checks_module, "CHECK_TIMEOUT", SHORT_DEADLINE)
    client = client_over(Hanging())

    results = await check_target(client, backup_prefix="a", activity_log_prefix="b")

    assert [c.kind for c in results] == ["unreachable"] * 3


async def test_cancelling_the_test_propagates_the_cancellation() -> None:
    client = client_over(Hanging())
    task = asyncio.create_task(check_target(client, backup_prefix="a", activity_log_prefix="b"))
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("tag", ["nolist", "readonly"])
async def test_the_message_for_each_stage_is_product_text_that_holds_no_credential(
    tag: str,
) -> None:
    client = client_over(captured_server(tag))

    results = await check_target(client, backup_prefix="backups", activity_log_prefix="activity")

    messages = [stage_message(c) for c in results]
    assert all(messages)
    assert not any(ACCESS in m or SECRET in m for m in messages)


def test_a_refused_prefix_stage_says_that_retention_cannot_run() -> None:
    refused = StorageCheck("backup_prefix", False, "refused", 403, "AccessDenied")

    assert "remote retention cannot run" in stage_message(refused)


def test_the_permission_note_says_the_test_does_not_check_write_or_delete() -> None:
    assert "does not check write or delete permission" in PERMISSION_NOTE
