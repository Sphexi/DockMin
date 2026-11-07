"""backup-syncer pushing an archive of several parts: ordering, retries, aborts and the cap."""

import asyncio
import functools
import hashlib
import logging
import math
from datetime import timedelta

import httpx
import pytest

from app.backup import archive as archive_module
from app.backup.manifest import MAX_ARCHIVE_BYTES
from app.config import Settings
from app.subsystems.backup_syncer import RETRY_DELAY, BackupSyncer
from tests.scheduler.support import Rig
from tests.subsystems import syncer_support
from tests.subsystems.multipart_bucket import MultipartBucket, error_xml, on_request
from tests.subsystems.syncer_support import Syncer

PART_BYTES = 16 * 1024
SECOND_PART_MARKER = "partNumber=2&"
SERVER_ERROR = 500
CAP = 7 * 1024**3
RUN_MINUTES = 180
TINY_CAP = 1024
CONNECT_FAILURE = httpx.ConnectError("unreachable")


def part_count(size: int) -> int:
    return math.ceil(size / PART_BYTES)


def part_operations(size: int) -> list[str]:
    return ["create", *(f"part {n}" for n in range(1, part_count(size) + 1)), "complete"]


@pytest.fixture
def small_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        archive_module,
        "hash_file",
        functools.partial(archive_module.hash_file, part_bytes=PART_BYTES),
    )


