"""activity-log-exporter against a fake bucket: objects, cursor, retries, failures and dry runs."""

import gzip
import json
import logging
import re
import ssl
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

import app.subsystems.activity_log_exporter as exporter_module
from app import netpolicy
from app.auth.errors import PermissionDenied
from app.auth.principal import Principal
from app.config import Settings
from app.crypto.vault import Vault
from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.activity_export_cursor import ActivityExportCursorRepository
from app.scheduler.model import RunResult
from app.services.activity_csv import CSV_COLUMNS
from app.subsystems.activity_log_exporter import (
    ActivityLogExporter,
    ActivityLogExporterParameters,
    ExportRelease,
    export_release,
)
from tests.object_storage.support import SECRET
from tests.scheduler.support import Rig
from tests.subsystems.exporter_support import (
    EXPORTER,
    PREFIX,
    RETRY_PAUSE,
    Exporter,
    error,
    nth_put,
    put_times,
    rows_of,
)

RULE_MISSING = "rule_missing"
PER_OBJECT = 4
ROWS = 10
OBJECTS = 3
LAST_ROW_OF_FIRST = PER_OBJECT
BUDGET_OVERSHOOT = timedelta(minutes=21)
TINY_OBJECT_BYTES = 10
SMALL_OBJECT_BYTES = 2_500
SPLIT_ROWS = 40
CAPPED_FIGURE = 10
NDJSON_OBJECT_ID = re.compile(r"[0-9]{20}\.ndjson\.gz")
SCHEMA_VERSION = 2
CURSOR_AT = "INSERT INTO activity_export_cursor (rule_id, last_event_id) VALUES (?, ?)"
ADMIN = Principal(1, "admin", "administrator")
STANDARD = Principal(2, "user", "standard")


