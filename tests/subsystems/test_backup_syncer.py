"""backup-syncer against a fake bucket: upload, records, retention, failures and dry runs."""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

import app.subsystems.backup_syncer as syncer_module
from app import netpolicy
from app.auth.errors import PermissionDenied
from app.auth.principal import Principal
from app.config import Settings
from app.scheduler.model import RunResult
from app.subsystems.backup_syncer import BackupSyncer
from tests.object_storage.support import SECRET
from tests.scheduler.support import Rig
from tests.subsystems.syncer_support import (
    RETRY_PAUSE,
    Bucket,
    Syncer,
    always_on,
    empty_listing,
    error,
    on_method,
)

ADMIN = Principal(1, "admin", "administrator")
STANDARD = Principal(2, "user", "standard")
HOUR = 3600
RETENTION = 2
FIRST_ARCHIVE = "dockmin-backup-20260101T120100Z.tar.gz"
UNREADABLE_SECRET = "v1:AAAA:BBBB"
RULE_MISSING = "rule_missing"
PAGE_OF_TWO = 2
STALE_ARCHIVES = 5


@pytest.fixture
async def syncer(rig: Rig, settings: Settings) -> Syncer:
    built = await Syncer.build(rig, settings)
    built.make_target(retention=RETENTION)
    await built.make_rule()
    return built


def connect_error(_request: httpx.Request) -> Exception:
    return httpx.ConnectError("unreachable")


def test_the_syncer_declares_the_policies_of_a_rule_that_takes_no_environment() -> None:
    assert (
        BackupSyncer.catch_up,
        BackupSyncer.event_policy,
        BackupSyncer.requires_environment,
        BackupSyncer.cancellation_safe,
        BackupSyncer.supports_dry_run,
    ) == (True, "per_action", False, True, True)
    assert BackupSyncer.min_interval_seconds == HOUR
    assert BackupSyncer.allowed_overrun_policies == frozenset({"skip"})


async def test_only_an_administrator_may_aim_a_rule_at_the_syncer(syncer: Syncer) -> None:
    await syncer.subsystem.authorise_target(ADMIN, None, "", None)  # type: ignore[arg-type]

    with pytest.raises(PermissionDenied):
        await syncer.subsystem.authorise_target(STANDARD, None, "", None)  # type: ignore[arg-type]


async def test_a_run_stores_the_archive_under_the_rules_folder_named_by_the_clock(
    syncer: Syncer,
) -> None:
    syncer.rig.clock.jump(60)

    result = await syncer.run()

    assert (result.outcome, result.attempted) == ("success", 1)
    assert list(syncer.bucket.objects) == [f"{syncer.folder}{FIRST_ARCHIVE}"]
    assert syncer.bucket.problems == []
    assert syncer.work_directories() == [] and not syncer.production.held


async def test_a_stored_archive_has_a_matching_record_and_one_success_row_by_the_subsystem(
    syncer: Syncer,
) -> None:
    syncer.rig.clock.jump(60)

    result = await syncer.run()

    [key] = syncer.bucket.objects
    [record] = syncer.rig.sql(
        "SELECT origin, object_storage_target_id, destination_key, archive_sha256, outcome "
        "FROM backup_record"
    )
    [row] = syncer.events("backup_uploaded")
    assert hashlib.sha256(syncer.bucket.objects[key]).hexdigest() == record["archive_sha256"]
    assert (record["origin"], record["destination_key"], record["outcome"]) == (
        "sync",
        key,
        "success",
    )
    assert (row["result"], row["actor_type"], row["actor_subsystem"], row["rule_id"]) == (
        "success",
        "subsystem",
        "backup-syncer",
        syncer.rule_id,
    )
    assert row["target_id"] == str(syncer.target_id)
    assert row["detail"]["object_path"] == key
    assert "[REDACTED]" not in json.dumps(row["detail"])
    assert dict(result.detail)["object_path"] == key
    assert dict(result.detail)["size_bytes"] == len(syncer.bucket.objects[key])


async def test_a_folder_holding_one_archive_writes_no_prune_row(syncer: Syncer) -> None:
    await syncer.run()

    assert syncer.events("backup_remote_pruned") == []