@pytest.fixture
async def syncer(rig: Rig, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Syncer:
    monkeypatch.setattr(syncer_support, "Bucket", MultipartBucket)
    built = await Syncer.build(rig, settings)
    built.make_target()
    await built.make_rule()
    return built


def bucket_of(syncer: Syncer) -> MultipartBucket:
    assert isinstance(syncer.bucket, MultipartBucket)
    return syncer.bucket


def stored_size(syncer: Syncer) -> int:
    [data] = syncer.bucket.objects.values()
    return len(data)


@pytest.mark.usefixtures("small_parts")
async def test_an_archive_above_one_part_goes_up_as_create_then_every_part_in_order_then_complete(
    syncer: Syncer,
) -> None:
    result = await syncer.run()

    bucket = bucket_of(syncer)
    [key] = bucket.objects
    [record] = syncer.rig.sql("SELECT archive_sha256, outcome FROM backup_record")
    assert result.outcome == "success"
    assert bucket.operations == part_operations(len(bucket.objects[key]))
    assert hashlib.sha256(bucket.objects[key]).hexdigest() == record["archive_sha256"]
    assert bucket.uploads == {} and bucket.aborted == []
    assert bucket.problems == []
    assert syncer.work_directories() == [] and not syncer.production.held


@pytest.mark.usefixtures("small_parts")
async def test_every_part_is_sent_with_a_content_md5(syncer: Syncer) -> None:
    await syncer.run()

    bucket = bucket_of(syncer)
    assert len(bucket.part_md5_headers) == part_count(stored_size(syncer))
    assert None not in bucket.part_md5_headers


@pytest.mark.usefixtures("small_parts")
async def test_the_uploaded_row_records_a_multipart_push_and_its_part_count(
    syncer: Syncer,
) -> None:
    await syncer.run()

    [row] = syncer.events("backup_uploaded")
    assert row["detail"]["multipart"] is True
    assert row["detail"]["parts"] == part_count(stored_size(syncer))
    assert row["result"] == "success"


async def test_an_archive_of_one_part_is_a_single_put_and_records_one_part(
    syncer: Syncer,
) -> None:
    await syncer.run()

    bucket = bucket_of(syncer)
    [row] = syncer.events("backup_uploaded")
    assert bucket.operations == []
    assert bucket.count("PUT") == 1
    assert (row["detail"]["multipart"], row["detail"]["parts"]) == (False, 1)


@pytest.mark.usefixtures("small_parts")
async def test_a_part_that_fails_transiently_is_retried_once_after_the_pause(
    syncer: Syncer,
) -> None:
    bucket = bucket_of(syncer)
    bucket.script = [on_request("PUT", SECOND_PART_MARKER, 1, CONNECT_FAILURE)]

    result = await syncer.run()

    assert result.outcome == "success"
    assert bucket.operations.count("part 2") == 2
    assert syncer.sleeps == [RETRY_DELAY]
    assert bucket.aborted == [] and len(syncer.events("backup_uploaded")) == 1


@pytest.mark.usefixtures("small_parts")
async def test_a_server_error_on_a_part_is_transient_too(syncer: Syncer) -> None:
    bucket = bucket_of(syncer)
    bucket.script = [on_request("PUT", SECOND_PART_MARKER, 1, httpx.Response(SERVER_ERROR))]

    result = await syncer.run()

    assert result.outcome == "success"
    assert syncer.sleeps == [RETRY_DELAY]


@pytest.mark.usefixtures("small_parts")
async def test_a_part_that_fails_twice_aborts_the_upload_and_fails_the_run_with_one_row(
    syncer: Syncer,
) -> None:
    bucket = bucket_of(syncer)
    bucket.script = [on_request("PUT", SECOND_PART_MARKER, 2, CONNECT_FAILURE)]

    result = await syncer.run()

    [row] = syncer.events("backup_uploaded")
    [record] = syncer.rig.sql("SELECT outcome FROM backup_record")
    assert (result.outcome, result.reason) == ("failure", "unreachable")
    assert bucket.operations == ["create", "part 1", "part 2", "part 2", "abort"]
    assert bucket.uploads == {} and len(bucket.aborted) == 1
    assert bucket.objects == {}
    assert (row["result"], row["detail"]["reason"]) == ("failure", "unreachable")
    assert record["outcome"] == "failure"
    assert syncer.work_directories() == [] and not syncer.production.held


@pytest.mark.usefixtures("small_parts")
async def test_a_part_the_server_refuses_is_not_retried_and_aborts_the_upload(
    syncer: Syncer,
) -> None:
    bucket = bucket_of(syncer)
    bucket.script = [on_request("PUT", "partNumber=1&", 5, error_xml(403, "AccessDenied"))]

    result = await syncer.run()

    assert (result.outcome, result.reason, syncer.sleeps) == ("failure", "refused", [])
    assert bucket.operations == ["create", "part 1", "abort"]
    assert len(syncer.events("backup_uploaded")) == 1


@pytest.mark.usefixtures("small_parts")
async def test_a_refused_complete_aborts_the_upload_and_fails_the_run_with_one_row(
    syncer: Syncer,
) -> None:
    bucket = bucket_of(syncer)
    bucket.script = [on_request("POST", "uploadId=", 1, error_xml(403, "AccessDenied"))]

    result = await syncer.run()

    [row] = syncer.events("backup_uploaded")
    assert (result.outcome, result.reason) == ("failure", "refused")
    assert bucket.operations[-2:] == ["complete", "abort"]
    assert bucket.objects == {} and bucket.uploads == {}
    assert row["detail"]["storage_status"] == 403
    assert syncer.work_directories() == []


@pytest.mark.usefixtures("small_parts")
async def test_a_complete_answered_200_with_an_error_body_aborts_and_fails_by_its_code(
    syncer: Syncer,
) -> None:
    bucket = bucket_of(syncer)
    bucket.script = [on_request("POST", "uploadId=", 1, error_xml(200, "InternalError"))]

    result = await syncer.run()

    [row] = syncer.events("backup_uploaded")
    assert (result.outcome, result.reason) == ("failure", "storage_error")
    assert bucket.operations[-2:] == ["complete", "abort"]
    assert (row["detail"]["storage_status"], row["detail"]["storage_code"]) == (
        200,
        "InternalError",
    )
    assert bucket.objects == {} and len(syncer.events("backup_uploaded")) == 1


@pytest.mark.usefixtures("small_parts")
async def test_a_create_the_server_refuses_fails_the_run_without_an_abort(
    syncer: Syncer,
) -> None:
    bucket = bucket_of(syncer)
    bucket.script = [on_request("POST", "uploads", 1, error_xml(403, "AccessDenied"))]

    result = await syncer.run()

    assert (result.outcome, result.reason) == ("failure", "refused")
    assert bucket.operations == ["create"]
    assert len(syncer.events("backup_uploaded")) == 1


@pytest.mark.usefixtures("small_parts")
async def test_a_failing_abort_is_logged_and_leaves_the_runs_own_failure_in_place(
    syncer: Syncer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    bucket = bucket_of(syncer)
    bucket.script = [
        on_request("PUT", SECOND_PART_MARKER, 2, error_xml(403, "AccessDenied")),
        on_request("DELETE", "uploadId=", 1, error_xml(403, "AccessDenied")),
    ]

    result = await syncer.run()

    [logged] = [r for r in caplog.records if r.getMessage() == "backup_multipart_abort_failed"]
    assert logged.kind == "refused"  # type: ignore[attr-defined]
    assert (result.outcome, result.reason) == ("failure", "refused")
    assert len(syncer.events("backup_uploaded")) == 1


@pytest.mark.usefixtures("small_parts")
async def test_a_run_cancelled_during_a_part_aborts_the_upload_and_records_no_success(
    syncer: Syncer,
) -> None:
    bucket = bucket_of(syncer)
    bucket.hold = asyncio.Event()
    task = asyncio.create_task(syncer.run())
    await asyncio.wait_for(bucket.entered.wait(), 20)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bucket.operations[-1] == "abort" and len(bucket.aborted) == 1
    assert bucket.uploads == {} and bucket.objects == {}
    assert syncer.events("backup_uploaded") == []
    assert syncer.work_directories() == [] and not syncer.production.held


@pytest.mark.usefixtures("small_parts")
async def test_an_abort_in_flight_completes_when_the_run_is_cancelled_a_second_time(
    syncer: Syncer,
) -> None:
    bucket = bucket_of(syncer)
    bucket.hold = asyncio.Event()
    bucket.abort_hold = asyncio.Event()
    task = asyncio.create_task(syncer.run())
    await asyncio.wait_for(bucket.entered.wait(), 20)
    task.cancel()
    await asyncio.wait_for(bucket.abort_entered.wait(), 20)

    task.cancel()
    bucket.abort_hold.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(bucket.aborted) == 1 and bucket.uploads == {}
    assert syncer.work_directories() == []


def test_the_archive_cap_is_seven_gibibytes() -> None:
    assert MAX_ARCHIVE_BYTES == CAP


async def test_an_archive_above_the_cap_is_refused_at_production_and_nothing_is_sent(
    syncer: Syncer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archive_module, "MAX_ARCHIVE_BYTES", TINY_CAP)

    result = await syncer.run()

    [row] = syncer.events("backup_uploaded")
    assert (result.outcome, result.reason) == ("failure", "production_failed")
    assert row["result"] == "failure"
    assert bucket_of(syncer).operations == [] and bucket_of(syncer).objects == {}
    assert syncer.work_directories() == [] and not syncer.production.held


def test_the_run_timeout_covers_the_longest_upload() -> None:
    assert BackupSyncer.run_timeout == timedelta(minutes=RUN_MINUTES)
