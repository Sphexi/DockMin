"""Object storage targets: create, edit, delete and test, with the rows each action leaves."""

import json
import shutil
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.crypto.vault import Vault
from app.db.engine import Database
from app.netpolicy import REFUSAL_TEXT
from app.services.activity import ActivityRecorder
from app.services.object_storage import ObjectStorageTargets
from app.services.object_storage_admin import (
    ObjectStorageTargetService,
    TargetInput,
    stage_message,
    target_warnings,
)
from tests.crypto.helpers import FIRST_KEY, vault_with
from tests.job_support import ADMIN, STANDARD
from tests.object_storage.support import ACCESS, BUCKET, ENDPOINT, SECRET, verify_signature
from tests.registry_client.loopback import Certificates, build_certificates
from tests.scheduler.support import Clock

pytestmark = pytest.mark.usefixtures("permit_loopback")

NEW_SECRET = "new-secret-value"
VIRTUAL_HOST_ENDPOINT = "http://s3.localhost:29000"
NAME_TAKEN = "An object storage target with this name already exists."
UNREADABLE = "v1:AAAA:BBBB"
STEP = 5
MISSING_ID = 999
PEM_ROOT = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----"
EMPTY_LISTING = (
    '<ListBucketResult xmlns="x"><Name>b</Name><IsTruncated>false</IsTruncated></ListBucketResult>'
)


def form(**changes: Any) -> TargetInput:
    base = TargetInput(
        name="primary",
        endpoint_url=ENDPOINT,
        bucket=BUCKET,
        access_key_id=ACCESS,
        backup_prefix="backups",
        activity_log_prefix="activity-log",
        secret_access_key=SecretStr(SECRET),
    )
    return replace(base, **changes)