async def run_hours(syncer: Syncer, hours: int) -> None:
    for _ in range(hours):
        syncer.rig.clock.jump(HOUR)
        await syncer.run()


def plant_foreign_objects(syncer: Syncer) -> list[str]:
    other_rule = f"backups/rule-{syncer.rule_id + 100}-20260101T120000/"
    foreign = [
        f"{syncer.folder}notes.txt",
        f"{syncer.folder}dockmin-backup-bad.tar.gz",
        f"{syncer.folder}sub/dockmin-backup-20250101T000000Z.tar.gz",
        f"{other_rule}dockmin-backup-20200101T000000Z.tar.gz",
        "backups/rule-1-20250101T000000/dockmin-backup-20200101T000000Z.tar.gz",
        "backups/stray.tar.gz",
    ]
    for key in foreign:
        syncer.bucket.objects[key] = b"x"
    return foreign


async def test_retention_keeps_the_newest_archives_of_the_folder_and_leaves_foreign_objects(
    syncer: Syncer,
) -> None:
    foreign = plant_foreign_objects(syncer)

    await run_hours(syncer, 4)

    assert syncer.archives() == [
        "dockmin-backup-20260101T150000Z.tar.gz",
        "dockmin-backup-20260101T160000Z.tar.gz",
    ]
    assert all(key in syncer.bucket.objects for key in foreign)


async def test_each_deletion_writes_a_prune_row_naming_the_deleted_file(syncer: Syncer) -> None:
    plant_foreign_objects(syncer)

    await run_hours(syncer, 4)

    rows = syncer.events("backup_remote_pruned")
    details = [row["detail"] for row in rows]
    assert [d["deleted"] for d in details] == [1, 1]
    assert details[0]["deleted_paths"] == "dockmin-backup-20260101T130000Z.tar.gz"
    assert all(
        (d["folder_path"], d["retention_count"], d["deleted_paths_truncated"])
        == (syncer.folder, RETENTION, False)
        for d in details
    )
    assert all(d["other_objects"] >= 3 for d in details)
    assert {(r["result"], r["actor_subsystem"], r["rule_id"]) for r in rows} == {
        ("success", "backup-syncer", syncer.rule_id)
    }


async def test_the_run_summary_counts_what_retention_deleted_and_kept(syncer: Syncer) -> None:
    await run_hours(syncer, 2)
    syncer.rig.clock.jump(HOUR)

    result = await syncer.run()

    assert (dict(result.detail)["retention_deleted"], dict(result.detail)["retention_kept"]) == (
        1,
        RETENTION,
    )


async def test_names_stamped_after_the_new_archive_and_repeated_keys_never_cost_a_real_archive(
    syncer: Syncer,
) -> None:
    await run_hours(syncer, 2)
    future = [
        f"{syncer.folder}dockmin-backup-29990101T000000Z.tar.gz",
        f"{syncer.folder}dockmin-backup-29990102T000000Z.tar.gz",
    ]
    for key in future:
        syncer.bucket.objects[key] = b"planted"
    syncer.bucket.page_size = 1

    syncer.rig.clock.jump(HOUR)
    result = await syncer.run()

    assert syncer.archives() == [
        "dockmin-backup-20260101T140000Z.tar.gz",
        "dockmin-backup-20260101T150000Z.tar.gz",
        "dockmin-backup-29990101T000000Z.tar.gz",
        "dockmin-backup-29990102T000000Z.tar.gz",
    ]
    assert all(key in syncer.bucket.objects for key in future)
    assert dict(result.detail)["retention_deleted"] == 1


def doubled_listing(bucket: Bucket) -> Callable[[httpx.Request], httpx.Response | None]:
    """Answer every listing with each stored key twice, as a faulty server might."""

    def step(request: httpx.Request) -> httpx.Response | None:
        if request.method != "GET":
            return None
        blocks = "".join(
            f"<Contents><Key>{key}</Key><Size>1</Size></Contents>" * 2
            for key in sorted(bucket.objects)
        )
        xml = (
            '<ListBucketResult xmlns="x"><Name>b</Name><IsTruncated>false</IsTruncated>'
            f"{blocks}</ListBucketResult>"
        )
        return httpx.Response(200, content=xml.encode())

    return step