@pytest.fixture
async def exp(
    rig: Rig, settings: Settings, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> Exporter:
    monkeypatch.setattr(exporter_module, "ROWS_PER_OBJECT", PER_OBJECT)
    built = Exporter(rig, settings, vault)
    built.make_target()
    await built.make_rule()
    return built


def connect_error(_request: httpx.Request) -> Exception:
    return httpx.ConnectError("unreachable")


def address_refused(_request: httpx.Request) -> Exception:
    return netpolicy.AddressRefused("169.254.169.254")


def tls_error(_request: httpx.Request) -> Exception:
    failure = httpx.ConnectError("tls")
    failure.__cause__ = ssl.SSLCertVerificationError("bad certificate")
    return failure


STORAGE_FAILURES = [
    pytest.param(connect_error, "unreachable", True, id="unreachable"),
    pytest.param(tls_error, "tls_failed", False, id="tls"),
    pytest.param(address_refused, "address_refused", False, id="address-refused"),
    pytest.param(error(403, "AccessDenied"), "refused", False, id="refused"),
    pytest.param(error(404, "NoSuchBucket"), "bucket_missing", False, id="bucket-missing"),
    pytest.param(error(400, "BadDigest"), "digest_mismatch", False, id="digest-mismatch"),
    pytest.param(error(403, "RequestTimeTooSkewed"), "clock_skew", False, id="clock-skew"),
    pytest.param(error(400, "EntityTooLarge"), "storage_error", False, id="storage-error"),
]


async def unreadable(exp: Exporter) -> None:
    exp.make_target(ciphertext="v1:AAAA:BBBB")
    await exp.make_rule()


async def bad_prefix(exp: Exporter) -> None:
    exp.make_target(prefix="a//b")
    await exp.make_rule()


async def bad_endpoint(exp: Exporter) -> None:
    exp.make_target(endpoint="ftp://example.invalid")
    await exp.make_rule()


async def bad_access_key(exp: Exporter) -> None:
    exp.make_target(access="bad key with spaces")
    await exp.make_rule()


async def missing_target(exp: Exporter) -> None:
    await exp.make_rule(target_id=999)


UNUSABLE_TARGETS = [
    pytest.param(missing_target, "target_missing", id="target-missing"),
    pytest.param(unreadable, "credential_unreadable", id="credential-unreadable"),
    pytest.param(bad_prefix, "prefix_invalid", id="prefix-invalid"),
    pytest.param(bad_endpoint, "endpoint_invalid", id="endpoint-invalid"),
    pytest.param(bad_access_key, "refused", id="unsignable-access-key"),
]


def test_the_exporter_declares_the_policies_of_a_rule_that_takes_no_environment() -> None:
    assert ActivityLogExporter.requires_environment is False
    assert ActivityLogExporter.event_policy == "failures_only"
    assert ActivityLogExporter.min_interval_seconds == 3600
    assert ActivityLogExporter.allowed_overrun_policies == frozenset({"skip"})
    assert (ActivityLogExporter.supports_dry_run, ActivityLogExporter.cancellation_safe) == (
        True,
        True,
    )


@pytest.mark.parametrize("target_id", [0, -1])
def test_a_target_id_below_one_is_not_a_valid_parameter(target_id: int) -> None:
    with pytest.raises(ValidationError):
        ActivityLogExporterParameters(object_storage_target_id=target_id)


async def test_only_an_administrator_may_aim_a_rule_at_the_exporter(exp: Exporter) -> None:
    await exp.subsystem.authorise_target(ADMIN, None, "", None)  # type: ignore[arg-type]

    with pytest.raises(PermissionDenied):
        await exp.subsystem.authorise_target(STANDARD, None, "", None)  # type: ignore[arg-type]


async def test_a_run_uploads_objects_by_first_id_and_moves_the_cursor_only_after_each_upload(
    exp: Exporter,
) -> None:
    exp.seed(ROWS)

    result = await exp.run()

    first, second, third = exp.bucket.puts
    assert result.outcome == "success"
    assert first.startswith(f"{PREFIX}/rule-{exp.rule_id}-20260101T120000/2026/01/01/")
    assert [key.rsplit("/", 1)[1] for key in exp.bucket.puts] == [
        "00000000000000000001.csv.gz",
        "00000000000000000005.csv.gz",
        "00000000000000000009.csv.gz",
    ]
    assert exp.bucket.cursor_at_put == [None, PER_OBJECT, 2 * PER_OBJECT]
    assert exp.cursor() == ROWS
    assert [len(rows_of(exp.bucket.objects[key])) - 1 for key in (first, second, third)] == [
        PER_OBJECT,
        PER_OBJECT,
        ROWS - 2 * PER_OBJECT,
    ]
    assert dict(result.detail) == {
        "target_id": exp.target_id,
        "objects": OBJECTS,
        "rows": ROWS,
        "first_id": 1,
        "last_id": ROWS,
        "more_pending": False,
    }
    assert exp.failures() == []


async def test_an_object_is_deterministic_gzip_and_its_cells_read_back_unchanged(
    exp: Exporter,
) -> None:
    exp.seed(1)

    await exp.run()

    [key] = exp.bucket.puts
    body = exp.bucket.objects[key]
    header, row = rows_of(body)
    assert body[4:8] == b"\x00\x00\x00\x00"
    assert gzip.compress(gzip.decompress(body), mtime=0) == body
    assert json.loads(row[header.index("detail")])["note"] == 'row 0, "q"\nline'


async def test_the_rows_an_export_has_taken_are_released_to_retention_and_later_ones_are_not(
    exp: Exporter,
) -> None:
    exp.seed(ROWS)
    exp.age()
    await exp.run()
    exp.seed(3, when=exp.rig.clock())

    removed = await exp.prune()

    remaining = exp.rig.sql(
        "SELECT count(*) FROM activity_event WHERE action = 'container_restarted'"
    )
    assert (removed, remaining[0][0]) == (ROWS, 3)


async def test_a_failed_upload_is_retried_once_after_a_pause_and_writes_the_same_key(
    exp: Exporter,
) -> None:
    exp.seed(2)
    exp.bucket.script.append(put_times(1, error(503, "ServiceUnavailable")))

    result = await exp.run()

    assert result.outcome == "success"
    assert exp.sleeps == [RETRY_PAUSE]
    assert len(exp.bucket.puts) == 1
    assert exp.bucket.requests == 2
    assert exp.cursor() == 2


async def test_a_failure_after_the_retry_records_one_row_and_holds_every_event(
    exp: Exporter,
) -> None:
    exp.seed(ROWS)
    exp.age()
    exp.bucket.script.append(put_times(2, error(503, "ServiceUnavailable")))

    result = await exp.run()

    [row] = exp.failures()
    detail = row["detail"]
    assert (result.outcome, result.reason) == ("failure", "storage_error")
    assert exp.cursor() is None
    assert exp.bucket.puts == []
    assert (row["result"], row["actor_type"], row["actor_subsystem"], row["rule_id"]) == (
        "failure",
        "subsystem",
        EXPORTER,
        exp.rule_id,
    )
    assert (row["target_type"], row["target_id"], row["environment_id"]) == (
        "object_storage_target",
        str(exp.target_id),
        None,
    )
    assert row["job_run_id"] is not None
    assert (detail["reason"], detail["storage_status"], detail["storage_code"]) == (
        "storage_error",
        503,
        "ServiceUnavailable",
    )
    assert (detail["first_id"], detail["last_id"], detail["rows"], detail["objects_exported"]) == (
        1,
        PER_OBJECT,
        PER_OBJECT,
        0,
    )
    assert detail["target_name"]
    assert detail["object_path"].endswith("00000000000000000001.csv.gz")
    assert SECRET not in json.dumps(detail)
    assert await exp.prune() == 0


async def test_the_next_run_writes_the_failed_key_again_with_the_events_that_arrived_since(
    exp: Exporter,
) -> None:
    exp.seed(2)
    exp.bucket.script.append(put_times(2, error(503, "ServiceUnavailable")))
    await exp.run()
    [row] = exp.failures()
    key = row["detail"]["object_path"]
    exp.seed(1, start=10)

    result = await exp.run()

    assert result.outcome == "success"
    assert exp.bucket.puts[0] == key
    assert exp.cursor() is not None


async def test_a_failure_after_some_objects_is_partial_and_the_next_run_resumes_at_the_failed_one(
    exp: Exporter,
) -> None:
    exp.seed(ROWS)
    exp.age()
    exp.bucket.script.append(nth_put(2, error(403, "AccessDenied")))

    result = await exp.run()

    [row] = exp.failures()
    assert (result.outcome, result.reason) == ("partial", "refused")
    assert (exp.cursor(), len(exp.bucket.puts)) == (PER_OBJECT, 1)
    assert (row["detail"]["objects_exported"], row["detail"]["first_id"]) == (1, PER_OBJECT + 1)
    assert dict(result.detail)["objects"] == 1
    assert dict(result.detail)["more_pending"] is True
    assert await exp.prune() == PER_OBJECT
    resumed = await exp.run()
    assert resumed.outcome == "success"
    assert exp.bucket.puts[1].endswith("00000000000000000005.csv.gz")


@pytest.mark.parametrize(("outcome", "reason", "retried"), STORAGE_FAILURES)
async def test_each_storage_failure_ends_the_run_with_one_row_naming_its_kind(
    exp: Exporter, outcome: Any, reason: str, retried: bool
) -> None:
    exp.seed(3)
    exp.bucket.script.append(lambda request: outcome(request) if request.method == "PUT" else None)

    result = await exp.run()

    [row] = exp.failures()
    assert (result.outcome, result.reason) == ("failure", reason)
    assert row["detail"]["reason"] == reason
    assert "<Error>" not in row["detail"]["message"]
    assert exp.cursor() is None
    assert exp.sleeps == ([RETRY_PAUSE] if retried else [])


@pytest.mark.parametrize(("prepare", "reason"), UNUSABLE_TARGETS)
async def test_a_target_that_cannot_be_used_fails_the_run_without_a_request(
    rig: Rig,
    settings: Settings,
    vault: Vault,
    monkeypatch: pytest.MonkeyPatch,
    prepare: Any,
    reason: str,
) -> None:
    monkeypatch.setattr(exporter_module, "ROWS_PER_OBJECT", PER_OBJECT)
    unusable = Exporter(rig, settings, vault)
    await prepare(unusable)
    unusable.seed(2)

    result = await unusable.run()

    [row] = unusable.failures()
    assert (result.outcome, result.reason) == ("failure", reason)
    assert row["detail"]["reason"] == reason
    assert unusable.bucket.requests == 0


async def test_a_rule_deleted_during_the_run_fails_it_without_moving_a_cursor(
    exp: Exporter,
) -> None:
    exp.seed(2)

    def delete_rule(request: httpx.Request) -> None:
        if request.method == "PUT":
            exp.rig.sql("DELETE FROM automation_rule WHERE id = ?", (exp.rule_id,))

    exp.bucket.script.append(delete_rule)

    result = await exp.run()

    assert result.outcome == "failure"
    assert exp.rig.sql("SELECT count(*) FROM activity_export_cursor")[0][0] == 0


async def test_a_rule_deleted_during_an_upload_ends_the_run_with_the_failure_it_recorded(
    exp: Exporter,
) -> None:
    exp.seed(2)

    def delete_rule(request: httpx.Request) -> None:
        if request.method == "PUT":
            exp.rig.sql("DELETE FROM automation_rule WHERE id = ?", (exp.rule_id,))

    exp.bucket.script.append(delete_rule)

    result = await exp.run()

    [row] = exp.failures()
    assert row["detail"]["reason"] == RULE_MISSING
    assert (result.outcome, result.reason) == ("failure", RULE_MISSING)


async def test_a_dry_run_counts_what_is_pending_and_names_the_next_object_without_touching_anything(
    exp: Exporter,
) -> None:
    exp.seed(7)
    exp.rig.sql("UPDATE object_storage_target SET secret_access_key_ciphertext = 'v1:AAAA:BBBB'")
    before = exp.count()

    result = await exp.run(dry=True)

    detail = dict(result.detail)
    assert result.outcome == "success"
    assert (detail["pending_rows"], detail["pending_rows_capped"]) == (7, False)
    assert str(detail["next_object_path"]).endswith("/00000000000000000001.csv.gz")
    assert (exp.bucket.requests, exp.cursor(), exp.count()) == (0, None, before)


async def test_a_dry_run_caps_the_figure_it_reports(
    exp: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp.seed(5)
    monkeypatch.setattr(exporter_module, "PENDING_COUNT_CAP", 3)

    result = await exp.run(dry=True)

    assert (dict(result.detail)["pending_rows"], dict(result.detail)["pending_rows_capped"]) == (
        3,
        True,
    )


async def test_a_dry_run_of_an_empty_log_names_no_object(exp: Exporter) -> None:
    result = await exp.run(dry=True)

    assert "next_object_path" not in dict(result.detail)


async def test_a_dry_run_whose_target_is_gone_or_whose_prefix_is_invalid_fails(
    exp: Exporter,
) -> None:
    exp.rig.sql("UPDATE object_storage_target SET activity_log_prefix = 'a//b'")

    result = await exp.run(dry=True)

    assert (result.outcome, result.reason) == ("failure", "prefix_invalid")
    assert exp.failures() == []


async def test_a_dry_run_whose_target_is_gone_fails_with_target_missing(exp: Exporter) -> None:
    exp.rig.sql("DELETE FROM object_storage_target")

    result = await exp.run(dry=True)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert exp.failures() == []


async def test_deleting_a_rule_releases_the_hold_and_its_cursor_goes_with_it(exp: Exporter) -> None:
    exp.seed(5)
    exp.age()
    await exp.run()
    exp.rig.sql("DELETE FROM automation_rule WHERE id = ?", (exp.rule_id,))
    exp.seed(5, start=10)

    removed = await exp.prune()

    assert exp.rig.sql("SELECT count(*) FROM activity_export_cursor")[0][0] == 0
    assert removed == 10


async def test_no_object_starts_after_the_start_budget_is_spent(exp: Exporter) -> None:
    exp.seed(ROWS)
    exp.age()

    def slow(request: httpx.Request) -> None:
        if request.method == "PUT":
            exp.rig.clock.now += BUDGET_OVERSHOOT

    exp.bucket.script.append(slow)

    result = await exp.run()

    assert result.outcome == "success"
    assert (len(exp.bucket.puts), exp.cursor()) == (1, PER_OBJECT)
    assert (dict(result.detail)["more_pending"], dict(result.detail)["objects"]) == (True, 1)


async def test_an_object_is_split_by_rows_when_it_reaches_the_byte_cap(
    exp: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter_module, "ROWS_PER_OBJECT", 20_000)
    monkeypatch.setattr(exporter_module, "MAX_OBJECT_BYTES", SMALL_OBJECT_BYTES)
    exp.seed(SPLIT_ROWS)

    result = await exp.run()

    sizes = [len(gzip.decompress(exp.bucket.objects[key])) for key in exp.bucket.puts]
    firsts = [int(rows_of(exp.bucket.objects[key])[1][0]) for key in exp.bucket.puts]
    assert result.outcome == "success"
    assert len(sizes) > 1
    assert max(sizes) <= SMALL_OBJECT_BYTES
    assert firsts[0] == 1
    assert exp.cursor() == SPLIT_ROWS


async def test_a_single_row_larger_than_the_byte_cap_still_goes_in_an_object_of_its_own(
    exp: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exporter_module, "MAX_OBJECT_BYTES", TINY_OBJECT_BYTES)
    exp.seed(3)

    result = await exp.run()

    assert result.outcome == "success"
    assert [len(rows_of(exp.bucket.objects[key])) - 1 for key in exp.bucket.puts] == [1, 1, 1]


async def test_a_rule_that_reuses_a_deleted_rules_id_writes_to_a_folder_of_its_own(
    exp: Exporter,
) -> None:
    exp.seed(3)
    await exp.run()
    first = set(exp.bucket.puts)
    exp.rig.sql("DELETE FROM automation_rule WHERE id = ?", (exp.rule_id,))
    exp.rig.clock.now += timedelta(hours=5)
    stamp = exp.rig.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
    exp.rig.sql(
        "INSERT INTO automation_rule (id, name, subsystem, environment_id, target_selector, "
        "parameters, is_enabled, created_by_user_id, created_at, updated_at) "
        "VALUES (?, 'again', ?, NULL, '', ?, 1, ?, ?, ?)",
        (
            exp.rule_id,
            EXPORTER,
            json.dumps({"object_storage_target_id": exp.target_id}),
            exp.user_id,
            stamp,
            stamp,
        ),
    )
    exp.job_id = await exp.rig.job(subsystem=EXPORTER, rule_id=exp.rule_id, expression="3600")

    result = await exp.run()

    second = set(exp.bucket.puts) - first
    assert result.outcome == "success"
    assert len(second) == 1


async def test_the_run_stops_with_no_request_or_body_kept_as_the_failures_context(
    exp: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp.seed(3)
    exp.bucket.script.append(lambda request: httpx.ConnectError("down", request=request))
    stops: list[Any] = []
    original = ActivityLogExporter._record_stop

    async def spy(self: Any, context: Any, progress: Any, stopped: Any) -> RunResult:
        stops.append(stopped)
        return await original(self, context, progress, stopped)

    monkeypatch.setattr(ActivityLogExporter, "_record_stop", spy)

    result = await exp.run()

    [stopped] = stops
    assert result.outcome == "failure"
    assert (stopped.__context__, stopped.__cause__) == (None, None)
    assert (stopped.failure.__context__, stopped.failure.__cause__) == (None, None)
    assert exp.sleeps == [RETRY_PAUSE]


async def test_the_secret_appears_in_no_row_no_run_detail_and_no_log(
    exp: Exporter, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    exp.seed(3)
    exp.bucket.script.append(put_times(2, error(403, "AccessDenied")))

    result = await exp.run()

    everything = json.dumps(
        [dict(row) for row in exp.rig.sql("SELECT * FROM activity_event")],
        default=str,
    )
    assert SECRET not in everything + repr(result) + repr(caplog.records)


async def seed_cursors(rig: Rig, exp: Exporter, first: int, second: int) -> tuple[int, int]:
    other = await rig.rule(
        "export-b",
        owner=exp.user_id,
        subsystem=EXPORTER,
        parameters=json.dumps({"object_storage_target_id": exp.target_id}),
        environment_id=None,
    )
    async with rig.database.session() as db, db.transaction():
        cursors = ActivityExportCursorRepository(db, rig.clock)
        await cursors.move_export_cursor(exp.rule_id, last_event_id=first, object_key="a")
        await cursors.move_export_cursor(other, last_event_id=second, object_key="b")
    return exp.rule_id, other


async def release_of(rig: Rig, rule_id: int) -> ExportRelease:
    async with rig.database.session() as db:
        return await export_release(db, rig.clock, rule_id)


async def test_deleting_the_lowest_exporter_releases_the_events_up_to_the_next_cursor(
    rig: Rig, exp: Exporter
) -> None:
    exp.seed(100)
    lowest, highest = await seed_cursors(rig, exp, 20, 60)

    assert await release_of(rig, lowest) == ExportRelease(rows=40, more_than=False)
    assert await release_of(rig, highest) == ExportRelease(rows=0, more_than=False)


async def test_deleting_the_only_exporter_releases_every_event_after_its_cursor(
    rig: Rig, exp: Exporter
) -> None:
    exp.seed(30)
    exp.rig.sql(
        "INSERT INTO activity_export_cursor (rule_id, last_event_id) VALUES (?, 5)", (exp.rule_id,)
    )

    assert await release_of(rig, exp.rule_id) == ExportRelease(rows=25, more_than=False)


async def test_two_rules_that_have_exported_nothing_release_nothing_when_one_is_deleted(
    rig: Rig, exp: Exporter
) -> None:
    exp.seed(30)
    await seed_cursors(rig, exp, 0, 0)

    assert await release_of(rig, exp.rule_id) == ExportRelease(rows=0, more_than=False)


async def test_the_released_figure_is_one_bounded_count_and_says_when_it_is_capped(
    rig: Rig, exp: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp.seed(30)
    monkeypatch.setattr(exporter_module, "MAX_EXPORT_COUNT", CAPPED_FIGURE)
    counted: list[str] = []
    after, between = ActivityEventRepository.count_after, ActivityEventRepository.count_between

    async def count_after(self: Any, *args: Any, **kwargs: Any) -> int:
        counted.append("after")
        return await after(self, *args, **kwargs)

    async def count_between(self: Any, *args: Any, **kwargs: Any) -> int:
        counted.append("between")
        return await between(self, *args, **kwargs)

    monkeypatch.setattr(ActivityEventRepository, "count_after", count_after)
    monkeypatch.setattr(ActivityEventRepository, "count_between", count_between)
    exp.rig.sql(
        "INSERT INTO activity_export_cursor (rule_id, last_event_id) VALUES (?, 0)", (exp.rule_id,)
    )

    release = await release_of(rig, exp.rule_id)

    assert release == ExportRelease(rows=CAPPED_FIGURE, more_than=True)
    assert counted == ["after"]


def ndjson_records(body: bytes) -> list[dict[str, Any]]:
    text = gzip.decompress(body).decode("utf-8")
    assert text.endswith("\n")
    return [json.loads(line) for line in text.split("\n")[:-1]]


@pytest.fixture
async def ndjson_exp(
    rig: Rig, settings: Settings, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> Exporter:
    monkeypatch.setattr(exporter_module, "ROWS_PER_OBJECT", PER_OBJECT)
    built = Exporter(rig, settings, vault)
    built.make_target()
    built.rule_id = await rig.rule(
        "export-ndjson",
        owner=built.user_id,
        subsystem=EXPORTER,
        parameters=json.dumps({"object_storage_target_id": built.target_id, "format": "ndjson"}),
        environment_id=None,
    )
    built.job_id = await rig.job(subsystem=EXPORTER, rule_id=built.rule_id, expression="3600")
    return built


@pytest.mark.parametrize(
    ("stored", "expected"),
    [({}, "csv"), ({"format": "csv"}, "csv"), ({"format": "ndjson"}, "ndjson")],
)
def test_a_rule_reads_as_csv_unless_it_names_ndjson(stored: dict[str, str], expected: str) -> None:
    parameters = ActivityLogExporterParameters.model_validate(
        {"object_storage_target_id": 1, **stored}
    )

    assert parameters.format == expected


@pytest.mark.parametrize("name", ["xml", "CSV", "", "ndjson.gz"])
def test_a_format_other_than_csv_or_ndjson_is_not_a_valid_parameter(name: str) -> None:
    with pytest.raises(ValidationError):
        ActivityLogExporterParameters.model_validate(
            {"object_storage_target_id": 1, "format": name}
        )


async def test_an_ndjson_rule_writes_gzip_objects_of_typed_records_under_the_csv_layout(
    ndjson_exp: Exporter,
) -> None:
    exp = ndjson_exp
    exp.seed(ROWS)

    result = await exp.run()

    first, second, third = exp.bucket.puts
    objects = [ndjson_records(exp.bucket.objects[key]) for key in exp.bucket.puts]
    assert result.outcome == "success"
    assert first.startswith(f"{PREFIX}/rule-{exp.rule_id}-20260101T120000/2026/01/01/")
    assert [key.rsplit("/", 1)[1] for key in exp.bucket.puts] == [
        "00000000000000000001.ndjson.gz",
        "00000000000000000005.ndjson.gz",
        "00000000000000000009.ndjson.gz",
    ]
    assert all(NDJSON_OBJECT_ID.fullmatch(key.rsplit("/", 1)[1]) for key in (first, second, third))
    assert [[record["id"] for record in records] for records in objects] == [
        [1, 2, 3, 4],
        [5, 6, 7, 8],
        [9, 10],
    ]
    assert exp.bucket.cursor_at_put == [None, PER_OBJECT, 2 * PER_OBJECT]
    assert exp.cursor() == ROWS
    assert dict(result.detail) == {
        "target_id": exp.target_id,
        "objects": OBJECTS,
        "rows": ROWS,
        "first_id": 1,
        "last_id": ROWS,
        "more_pending": False,
    }


async def test_an_ndjson_record_has_the_schema_version_the_csv_keys_and_its_detail_as_an_object(
    ndjson_exp: Exporter,
) -> None:
    exp = ndjson_exp
    exp.seed(1)

    await exp.run()

    [key] = exp.bucket.puts
    body = exp.bucket.objects[key]
    [record] = ndjson_records(body)
    assert list(record) == ["schema_version", *CSV_COLUMNS]
    assert record["schema_version"] == SCHEMA_VERSION
    assert isinstance(record["id"], int) and isinstance(record["actor_user_id"], int)
    assert record["actor_user_id"] == exp.user_id
    assert record["occurred_at"] == "2026-01-01T12:00:00Z"
    assert record["detail"] == {"note": 'row 0, "q"\nline'}
    assert record["rule_id"] is None and record["job_run_id"] is None
    assert body[4:8] == b"\x00\x00\x00\x00"
    assert gzip.compress(gzip.decompress(body), mtime=0) == body


async def test_the_byte_cap_of_an_ndjson_object_counts_the_uncompressed_records(
    ndjson_exp: Exporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = ndjson_exp
    monkeypatch.setattr(exporter_module, "ROWS_PER_OBJECT", 20_000)
    monkeypatch.setattr(exporter_module, "MAX_OBJECT_BYTES", SMALL_OBJECT_BYTES)
    exp.seed(SPLIT_ROWS)

    result = await exp.run()

    bodies = [gzip.decompress(exp.bucket.objects[key]) for key in exp.bucket.puts]
    ids = [
        record["id"]
        for key in exp.bucket.puts
        for record in ndjson_records(exp.bucket.objects[key])
    ]
    assert result.outcome == "success"
    assert len(bodies) > 1
    assert max(len(body) for body in bodies) <= SMALL_OBJECT_BYTES
    assert ids == list(range(1, SPLIT_ROWS + 1))
    assert exp.cursor() == SPLIT_ROWS


async def test_a_dry_run_of_an_ndjson_rule_names_an_ndjson_object_and_contacts_nothing(
    ndjson_exp: Exporter,
) -> None:
    exp = ndjson_exp
    exp.seed(3)

    result = await exp.run(dry=True)

    assert str(dict(result.detail)["next_object_path"]).endswith("/00000000000000000001.ndjson.gz")
    assert (exp.bucket.requests, exp.cursor()) == (0, None)