class Rig:
    """The service over a throwaway database, one session per call as a request would open."""

    def __init__(self, database: Database, vault: Vault) -> None:
        self.database = database
        self.vault = vault
        self.clock = Clock()

    async def call(
        self,
        name: str,
        *arguments: Any,
        transport: httpx.AsyncBaseTransport | None = None,
        **options: Any,
    ) -> Any:
        async with self.database.session() as session:
            service = ObjectStorageTargetService(
                session,
                self.vault,
                self.clock,
                ActivityRecorder(session, self.clock),
                transport=transport,
            )
            return await getattr(service, name)(*arguments, **options)

    async def outcome(self, name: str, *arguments: Any, **options: Any) -> Any:
        """Return the fields of a validation refusal, or the strings denied, notfound, ok."""
        try:
            await self.call(name, *arguments, **options)
        except ValidationFailed as refused:
            return refused.fields
        except PermissionDenied:
            return "denied"
        except ResourceNotFound:
            return "notfound"
        return "ok"

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        connection = self.database.connect()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def run(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.rows(sql, params)

    def targets(self) -> int:
        return len(self.rows("SELECT 1 FROM object_storage_target"))

    def stamp(self, target_id: int) -> str:
        return str(
            self.rows("SELECT updated_at FROM object_storage_target WHERE id = ?", (target_id,))[0][
                0
            ]
        )

    def events(self, action: str) -> list[dict[str, Any]]:
        return [
            {**dict(row), "detail": json.loads(row["detail"]) if row["detail"] else {}}
            for row in self.rows(
                "SELECT result, target_id, detail FROM activity_event WHERE action = ? ORDER BY id",
                (action,),
            )
        ]

    async def update(self, target_id: int, *, secret: str = "", **changes: Any) -> Any:
        self.clock.jump(STEP)
        candidate = form(name="primary", secret_access_key=SecretStr(secret), **changes)
        return await self.outcome(
            "update", ADMIN, target_id, candidate, expected_updated_at=self.stamp(target_id)
        )

    def reference(self, subsystem: str, target_id: int, name: str) -> None:
        self.run(
            "INSERT INTO automation_rule (name, subsystem, target_selector, parameters, "
            "created_at, updated_at) VALUES (?, ?, '', ?, 'x', 'x')",
            (name, subsystem, json.dumps({"object_storage_target_id": target_id})),
        )


@pytest.fixture
async def rig(provisioned_template: Path, tmp_path: Path) -> AsyncIterator[Rig]:
    path = tmp_path / "dockmin.db"
    shutil.copyfile(provisioned_template, path)
    database = Database(path)
    yield Rig(database, vault_with(FIRST_KEY))
    await database.aclose()


async def opened_secret(rig: Rig, target_id: int) -> str:
    async with rig.database.session() as session:
        opened = await ObjectStorageTargets(session, rig.vault).open(target_id)
    assert opened is not None
    return opened.credentials.secret_access_key.get_secret_value()


async def test_creating_a_target_seals_the_secret_and_writes_one_row_without_it(rig: Rig) -> None:
    target_id = await rig.call("create", ADMIN, form())

    [stored] = rig.rows("SELECT name, secret_access_key_ciphertext FROM object_storage_target")
    [event] = rig.events("object_storage_target_created")
    detail = event["detail"]
    assert stored["name"] == "primary" and SECRET not in stored["secret_access_key_ciphertext"]
    assert await opened_secret(rig, target_id) == SECRET
    assert (event["result"], event["target_id"]) == ("success", str(target_id))
    assert (detail["name"], detail["bucket"], detail["use_path_style"]) == ("primary", BUCKET, True)
    assert (detail["retention_count"], detail["endpoint_host"]) == (None, "127.0.0.1:29000")
    assert SECRET not in json.dumps(detail) and ACCESS not in json.dumps(detail)


async def test_a_name_taken_in_another_case_and_a_standard_user_are_refused_and_write_nothing(
    rig: Rig,
) -> None:
    await rig.call("create", ADMIN, form())

    taken = await rig.outcome("create", ADMIN, form(name="PRIMARY"))
    denied = await rig.outcome("create", STANDARD, form(name="other"))

    assert taken == {"name": NAME_TAKEN}
    assert denied == "denied"
    assert rig.targets() == 1


INVALID = {
    "endpoint-scheme": ({"endpoint_url": "ftp://x"}, "endpoint_url"),
    "endpoint-path": ({"endpoint_url": "http://x/p"}, "endpoint_url"),
    "region": ({"region": "bad region"}, "region"),
    "virtual-host-ip": ({"use_path_style": False}, "use_path_style"),
    "virtual-host-bucket": (
        {"use_path_style": False, "endpoint_url": VIRTUAL_HOST_ENDPOINT, "bucket": "Bad_Bucket"},
        "bucket",
    ),
    "path-style-bucket": ({"bucket": "-x"}, "bucket"),
    "access-key": ({"access_key_id": "bad key"}, "access_key_id"),
    "equal-prefixes": ({"backup_prefix": "p", "activity_log_prefix": "p"}, "activity_log_prefix"),
    "nested-prefixes": (
        {"backup_prefix": "a", "activity_log_prefix": "a/b"},
        "activity_log_prefix",
    ),
    "bad-prefix": ({"backup_prefix": "a//b"}, "backup_prefix"),
    "retention-text": ({"retention_count": "many"}, "retention_count"),
    "retention-zero": ({"retention_count": "0"}, "retention_count"),
    "retention-unconfirmed": ({"retention_count": "3"}, "confirm_retention"),
    "missing-secret": ({"secret_access_key": SecretStr("")}, "secret_access_key"),
    "empty-name": ({"name": " "}, "name"),
    "bundle-without-verification": ({"verify_tls": False, "ca_bundle": PEM_ROOT}, "ca_bundle"),
    "bundle-not-pem": ({"ca_bundle": "hello"}, "ca_bundle"),
}


@pytest.mark.parametrize(("changes", "field"), INVALID.values(), ids=INVALID.keys())
async def test_a_create_that_breaks_a_rule_is_refused_on_that_field_and_writes_no_target(
    rig: Rig, changes: dict[str, Any], field: str
) -> None:
    refused = await rig.outcome("create", ADMIN, form(**changes))

    assert isinstance(refused, dict) and field in refused
    assert rig.targets() == 0


async def test_an_invalid_ca_bundle_is_reported_together_with_the_other_field_errors(
    rig: Rig,
) -> None:
    refused = await rig.outcome(
        "create", ADMIN, form(name=" ", access_key_id="bad key", ca_bundle="hello")
    )

    assert isinstance(refused, dict)
    assert {"name", "access_key_id", "ca_bundle"} <= set(refused)
    assert "hello" not in refused["ca_bundle"]


async def test_a_refused_create_leaves_a_failure_row_that_does_not_hold_the_secret(
    rig: Rig,
) -> None:
    await rig.outcome("create", ADMIN, form(endpoint_url="ftp://x"))

    [event] = rig.events("object_storage_target_created")
    assert event["result"] == "failure"
    assert SECRET not in json.dumps(event["detail"])


async def test_a_name_and_prefix_change_with_a_blank_secret_keeps_the_stored_secret(
    rig: Rig,
) -> None:
    target_id = await rig.call("create", ADMIN, form())
    before = rig.rows("SELECT secret_access_key_ciphertext FROM object_storage_target")[0][0]

    renamed = await rig.outcome(
        "update",
        ADMIN,
        target_id,
        form(name="renamed", backup_prefix="bk", secret_access_key=SecretStr("")),
        expected_updated_at=rig.stamp(target_id),
    )

    [after] = rig.rows(
        "SELECT secret_access_key_ciphertext, name, backup_prefix FROM object_storage_target"
    )
    assert renamed == "ok"
    assert tuple(after) == (before, "renamed", "bk")


REDIRECTS = {
    "endpoint": {"endpoint_url": "http://127.0.0.1:29001"},
    "bucket": {"bucket": "other-bucket"},
    "addressing": {"use_path_style": False, "endpoint_url": VIRTUAL_HOST_ENDPOINT},
    "verification": {"verify_tls": False},
}


@pytest.mark.parametrize("change", REDIRECTS.values(), ids=REDIRECTS.keys())
async def test_changing_where_the_credential_is_sent_with_a_blank_secret_demands_re_entry(
    rig: Rig, change: dict[str, Any]
) -> None:
    target_id = await rig.call("create", ADMIN, form())

    refused = await rig.update(target_id, **change)

    assert isinstance(refused, dict) and "secret_access_key" in refused


async def test_a_new_secret_with_a_bucket_change_is_saved_and_the_row_says_so(rig: Rig) -> None:
    target_id = await rig.call("create", ADMIN, form())
    rig.clock.jump(STEP)

    saved = await rig.outcome(
        "update",
        ADMIN,
        target_id,
        form(bucket="other-bucket", secret_access_key=SecretStr(NEW_SECRET)),
        expected_updated_at=rig.stamp(target_id),
    )

    detail = rig.events("object_storage_target_updated")[-1]["detail"]
    assert saved == "ok"
    assert (detail["secret_replaced"], "bucket" in detail["changed"]) == (True, True)
    assert await opened_secret(rig, target_id) == NEW_SECRET


async def test_an_update_from_a_stale_form_or_to_a_missing_target_is_refused(rig: Rig) -> None:
    target_id = await rig.call("create", ADMIN, form())
    stale = rig.stamp(target_id)
    rig.clock.jump(STEP)
    await rig.call("update", ADMIN, target_id, form(name="renamed"), expected_updated_at=stale)

    refused = await rig.outcome(
        "update", ADMIN, target_id, form(name="again"), expected_updated_at=stale
    )
    missing = await rig.outcome("update", ADMIN, MISSING_ID, form(), expected_updated_at=stale)

    assert isinstance(refused, dict) and "form" in refused
    assert missing == "notfound"


async def test_an_update_that_changes_nothing_records_nothing(rig: Rig) -> None:
    target_id = await rig.call("create", ADMIN, form())
    before = len(rig.events("object_storage_target_updated"))

    result = await rig.update(target_id)

    assert result == "ok"
    assert len(rig.events("object_storage_target_updated")) == before


def verdict(result: Any) -> str:
    return "ok" if result == "ok" else next(iter(result))


RETENTION = [
    pytest.param(None, "3", False, "confirm_retention", id="setting-needs-the-box"),
    pytest.param(None, "3", True, "ok", id="setting-with-the-box"),
    pytest.param(5, "2", False, "confirm_retention", id="lowering-needs-the-box"),
    pytest.param(2, "5", False, "ok", id="raising-needs-no-box"),
    pytest.param(2, "", False, "ok", id="clearing-needs-no-box"),
]


@pytest.mark.parametrize(("stored", "new", "confirmed", "expected"), RETENTION)
async def test_retention_that_deletes_more_archives_must_be_confirmed(
    rig: Rig, stored: int | None, new: str, confirmed: bool, expected: str
) -> None:
    target_id = await rig.call("create", ADMIN, form())
    rig.run("UPDATE object_storage_target SET retention_count = ?", (stored,))

    result = await rig.update(target_id, retention_count=new, confirm_retention=confirmed)

    assert verdict(result) == expected


async def test_a_target_view_and_a_page_of_targets_never_hold_the_secret(
    rig: Rig,
) -> None:
    target_id = await rig.call("create", ADMIN, form(retention_count="3", confirm_retention=True))

    view = await rig.call("get", ADMIN, target_id)
    page = await rig.call("page", ADMIN)

    assert SECRET not in repr(view) and SECRET not in repr(page)
    assert (view.retention_count, view.use_path_style) == (3, True)
    assert [item.name for item in page.items] == ["primary"]


async def test_a_ca_bundle_is_stored_listed_kept_on_a_blank_edit_and_removed_on_request(
    rig: Rig, certificates: Certificates
) -> None:
    target_id = await rig.call(
        "create", ADMIN, form(endpoint_url="https://s3.example.test", ca_bundle=certificates.ca_pem)
    )
    created = await rig.call("get", ADMIN, target_id)

    kept = await rig.update(target_id, endpoint_url="https://s3.example.test")
    after_keep = await rig.call("get", ADMIN, target_id)
    removed = await rig.update(
        target_id,
        endpoint_url="https://s3.example.test",
        remove_ca_bundle=True,
        secret=NEW_SECRET,
    )
    after_remove = await rig.call("get", ADMIN, target_id)

    assert created.ca_bundle_set and len(created.ca_certificates) == 1
    assert kept == "ok" and after_keep.ca_bundle_set
    assert removed == "ok" and not after_remove.ca_bundle_set


@pytest.fixture(scope="module")
def certificates() -> Certificates:
    return build_certificates()


async def test_deleting_is_blocked_while_rules_use_the_target_and_the_preview_names_them(
    rig: Rig,
) -> None:
    target_id = await rig.call("create", ADMIN, form())
    rig.reference("backup-syncer", target_id, "sync")
    rig.reference("activity-log-exporter", target_id, "export")

    preview = await rig.call("delete_preview", ADMIN, target_id)
    refused = await rig.outcome("delete", ADMIN, target_id, expected_name="primary")

    [failure] = rig.events("object_storage_target_deleted")
    assert preview.blocked and not preview.more_rules
    assert {rule.subsystem for rule in preview.rules} == {"backup-syncer", "activity-log-exporter"}
    assert isinstance(refused, dict) and "target" in refused and rig.targets() == 1
    assert (failure["result"], failure["detail"]["reason"]) == ("failure", "target_in_use")


async def test_deleting_needs_the_name_as_reviewed_and_keeps_the_records_of_the_target(
    rig: Rig,
) -> None:
    target_id = await rig.call("create", ADMIN, form())
    rig.run(
        "INSERT INTO backup_record (origin, object_storage_target_id, created_at, key_version, "
        "key_fingerprint, outcome) "
        "VALUES ('sync', ?, '2026-01-01T00:00:00Z', 1, 'aaaa', 'success')",
        (target_id,),
    )

    changed = await rig.outcome("delete", ADMIN, target_id, expected_name="wrong")
    removed = await rig.outcome("delete", ADMIN, target_id, expected_name="primary")

    assert isinstance(changed, dict) and "name" in changed
    assert removed == "ok" and rig.targets() == 0
    assert rig.rows("SELECT object_storage_target_id FROM backup_record")[0][0] is None
    assert await rig.outcome("delete", STANDARD, target_id, expected_name="x") == "denied"


class Bucket:
    """Answers the three probes of a connection test."""

    def __init__(self) -> None:
        self.refuse_listing = False
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        verify_signature(request)
        self.requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(200)
        if self.refuse_listing:
            return httpx.Response(403, content=b"<Error><Code>AccessDenied</Code></Error>")
        return httpx.Response(200, content=EMPTY_LISTING.encode())


async def test_a_healthy_bucket_passes_all_three_stages_and_the_row_says_so(rig: Rig) -> None:
    target_id = await rig.call("create", ADMIN, form())
    bucket = Bucket()

    checks = await rig.call("test", ADMIN, target_id, transport=httpx.MockTransport(bucket))

    detail = rig.events("object_storage_target_tested")[-1]["detail"]
    assert [(c.stage, c.passed) for c in checks] == [
        ("bucket", True),
        ("backup_prefix", True),
        ("activity_log_prefix", True),
    ]
    assert detail["passed"] is True and "duration_ms" in detail


async def test_a_refused_listing_fails_both_prefix_stages_and_the_bucket_stage_still_passes(
    rig: Rig,
) -> None:
    target_id = await rig.call("create", ADMIN, form())
    bucket = Bucket()
    bucket.refuse_listing = True

    checks = await rig.call("test", ADMIN, target_id, transport=httpx.MockTransport(bucket))

    detail = rig.events("object_storage_target_tested")[-1]["detail"]
    assert [(c.stage, c.passed, c.kind) for c in checks] == [
        ("bucket", True, None),
        ("backup_prefix", False, "refused"),
        ("activity_log_prefix", False, "refused"),
    ]
    assert (detail["failed_stage"], detail["storage_status"], detail["storage_code"]) == (
        "backup_prefix",
        403,
        "AccessDenied",
    )
    assert "remote retention cannot run" in stage_message(checks[1])


async def test_a_test_by_a_standard_user_or_of_a_missing_target_is_refused(rig: Rig) -> None:
    target_id = await rig.call("create", ADMIN, form())

    denied = await rig.outcome("test", STANDARD, target_id)
    missing = await rig.outcome("test", ADMIN, MISSING_ID)

    assert (denied, missing) == ("denied", "notfound")


async def test_a_secret_the_key_cannot_read_is_reported_as_a_field_to_enter_again(
    rig: Rig,
) -> None:
    target_id = await rig.call("create", ADMIN, form())
    rig.run("UPDATE object_storage_target SET secret_access_key_ciphertext = ?", (UNREADABLE,))

    refused = await rig.outcome("test", ADMIN, target_id)

    assert isinstance(refused, dict) and "secret_access_key" in refused


async def test_the_summaries_list_the_id_and_name_of_each_target_by_name(rig: Rig) -> None:
    second = await rig.call("create", ADMIN, form(name="b-target", backup_prefix="b"))
    first = await rig.call("create", ADMIN, form(name="a-target", backup_prefix="a"))

    assert await rig.call("summaries", ADMIN) == [(first, "a-target"), (second, "b-target")]
    assert await rig.outcome("summaries", STANDARD) == "denied"


WARNINGS: list[tuple[str, bool, int]] = [
    ("https://s3.example.com", True, 0),
    ("http://s3.example.com", True, 1),
    ("https://s3.example.com", False, 1),
    ("http://127.0.0.1:9000", True, 2),
    ("https://localhost", True, 1),
    ("https://169.254.169.254", True, 1),
    ("https://[fd00:ec2::254]", True, 1),
    ("http://127.0.0.1:9000", False, 3),
]


@pytest.mark.parametrize(("endpoint", "verify", "count"), WARNINGS)
def test_a_warning_is_shown_for_plain_http_for_unverified_certificates_and_for_local_addresses(
    endpoint: str, verify: bool, count: int
) -> None:
    assert len(target_warnings(endpoint, verify)) == count


@pytest.mark.parametrize(
    "endpoint",
    [
        ENDPOINT,
        "http://localhost:9000",
        "http://[::1]:9000",
        "http://169.254.169.254",
        "http://metadata.google.internal",
        "http://[::ffff:127.0.0.1]:9000",
    ],
)
async def test_an_endpoint_on_an_internal_address_is_refused_on_its_field_and_writes_nothing(
    rig: Rig, address_policy: Callable[..., None], endpoint: str
) -> None:
    address_policy()

    refused = await rig.outcome("create", ADMIN, form(endpoint_url=endpoint))

    assert refused == {"endpoint_url": REFUSAL_TEXT}
    assert rig.targets() == 0
    assert rig.events("object_storage_target_created")[0]["result"] == "failure"


async def test_a_permit_lets_the_same_endpoint_be_saved(
    rig: Rig, address_policy: Callable[..., None]
) -> None:
    address_policy("127.0.0.0/8")

    saved = await rig.outcome("create", ADMIN, form())

    assert saved == "ok" and rig.targets() == 1


async def test_a_permit_for_one_network_does_not_cover_another(
    rig: Rig, address_policy: Callable[..., None]
) -> None:
    address_policy("127.0.0.0/8")

    refused = await rig.outcome("create", ADMIN, form(endpoint_url="http://169.254.169.254"))

    assert refused == {"endpoint_url": REFUSAL_TEXT}


async def test_a_target_whose_permit_was_withdrawn_cannot_be_edited_until_its_endpoint_changes(
    rig: Rig, address_policy: Callable[..., None]
) -> None:
    address_policy("127.0.0.0/8")
    target_id = await rig.call("create", ADMIN, form())
    address_policy()

    unchanged = await rig.update(target_id, backup_prefix="other")
    moved = await rig.update(target_id, endpoint_url="https://s3.example.test", secret=NEW_SECRET)

    assert unchanged == {"endpoint_url": REFUSAL_TEXT}
    assert moved == "ok"


@pytest.mark.parametrize(
    ("endpoint", "count"),
    [
        ("http://[::ffff:127.0.0.1]:9000", 2),
        ("https://metadata.google.internal", 1),
        ("https://instance-data.ec2.internal", 1),
        ("https://s3.localhost", 1),
        ("https://s3.example.test", 0),
    ],
)
def test_a_warning_names_an_internal_address_whether_or_not_a_permit_covers_it(
    endpoint: str, count: int, address_policy: Callable[..., None]
) -> None:
    address_policy("127.0.0.0/8", "169.254.0.0/16", "::1/128")

    assert len(target_warnings(endpoint, True)) == count