async def test_a_listing_that_repeats_keys_deletes_each_old_archive_once_and_keeps_the_newest(
    syncer: Syncer,
) -> None:
    for second in range(3):
        syncer.bucket.objects[f"{syncer.folder}dockmin-backup-20251201T00000{second}Z.tar.gz"] = (
            b"x"
        )
    syncer.bucket.script = [doubled_listing(syncer.bucket)]
    syncer.rig.clock.jump(60)

    result = await syncer.run()

    deletes = [line for line in syncer.bucket.log if line.startswith("DELETE")]
    assert dict(result.detail)["retention_deleted"] == len(deletes) == 2
    assert len(set(deletes)) == 2
    assert syncer.archives() == [
        "dockmin-backup-20251201T000002Z.tar.gz",
        FIRST_ARCHIVE,
    ]


async def test_retention_off_never_lists_the_bucket(rig: Rig, settings: Settings) -> None:
    syncer = await Syncer.build(rig, settings)
    syncer.make_target()
    await syncer.make_rule()
    syncer.rig.clock.jump(60)

    result = await syncer.run()

    assert result.outcome == "success"
    assert syncer.bucket.count("GET") == 0


async def test_a_transient_failure_is_retried_once_after_a_pause_and_the_run_succeeds(
    syncer: Syncer,
) -> None:
    syncer.bucket.script = [on_method("PUT", 1, error(503, "SlowDown"))]

    result = await syncer.run()

    assert result.outcome == "success"
    assert syncer.sleeps == [RETRY_PAUSE]
    assert syncer.bucket.count("PUT") == 2


async def test_a_refusal_fails_the_run_without_a_retry_and_stores_nothing(syncer: Syncer) -> None:
    syncer.bucket.script = [always_on("PUT", error(403, "AccessDenied"))]

    result = await syncer.run()

    [row] = syncer.events("backup_uploaded")
    [record] = syncer.rig.sql("SELECT outcome, error, destination_key FROM backup_record")
    detail = row["detail"]
    assert (result.outcome, result.reason, syncer.sleeps) == ("failure", "refused", [])
    assert row["result"] == "failure"
    assert (detail["stage"], detail["storage_status"], detail["storage_code"]) == (
        "upload",
        403,
        "AccessDenied",
    )
    assert detail["reason"] == "refused" and "message" in detail
    assert record["outcome"] == "failure" and record["error"]
    assert syncer.bucket.objects == {} and syncer.work_directories() == []
    assert not syncer.production.held


async def test_an_address_the_policy_refuses_fails_the_run_at_once_and_is_not_retried(
    syncer: Syncer,
) -> None:
    def refused(_request: httpx.Request) -> Exception:
        return netpolicy.AddressRefused("169.254.169.254")

    syncer.bucket.script = [always_on("PUT", refused)]

    result = await syncer.run()

    [row] = syncer.events("backup_uploaded")
    assert (result.outcome, result.reason, syncer.sleeps) == ("failure", "address_refused", [])
    assert row["detail"]["reason"] == "address_refused"
    assert "169.254.169.254" not in json.dumps(row["detail"])
    assert syncer.bucket.count("PUT") == 1
    assert syncer.work_directories() == [] and not syncer.production.held


async def test_a_failure_row_never_carries_text_the_server_sent(syncer: Syncer) -> None:
    syncer.bucket.script = [always_on("PUT", error(403, "AccessDenied"))]

    await syncer.run()

    [row] = syncer.events("backup_uploaded")
    assert row["detail"]["message"] != "m"
    assert "<Error>" not in json.dumps(row["detail"])


async def test_an_unreachable_bucket_fails_after_one_pause(syncer: Syncer) -> None:
    syncer.bucket.script = [on_method("PUT", 5, connect_error)]

    result = await syncer.run()

    assert (result.outcome, result.reason, syncer.sleeps) == (
        "failure",
        "unreachable",
        [RETRY_PAUSE],
    )


