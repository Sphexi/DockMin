"""The stored-secret overview, the read-only check and the re-key under the offline gate."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import app.services.encryption as encryption_module
from app.auth.errors import PermissionDenied, ValidationFailed
from app.config import PREVIOUS_SECRET_KEY_VAR, SECRET_KEY_VAR, Settings, load_settings
from app.crypto.keys import MasterKeys, key_fingerprint
from app.crypto.registry import REGISTRY_CREDENTIAL_SECRET, SECRET_FIELDS
from app.crypto.startup import establish_vault
from app.db.repositories.crypto_state import CryptoStateRepository
from app.logging import Scrubber
from app.services.encryption import (
    BACKUP_BUSY_MESSAGE,
    CONFIRM_FIELD,
    CONFIRM_MESSAGE,
    RESTORE_ACTIVE_MESSAGE,
    RUN_ACTIVE_MESSAGE,
    STALE_COUNTS_MESSAGE,
    STOPPING_MESSAGE,
    EncryptionConflict,
    EncryptionService,
    RekeyRun,
    RekeyRunNotFound,
    counts_digest,
)
from app.services.restore import UPDATE_IN_PROGRESS, RestoreBusy, RestoreCoordinator
from tests.crypto.helpers import CredentialRepository, tamper
from tests.encryption_support import Steps
from tests.restore_support import (
    ADMINISTRATOR,
    LIVE_KEYS,
    OPERATOR,
    STANDARD,
    Instance,
    make_instance,
)
from tests.support import VALID_KEY

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OWNER_ID = "r" * 32
UPDATE_ID = "ab" * 16
SETTLE_POLLS = 1200
SETTLE_PAUSE = 0.01
SHORT_WAIT = 0.05
CURRENT_VERSION = 2
OLD_VERSION = 1
SECRET_COLUMNS = len(SECRET_FIELDS)
UNPARSEABLE = "not-a-token"
UNKNOWN_RUN = "f" * 32
REGISTRY_PLAINTEXT = "plain-registry_credential-secret_ciphertext"

Counts = dict[tuple[str, str], dict[int, int]]


def encoded(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


@pytest.fixture
def steps(monkeypatch: pytest.MonkeyPatch) -> Steps:
    held = Steps()
    monkeypatch.setattr(encryption_module, "RekeyPass", held)
    return held


@pytest.fixture
async def instance(tmp_path: Path) -> AsyncIterator[Instance]:
    built = await make_instance(tmp_path / "live")
    yield built
    await built.close()


def build_service(
    instance: Instance, settings: Settings | None = None, *, wait_seconds: float = SHORT_WAIT
) -> EncryptionService:
    return EncryptionService(
        gate=instance.gate,
        production=instance.production,
        data_dir=instance.data_dir,
        runtime=instance.runtime,
        settings=settings or load_settings({SECRET_KEY_VAR: VALID_KEY}),
        clock=instance.clock,
        wait_seconds=wait_seconds,
    )


@pytest.fixture
async def service(instance: Instance) -> AsyncIterator[EncryptionService]:
    built = build_service(instance)
    yield built
    await built.aclose()


async def settle(service: EncryptionService, run_id: str) -> RekeyRun:
    for _ in range(SETTLE_POLLS):
        run = await service.run(ADMINISTRATOR, run_id)
        if run.phase == "finished":
            return run
        await asyncio.sleep(SETTLE_PAUSE)
    raise TimeoutError("the run did not finish")


async def counts_of(instance: Instance) -> Counts:
    async with instance.database.session() as db:
        return await CryptoStateRepository(db).count_by_field_and_version()


async def reviewed_digest(service: EncryptionService) -> str:
    return (await service.overview(ADMINISTRATOR)).counts_digest


def stored_secrets(instance: Instance) -> list[tuple[str, int, str]]:
    return [
        (field.table, row["id"], row[field.column])
        for field in SECRET_FIELDS
        for row in instance.live_rows(f"SELECT id, {field.column} FROM {field.table} ORDER BY id")
    ]


def activity_rows(instance: Instance) -> int:
    return int(instance.live_rows("SELECT COUNT(*) FROM activity_event")[0][0])


def detail_of(row: Any) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(row["detail"])
    return decoded


def plant_damage(instance: Instance) -> str:
    """Corrupt the one current-version registry value and make a target secret unparseable."""
    sealed = instance.live_rows("SELECT secret_ciphertext FROM registry_credential WHERE id = 2")[
        0
    ][0]
    instance.world.run(
        "UPDATE registry_credential SET secret_ciphertext = ? WHERE id = 2", (tamper(sealed),)
    )
    instance.world.run(
        "UPDATE object_storage_target SET secret_access_key_ciphertext = ?", (UNPARSEABLE,)
    )
    return str(sealed)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(
            {("t", "a"): {1: 2, 2: 3}, ("t", "b"): {2: 1}},
            {("t", "b"): {2: 1}, ("t", "a"): {2: 3, 1: 2}},
            id="order-of-columns-and-versions",
        ),
        pytest.param({}, {}, id="empty"),
    ],
)
def test_counts_digest_is_the_same_for_equal_counts(left: Counts, right: Counts) -> None:
    assert counts_digest(left) == counts_digest(right)


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({("t", "a"): {1: 2, 2: 4}}, id="count"),
        pytest.param({("t", "a"): {1: 2, 3: 3}}, id="version"),
        pytest.param({("t", "a"): {1: 2}}, id="missing-version"),
        pytest.param({("u", "a"): {1: 2, 2: 3}}, id="table"),
        pytest.param({("t", "c"): {1: 2, 2: 3}}, id="column"),
        pytest.param({}, id="empty"),
    ],
)
def test_counts_digest_differs_when_any_count_version_or_column_differs(
    changed: Counts,
) -> None:
    assert counts_digest(changed) != counts_digest({("t", "a"): {1: 2, 2: 3}})


def test_counts_digest_is_a_sha256_hex_string() -> None:
    digest = counts_digest({("t", "a"): {1: 1}})

    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda service: service.overview(STANDARD), id="overview"),
        pytest.param(lambda service: service.check(STANDARD), id="check"),
        pytest.param(lambda service: service.run(STANDARD, UNKNOWN_RUN), id="run"),
        pytest.param(
            lambda service: service.begin_rekey(STANDARD, expected_counts="", confirmed=True),
            id="rekey",
        ),
    ],
)
async def test_a_standard_user_can_use_no_part_of_the_service(
    service: EncryptionService,
    instance: Instance,
    call: Callable[[EncryptionService], Awaitable[object]],
) -> None:
    with pytest.raises(PermissionDenied):
        await call(service)

    assert instance.gate.phase == "open" and not instance.production.held


async def test_the_overview_reports_the_keys_and_the_count_of_values_per_column_and_version(
    instance: Instance,
) -> None:
    settings = load_settings(
        {SECRET_KEY_VAR: encoded(LIVE_KEYS[1]), PREVIOUS_SECRET_KEY_VAR: encoded(LIVE_KEYS[0])}
    )
    service = build_service(instance, settings)

    overview = await service.overview(ADMINISTRATOR)

    by_column = {(c.table, c.column): dict(c.by_version) for c in overview.columns}
    assert overview.key_source == "variable"
    assert overview.key_version == CURRENT_VERSION
    assert overview.key_fingerprint == key_fingerprint(LIVE_KEYS[1])
    assert overview.previous_key is not None
    assert overview.previous_key.fingerprint == key_fingerprint(LIVE_KEYS[0])
    assert overview.previous_key.source == settings.previous_key_description
    assert by_column[(REGISTRY_CREDENTIAL_SECRET.table, REGISTRY_CREDENTIAL_SECRET.column)] == {
        OLD_VERSION: 1,
        CURRENT_VERSION: 1,
    }
    assert overview.values_below_current == SECRET_COLUMNS
    assert overview.counts_digest == counts_digest(await counts_of(instance))
    assert overview.last_run is None


async def test_the_overview_reports_no_previous_key_when_none_is_configured(
    service: EncryptionService,
) -> None:
    overview = await service.overview(ADMINISTRATOR)

    assert overview.previous_key is None


async def test_a_run_nobody_started_is_not_found(service: EncryptionService) -> None:
    with pytest.raises(RekeyRunNotFound):
        await service.run(ADMINISTRATOR, UNKNOWN_RUN)


async def test_a_check_leaves_the_gate_open_and_changes_nothing_but_its_own_activity_row(
    instance: Instance, service: EncryptionService, steps: Steps
) -> None:
    before_values = stored_secrets(instance)
    before_rows = activity_rows(instance)

    run_id = await service.check(ADMINISTRATOR)
    await steps.reached.wait()
    during = (
        instance.gate.phase,
        instance.production.held,
        instance.suspension.entered,
        service.check_running(),
    )
    steps.proceed.set()
    run = await settle(service, run_id)

    [row] = instance.actions("encryption_values_checked")
    assert during == ("open", False, 0, True)
    assert (run.kind, run.outcome, run.rewritten) == ("check", "complete", 0)
    assert run.checked == 1 and run.failures == ()
    assert (row["result"], row["actor_user_id"]) == ("success", ADMINISTRATOR.user_id)
    assert detail_of(row) == {"checked": 1, "corrupt": 0, "unparseable": 0}
    assert stored_secrets(instance) == before_values
    assert activity_rows(instance) == before_rows + 1
    assert not service.check_running()


async def test_a_check_reports_each_value_that_does_not_open_by_location_and_never_by_value(
    instance: Instance, service: EncryptionService
) -> None:
    sealed = plant_damage(instance)
    before = stored_secrets(instance)

    run = await settle(service, await service.check(ADMINISTRATOR))

    [row] = instance.actions("encryption_values_checked")
    reported = {(f.field.table, f.row_id, f.kind) for f in run.failures}
    assert run.outcome == "values_unreadable"
    assert (run.corrupt, run.unparseable) == (1, 1)
    assert ("registry_credential", 2, "corrupt") in reported
    assert ("object_storage_target", 1, "unparseable") in reported
    assert detail_of(row)["corrupt"] == 1 and detail_of(row)["unparseable"] == 1
    assert stored_secrets(instance) == before
    assert sealed not in repr(run) and UNPARSEABLE not in repr(run)
    assert REGISTRY_PLAINTEXT not in repr(run)


async def test_a_finished_run_is_forgotten_after_half_an_hour(
    instance: Instance, service: EncryptionService
) -> None:
    run_id = await service.check(ADMINISTRATOR)
    await settle(service, run_id)

    instance.clock.jump(31 * 60)

    with pytest.raises(RekeyRunNotFound):
        await service.run(ADMINISTRATOR, run_id)


async def test_a_rekey_that_is_not_confirmed_names_the_field_and_takes_nothing(
    instance: Instance, service: EncryptionService
) -> None:
    digest = await reviewed_digest(service)
    before = stored_secrets(instance)

    with pytest.raises(ValidationFailed) as raised:
        await service.begin_rekey(ADMINISTRATOR, expected_counts=digest, confirmed=False)

    assert raised.value.fields == {CONFIRM_FIELD: CONFIRM_MESSAGE}
    assert instance.gate.phase == "open" and not instance.production.held
    assert instance.suspension.entered == 0
    assert stored_secrets(instance) == before
    assert instance.actions("encryption_rekey_completed") == []


async def test_a_rekey_with_counts_that_changed_since_the_review_is_refused_and_rewrites_nothing(
    instance: Instance, service: EncryptionService
) -> None:
    digest = await reviewed_digest(service)
    async with instance.database.session() as db:
        await CredentialRepository(db, instance.world.vault).add("added-later", "plain-later")
    before = stored_secrets(instance)

    with pytest.raises(EncryptionConflict) as raised:
        await service.begin_rekey(ADMINISTRATOR, expected_counts=digest, confirmed=True)

    assert str(raised.value) == STALE_COUNTS_MESSAGE
    assert stored_secrets(instance) == before
    assert instance.gate.phase == "open" and not instance.production.held
    assert instance.suspension.entered == 0
    assert instance.actions("encryption_rekey_completed") == []


async def test_a_rekey_with_nothing_below_the_current_version_is_refused(
    instance: Instance, service: EncryptionService
) -> None:
    await settle(
        service,
        await service.begin_rekey(
            ADMINISTRATOR, expected_counts=await reviewed_digest(service), confirmed=True
        ),
    )

    with pytest.raises(EncryptionConflict) as raised:
        await service.begin_rekey(
            ADMINISTRATOR, expected_counts=await reviewed_digest(service), confirmed=True
        )

    assert str(raised.value) == f"Every stored secret is already at key version {CURRENT_VERSION}."


def block_with_a_restore(instance: Instance) -> None:
    instance.gate.engage(OWNER_ID, NOW, purpose="restore")


def block_with_an_update(instance: Instance) -> None:
    (instance.data_dir / "update" / UPDATE_ID).mkdir(parents=True)


def block_with_a_job(instance: Instance) -> None:
    instance.images.count = 1


def block_with_a_backup(instance: Instance) -> None:
    assert instance.production.try_hold()


@pytest.mark.parametrize(
    ("block", "message"),
    [
        pytest.param(block_with_a_restore, RESTORE_ACTIVE_MESSAGE, id="restore-engaged"),
        pytest.param(block_with_an_update, UPDATE_IN_PROGRESS, id="update-directory"),
        pytest.param(block_with_a_job, "1 jobs are running; wait for them to finish.", id="job"),
        pytest.param(block_with_a_backup, BACKUP_BUSY_MESSAGE, id="backup-in-production"),
    ],
)
async def test_a_rekey_is_refused_while_something_else_holds_the_instance(
    instance: Instance,
    service: EncryptionService,
    block: Callable[[Instance], None],
    message: str,
) -> None:
    digest = await reviewed_digest(service)
    block(instance)
    before = stored_secrets(instance)
    phase, held = instance.gate.phase, instance.production.held

    with pytest.raises(EncryptionConflict) as raised:
        await service.begin_rekey(ADMINISTRATOR, expected_counts=digest, confirmed=True)

    assert str(raised.value) == message
    assert stored_secrets(instance) == before
    assert (instance.gate.phase, instance.production.held) == (phase, held)
    assert instance.suspension.entered == 0
    assert instance.actions("encryption_rekey_completed") == []


async def test_a_confirmed_rekey_holds_the_instance_then_leaves_nothing_below_the_current_version(
    instance: Instance, service: EncryptionService, steps: Steps
) -> None:
    digest = await reviewed_digest(service)

    run_id = await service.begin_rekey(ADMINISTRATOR, expected_counts=digest, confirmed=True)
    await steps.reached.wait()
    during = (
        instance.gate.phase,
        instance.gate.purpose,
        instance.production.held,
        (instance.suspension.entered, instance.suspension.exited),
    )
    steps.proceed.set()
    run = await settle(service, run_id)

    requested = instance.actions("encryption_rekey_requested")
    completed = instance.actions("encryption_rekey_completed")
    ordered = [row["action"] for row in instance.actions("encryption_rekey_%")]
    assert during == ("engaged", "rekey", True, (1, 0))
    assert (run.outcome, run.rewritten, run.remaining_below_current) == (
        "complete",
        SECRET_COLUMNS,
        0,
    )
    assert ordered == ["encryption_rekey_requested", "encryption_rekey_completed"]
    assert [(r["result"], r["actor_user_id"]) for r in requested + completed] == [
        ("success", ADMINISTRATOR.user_id)
    ] * 2
    assert detail_of(requested[0]) == {
        "key_version": CURRENT_VERSION,
        "values_below_current": SECRET_COLUMNS,
    }
    assert detail_of(completed[0])["remaining_below_current"] == 0
    assert detail_of(completed[0])["rewritten"] == SECRET_COLUMNS
    assert "reason" not in detail_of(completed[0])
    assert await instance_versions(instance) == {CURRENT_VERSION: SECRET_COLUMNS + 1}
    assert (instance.gate.phase, instance.production.held) == ("open", False)
    assert (instance.suspension.entered, instance.suspension.exited) == (1, 1)


async def instance_versions(instance: Instance) -> dict[int, int]:
    async with instance.database.session() as db:
        return await CryptoStateRepository(db).count_by_version()


async def test_after_a_rekey_the_instance_starts_with_the_current_key_alone(
    instance: Instance, service: EncryptionService
) -> None:
    await settle(
        service,
        await service.begin_rekey(
            ADMINISTRATOR, expected_counts=await reviewed_digest(service), confirmed=True
        ),
    )

    vault = await establish_vault(instance.database, MasterKeys(LIVE_KEYS[1]), Scrubber())

    assert vault.current_version == CURRENT_VERSION
    async with instance.database.session() as db:
        revealed = await CredentialRepository(db, vault).reveal(REGISTRY_CREDENTIAL_SECRET, 1)
    assert revealed == REGISTRY_PLAINTEXT


async def test_a_rekey_that_meets_an_unopenable_value_ends_in_failure_and_rewrites_the_rest(
    instance: Instance, service: EncryptionService
) -> None:
    instance.world.run(
        "UPDATE object_storage_target SET secret_access_key_ciphertext = ?", (UNPARSEABLE,)
    )

    run = await settle(
        service,
        await service.begin_rekey(
            ADMINISTRATOR, expected_counts=await reviewed_digest(service), confirmed=True
        ),
    )

    [row] = instance.actions("encryption_rekey_completed")
    assert run.outcome == "values_unreadable"
    assert row["result"] == "failure" and instance.reason_of(row) == "values_unreadable"
    assert {(f.field.table, f.row_id) for f in run.failures} == {("object_storage_target", 1)}
    assert run.rewritten == SECRET_COLUMNS - 1
    assert (instance.gate.phase, instance.production.held) == ("open", False)
    assert instance.suspension.exited == 1


async def test_a_rekey_stopped_by_shutdown_leaves_every_value_readable_and_the_next_one_resumes(
    instance: Instance, service: EncryptionService, steps: Steps
) -> None:
    run_id = await service.begin_rekey(
        ADMINISTRATOR, expected_counts=await reviewed_digest(service), confirmed=True
    )
    await steps.reached.wait()

    closing = asyncio.ensure_future(service.aclose())
    await asyncio.sleep(0)
    steps.proceed.set()
    await closing

    run = await service.run(ADMINISTRATOR, run_id)
    [row] = instance.actions("encryption_rekey_completed")
    mixed = await instance_versions(instance)
    async with instance.database.session() as db:
        reader = CredentialRepository(db, instance.world.vault)
        revealed = [
            await reader.reveal(REGISTRY_CREDENTIAL_SECRET, 1),
            await reader.reveal(next(f for f in SECRET_FIELDS if f.table == "stack_variable"), 1),
        ]
    assert (run.phase, run.outcome) == ("finished", "interrupted")
    assert (row["result"], instance.reason_of(row)) == ("failure", "interrupted")
    assert set(mixed) == {OLD_VERSION, CURRENT_VERSION}
    assert revealed[0] == REGISTRY_PLAINTEXT
    assert revealed[1] == "plain-stack_variable-value_ciphertext"
    assert (instance.gate.phase, instance.production.held) == ("open", False)
    assert instance.suspension.exited == 1

    resumed = build_service(instance)
    finished = await settle(
        resumed,
        await resumed.begin_rekey(
            ADMINISTRATOR, expected_counts=await reviewed_digest(resumed), confirmed=True
        ),
    )
    await resumed.aclose()
    assert finished.outcome == "complete"
    assert set(await instance_versions(instance)) == {CURRENT_VERSION}


async def test_a_service_that_is_shutting_down_starts_nothing(instance: Instance) -> None:
    stopping = build_service(instance)
    await stopping.aclose()

    with pytest.raises(EncryptionConflict) as check:
        await stopping.check(ADMINISTRATOR)
    with pytest.raises(EncryptionConflict) as rekey:
        await stopping.begin_rekey(ADMINISTRATOR, expected_counts="", confirmed=True)

    assert str(check.value) == str(rekey.value) == STOPPING_MESSAGE


async def test_a_running_check_refuses_a_second_check_and_a_rekey(
    service: EncryptionService, steps: Steps
) -> None:
    await service.check(ADMINISTRATOR)
    await steps.reached.wait()

    with pytest.raises(EncryptionConflict) as second:
        await service.check(ADMINISTRATOR)
    with pytest.raises(EncryptionConflict) as rekey:
        await service.begin_rekey(ADMINISTRATOR, expected_counts="", confirmed=True)

    steps.proceed.set()
    assert str(second.value) == str(rekey.value) == RUN_ACTIVE_MESSAGE


def restore_coordinator(instance: Instance, service: EncryptionService) -> RestoreCoordinator:
    return RestoreCoordinator(
        gate=instance.gate,
        production=instance.production,
        data_dir=instance.data_dir,
        runtime=instance.runtime,
        stop_runtime=instance.stop,
        start_runtime=instance.start,
        clock=instance.clock,
        sleep=instance.sleep,
        busy=service.check_running,
    )


async def test_a_running_check_blocks_a_restore_from_beginning(
    instance: Instance, service: EncryptionService, steps: Steps
) -> None:
    coordinator = restore_coordinator(instance, service)
    await service.check(ADMINISTRATOR)
    await steps.reached.wait()

    with pytest.raises(RestoreBusy) as raised:
        await coordinator.begin(OPERATOR, session_id=1)

    steps.proceed.set()
    assert "stored-secret check is running" in str(raised.value)
    assert instance.gate.phase == "open" and not instance.production.held
    await coordinator.aclose()


async def test_a_restore_begins_once_the_check_has_finished(
    instance: Instance, service: EncryptionService
) -> None:
    coordinator = restore_coordinator(instance, service)
    await settle(service, await service.check(ADMINISTRATOR))

    restore_id = await coordinator.begin(OPERATOR, session_id=1)

    assert (instance.gate.phase, instance.gate.restore_id) == ("engaged", restore_id)
    await coordinator.aclose()