async def test_an_archive_the_listing_does_not_show_is_kept_and_nothing_is_deleted(
    syncer: Syncer,
) -> None:
    await run_hours(syncer, 2)
    syncer.bucket.script = [empty_listing]
    deletes = syncer.bucket.count("DELETE")
    syncer.rig.clock.jump(HOUR)

    result = await syncer.run()

    assert result.outcome == "success"
    assert syncer.bucket.count("DELETE") == deletes
    assert dict(result.detail)["retention_skipped"] == "uploaded_not_listed"


async def test_a_listing_failure_after_a_stored_archive_is_partial_with_a_prune_failure_row(
    syncer: Syncer,
) -> None:
    syncer.bucket.script = [always_on("GET", error(403, "AccessDenied"))]

    result = await syncer.run()

    [row] = syncer.events("backup_remote_pruned")
    assert (result.outcome, result.reason) == ("partial", "refused")
    assert row["result"] == "failure" and row["detail"]["reason"] == "refused"
    assert len(syncer.bucket.objects) == 1


async def test_a_delete_failure_is_partial_stops_at_the_first_and_deletes_nothing(
    syncer: Syncer,
) -> None:
    await run_hours(syncer, 2)
    syncer.bucket.script = [always_on("DELETE", error(403, "AccessDenied"))]
    syncer.rig.clock.jump(HOUR)

    result = await syncer.run()

    row = syncer.events("backup_remote_pruned")[-1]
    assert result.outcome == "partial"
    assert (row["result"], row["detail"]["deleted"], row["detail"]["storage_status"]) == (
        "failure",
        0,
        403,
    )
    assert syncer.bucket.count("DELETE") == 1


async def test_deletions_are_capped_per_run_and_a_later_run_catches_up(
    syncer: Syncer, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_hours(syncer, 3)
    for second in range(4):
        syncer.bucket.objects[f"{syncer.folder}dockmin-backup-20251201T00000{second}Z.tar.gz"] = (
            b"x"
        )
    monkeypatch.setattr(syncer_module, "MAX_DELETES_PER_RUN", 1)
    syncer.rig.clock.jump(HOUR)

    capped = await syncer.run()
    after_cap = len(syncer.archives())
    monkeypatch.setattr(syncer_module, "MAX_DELETES_PER_RUN", 100)
    syncer.rig.clock.jump(HOUR)
    await syncer.run()

    assert dict(capped.detail)["retention_deleted"] == 1
    assert after_cap > RETENTION
    assert len(syncer.archives()) == RETENTION


async def test_a_paged_listing_is_followed_to_its_end(syncer: Syncer) -> None:
    for minute in range(STALE_ARCHIVES):
        syncer.bucket.objects[
            f"{syncer.folder}dockmin-backup-20250101T0000{minute:02d}Z.tar.gz"
        ] = b"x"
    syncer.bucket.page_size = PAGE_OF_TWO
    syncer.rig.clock.jump(HOUR)

    result = await syncer.run()

    assert syncer.archives() == [
        "dockmin-backup-20250101T000004Z.tar.gz",
        "dockmin-backup-20260101T130000Z.tar.gz",
    ]
    assert dict(result.detail)["retention_deleted"] == STALE_ARCHIVES - 1
    assert syncer.bucket.count("GET") >= 3


async def test_a_dry_run_describes_the_plan_and_touches_nothing(syncer: Syncer) -> None:
    rows_before = syncer.count("activity_event")
    requests_before = syncer.bucket.requests

    result = await syncer.run(dry=True)

    detail: dict[str, Any] = dict(result.detail)
    assert result.outcome == "success"
    assert detail["target_id"] == syncer.target_id
    assert str(detail["next_object_path"]).startswith(syncer.folder)
    assert detail["retention_count"] == RETENTION and detail["database_bytes"] > 0
    assert "stack_files" in detail
    assert syncer.bucket.requests == requests_before
    assert syncer.count("activity_event") == rows_before
    assert syncer.work_directories() == []


async def unreadable_secret(syncer: Syncer) -> None:
    syncer.make_target(ciphertext=UNREADABLE_SECRET)
    await syncer.make_rule()


async def invalid_endpoint(syncer: Syncer) -> None:
    syncer.make_target(endpoint="ftp://example.invalid")
    await syncer.make_rule()


async def invalid_prefix(syncer: Syncer) -> None:
    syncer.make_target(prefix="a//b")
    await syncer.make_rule()


async def missing_target(syncer: Syncer) -> None:
    await syncer.make_rule(target_id=12345)


UNUSABLE_TARGETS = [
    pytest.param(missing_target, "target_missing", id="target-missing"),
    pytest.param(unreadable_secret, "credential_unreadable", id="credential-unreadable"),
    pytest.param(invalid_endpoint, "endpoint_invalid", id="endpoint-invalid"),
    pytest.param(invalid_prefix, "prefix_invalid", id="prefix-invalid"),
]


@pytest.mark.parametrize(("prepare", "reason"), UNUSABLE_TARGETS)
async def test_a_target_that_cannot_be_used_fails_the_run_before_anything_is_produced(
    syncer: Syncer, prepare: Callable[[Syncer], Awaitable[None]], reason: str
) -> None:
    await prepare(syncer)

    result = await syncer.run()

    [row] = syncer.events("backup_uploaded")
    assert (result.outcome, result.reason) == ("failure", reason)
    assert (row["result"], row["detail"]["reason"]) == ("failure", reason)
    assert syncer.work_directories() == [] and syncer.bucket.requests == 0


@pytest.mark.parametrize(
    ("prepare", "reason"),
    [UNUSABLE_TARGETS[0], UNUSABLE_TARGETS[3]],
)
async def test_a_dry_run_of_a_missing_target_or_a_bad_prefix_fails_the_same_way(
    syncer: Syncer, prepare: Callable[[Syncer], Awaitable[None]], reason: str
) -> None:
    await prepare(syncer)

    result = await syncer.run(dry=True)

    assert (result.outcome, result.reason) == ("failure", reason)


async def test_a_run_waits_for_a_production_in_progress_and_then_runs(syncer: Syncer) -> None:
    assert syncer.production.try_hold()
    task = asyncio.create_task(syncer.run())
    await asyncio.sleep(0.2)
    waiting_without_upload = not task.done() and syncer.bucket.count("PUT") == 0

    syncer.production.free()
    result = await asyncio.wait_for(task, 20)

    assert waiting_without_upload
    assert result.outcome == "success"


async def test_cancelling_a_run_during_the_upload_removes_its_work_directory(
    syncer: Syncer,
) -> None:
    syncer.bucket.hold = asyncio.Event()
    task = asyncio.create_task(syncer.run())
    await asyncio.wait_for(syncer.bucket.entered.wait(), 20)
    during = (syncer.work_directories() != [], syncer.production.held)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert during == (True, False)
    assert syncer.work_directories() == [] and not syncer.production.held


async def test_no_activity_row_and_no_request_carries_the_secret(syncer: Syncer) -> None:
    await run_hours(syncer, 3)

    rows = syncer.rig.sql("SELECT detail FROM activity_event")
    assert all(SECRET not in str(row["detail"]) for row in rows)
    assert syncer.bucket.problems == []


async def delete_while_uploading(syncer: Syncer, statement: str) -> RunResult:
    syncer.bucket.hold = asyncio.Event()
    task = asyncio.create_task(syncer.run())
    async with asyncio.timeout(10):
        await syncer.bucket.entered.wait()
    syncer.rig.sql(statement)
    syncer.bucket.hold.set()
    return await task


async def test_a_target_deleted_while_the_archive_uploads_fails_the_run_as_target_missing(
    syncer: Syncer,
) -> None:
    result = await delete_while_uploading(syncer, "DELETE FROM object_storage_target")

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert syncer.rig.sql("SELECT id FROM backup_record") == []
    assert syncer.work_directories() == [] and not syncer.production.held


async def test_a_rule_deleted_while_the_archive_uploads_ends_the_run_with_the_failure_it_recorded(
    syncer: Syncer,
) -> None:
    result = await delete_while_uploading(syncer, "DELETE FROM automation_rule")

    [row] = syncer.events("backup_uploaded")
    assert (row["result"], row["detail"]["reason"]) == ("failure", RULE_MISSING)
    assert (result.outcome, result.reason) == ("failure", RULE_MISSING)
