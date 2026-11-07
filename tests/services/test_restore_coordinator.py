"""The restore coordinator up to the key: engaging, ownership, review, the key step, cancel."""

import asyncio
import base64
import json
import shutil
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import app.backup.pipeline as pipeline_module
import app.services.restore as restore_module
from app.auth.errors import PermissionDenied, ValidationFailed
from app.auth.principal import Principal
from app.backup import StopRequested
from app.services.restore import (
    UPDATE_IN_PROGRESS,
    RestoreBusy,
    RestoreNotFound,
    sweep_restore_leftovers,
)
from tests.crypto.helpers import THIRD_KEY
from tests.restore_support import (
    IDLE_SECONDS,
    OPERATOR,
    OTHER_SESSION,
    STANDARD,
    Instance,
    begin_and_upload,
    confirm_arguments,
    feed,
    key_for_archive,
    make_instance,
    sha,
    source_archive,
)

ENGAGE_WAIT = 30.0
RESTORE_ID_LENGTH = 32
UPDATE_ID = "ab" * 16
SECOND_ADMINISTRATOR_SESSION = 9
SETTLE_TICKS = 50
STALLED_UPLOAD_BYTES = 100_000
PROMPT_SECONDS = 2.0
JUNK = b"\x1f\x8b" + b"junk" * 1000


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    built, world = asyncio.run(source_archive(tmp_path_factory.mktemp("restore-source")))
    asyncio.run(world.database.aclose())
    return built


@pytest.fixture
async def instance(tmp_path: Path) -> AsyncIterator[Instance]:
    built = await make_instance(tmp_path / "live")
    yield built
    await built.close()


async def until(condition: Callable[[], object]) -> None:
    for _ in range(SETTLE_TICKS * 10):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("the condition did not become true")


def second_administrator(instance: Instance) -> Principal:
    instance.world.run(
        "INSERT INTO user (username, password_hash, role, is_enabled, created_at) "
        "VALUES ('admin-two', 'x', 'administrator', 1, '2026-01-01T00:00:00Z')"
    )
    user_id = instance.live_rows("SELECT id FROM user WHERE username = 'admin-two'")[0][0]
    return Principal(user_id, "admin-two", "administrator", SECOND_ADMINISTRATOR_SESSION)


def refusal_fields(error: ValidationFailed) -> tuple[str, ...]:
    return tuple(error.fields)


async def test_a_standard_user_cannot_begin_a_restore(instance: Instance) -> None:
    assert instance.coordinator is not None

    with pytest.raises(PermissionDenied):
        await instance.coordinator.begin(STANDARD, session_id=2)

    assert instance.gate.phase == "open" and not instance.production.held


async def test_beginning_engages_the_gate_holds_production_suspends_the_scheduler_and_logs_once(
    instance: Instance,
) -> None:
    assert instance.coordinator is not None

    restore_id = await instance.coordinator.begin(OPERATOR, session_id=1)

    rows = instance.actions("instance_restore_started")
    assert len(restore_id) == RESTORE_ID_LENGTH
    assert (instance.gate.phase, instance.gate.restore_id) == ("engaged", restore_id)
    assert instance.production.held
    assert (instance.suspension.entered, instance.suspension.exited) == (1, 0)
    assert [(r["result"], r["actor_user_id"]) for r in rows] == [("success", 1)]
    assert json.loads(rows[0]["detail"])["restore_id"] == restore_id


async def test_a_second_restore_cannot_begin_while_one_is_engaged(instance: Instance) -> None:
    assert instance.coordinator is not None
    await instance.coordinator.begin(OPERATOR, session_id=1)

    with pytest.raises(RestoreBusy):
        await instance.coordinator.begin(OPERATOR, session_id=1)


async def test_running_jobs_refuse_a_restore_and_the_message_counts_them(
    instance: Instance,
) -> None:
    assert instance.coordinator is not None
    instance.images.count, instance.stacks.count = 1, 2

    with pytest.raises(RestoreBusy) as raised:
        await instance.coordinator.begin(OPERATOR, session_id=1)

    assert str(raised.value) == "3 jobs are running; wait for them to finish."
    assert instance.gate.phase == "open" and not instance.production.held
    assert instance.suspension.entered == 0


async def test_a_restore_is_refused_while_an_update_is_in_progress(instance: Instance) -> None:
    assert instance.coordinator is not None
    (instance.data_dir / "update" / UPDATE_ID).mkdir(parents=True)

    with pytest.raises(RestoreBusy) as raised:
        await instance.coordinator.begin(OPERATOR, session_id=1)

    assert str(raised.value) == UPDATE_IN_PROGRESS
    assert instance.gate.phase == "open" and not instance.production.held
    assert instance.suspension.entered == 0


async def test_a_backup_in_production_is_waited_for_and_then_refused_after_thirty_seconds(
    instance: Instance,
) -> None:
    assert instance.coordinator is not None
    assert instance.production.try_hold()
    attempt = asyncio.create_task(instance.coordinator.begin(OPERATOR, session_id=1))
    await until(lambda: instance.slept == [ENGAGE_WAIT])

    instance.waiting[0].set_result(None)

    with pytest.raises(RestoreBusy, match="still being produced"):
        await attempt
    assert instance.gate.phase == "open" and instance.production.held


async def test_a_restore_takes_the_lock_once_a_production_in_progress_finishes(
    instance: Instance,
) -> None:
    assert instance.coordinator is not None
    assert instance.production.try_hold()
    attempt = asyncio.create_task(instance.coordinator.begin(OPERATOR, session_id=1))
    await until(lambda: instance.slept)

    instance.production.free()
    await asyncio.wait_for(attempt, PROMPT_SECONDS * 5)

    assert instance.production.held and instance.gate.phase == "engaged"
    assert (instance.suspension.entered, instance.suspension.exited) == (1, 0)


async def test_cancelling_during_the_wait_for_production_ends_the_attempt_and_leaves_the_gate_open(
    instance: Instance,
) -> None:
    assert instance.coordinator is not None
    second = second_administrator(instance)
    assert instance.production.try_hold()
    attempt = asyncio.create_task(instance.coordinator.begin(OPERATOR, session_id=1))
    await until(lambda: instance.slept)

    await asyncio.wait_for(instance.coordinator.cancel(second), PROMPT_SECONDS)

    with pytest.raises(RestoreBusy, match="cancelled"):
        await attempt
    assert instance.gate.phase == "open" and instance.production.held
    assert instance.coordinator.working_directory is None


async def test_jobs_that_start_during_the_wait_for_production_are_refused_once_engaged(
    instance: Instance,
) -> None:
    assert instance.coordinator is not None
    assert instance.production.try_hold()
    attempt = asyncio.create_task(instance.coordinator.begin(OPERATOR, session_id=1))
    await until(lambda: instance.slept)

    instance.images.count = 2
    instance.production.free()

    with pytest.raises(RestoreBusy) as raised:
        await attempt
    assert str(raised.value) == "2 jobs are running; wait for them to finish."
    assert instance.gate.phase == "open" and not instance.production.held
    assert instance.suspension.entered == instance.suspension.exited


async def test_a_new_restore_can_begin_after_one_was_cancelled(instance: Instance) -> None:
    assert instance.coordinator is not None
    await instance.coordinator.begin(OPERATOR, session_id=1)
    await instance.coordinator.cancel(OPERATOR)

    restore_id = await instance.coordinator.begin(OPERATOR, session_id=1)

    assert instance.gate.restore_id == restore_id
    assert instance.suspension.entered == 2 and instance.suspension.exited == 1


async def test_an_engaged_restore_idle_for_the_whole_timeout_is_released_as_timed_out(
    instance: Instance,
) -> None:
    assert instance.coordinator is not None
    restore_id = await instance.coordinator.begin(OPERATOR, session_id=1)
    await until(lambda: instance.slept)
    first_sleep = instance.slept[-1]

    instance.clock.jump(IDLE_SECONDS + 1)
    instance.waiting[-1].set_result(None)
    await until(lambda: instance.gate.phase == "open")

    progress_owner = instance.coordinator.progress(restore_id, session_id=1)
    progress_other = instance.coordinator.progress(restore_id, session_id=5)
    assert first_sleep == IDLE_SECONDS
    assert not instance.production.held
    assert instance.suspension.exited == 1
    assert instance.reason_of(instance.actions("instance_restore_failed")[-1]) == "timed_out"
    assert not (instance.data_dir / "restore" / restore_id).exists()
    assert progress_owner is not None and progress_owner.reason == "timed_out"
    assert progress_other is None


async def test_activity_moves_the_idle_deadline(instance: Instance, archive: Path) -> None:
    assert instance.coordinator is not None
    await instance.coordinator.begin(OPERATOR, session_id=1)
    await until(lambda: instance.slept)
    instance.clock.jump(IDLE_SECONDS - 60)
    await instance.coordinator.receive_archive(OPERATOR, 1, feed(archive))

    instance.clock.jump(120)
    instance.waiting[-1].set_result(None)
    await until(lambda: len(instance.slept) >= 2)

    assert instance.gate.phase == "engaged"
    assert instance.slept[-1] == IDLE_SECONDS - 120


async def test_another_administrator_may_cancel_and_is_the_actor_of_the_row(
    instance: Instance,
) -> None:
    assert instance.coordinator is not None
    second = second_administrator(instance)
    await instance.coordinator.begin(OPERATOR, session_id=1)

    with pytest.raises(PermissionDenied):
        await instance.coordinator.cancel(STANDARD)
    await instance.coordinator.cancel(second)

    row = instance.actions("instance_restore_failed")[-1]
    assert instance.gate.phase == "open" and not instance.production.held
    assert (row["actor_user_id"], instance.reason_of(row)) == (second.user_id, "cancelled")
    assert (instance.suspension.entered, instance.suspension.exited) == (1, 1)


async def test_cancelling_records_the_stage_reached_and_the_archive_and_removes_the_directory(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    restore_id = await begin_and_upload(instance, archive)

    await instance.coordinator.cancel(OPERATOR)

    detail = json.loads(instance.actions("instance_restore_failed")[-1]["detail"])
    assert detail["stage"] == "key" and detail["restore_id"] == restore_id
    assert {"archive_created_at", "archive_schema_version"} <= set(detail)
    assert not (instance.data_dir / "restore" / restore_id).exists()
    assert instance.coordinator.status(OPERATOR, 1) is None


async def test_cancelling_with_nothing_engaged_does_nothing_and_a_review_finds_no_restore(
    instance: Instance,
) -> None:
    assert instance.coordinator is not None

    await instance.coordinator.cancel(OPERATOR)

    assert instance.actions("instance_restore_failed") == []
    with pytest.raises(RestoreNotFound):
        await instance.coordinator.review(OPERATOR, 1)


async def test_an_upload_that_stalls_is_interrupted_by_a_cancel_and_leaves_no_directory(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    second = second_administrator(instance)
    restore_id = await begin_and_upload(instance, archive)
    arguments = await confirm_arguments(instance)
    stall = asyncio.Event()

    async def stalling() -> AsyncIterator[bytes]:
        yield b"\x1f\x8b" + b"x" * STALLED_UPLOAD_BYTES
        await stall.wait()
        yield b"never"

    upload = asyncio.create_task(instance.coordinator.receive_archive(OPERATOR, 1, stalling()))
    await asyncio.sleep(0.2)
    canceller = asyncio.create_task(instance.coordinator.cancel(second))
    await asyncio.sleep(0)

    with pytest.raises(RestoreNotFound):
        await instance.coordinator.confirm(OPERATOR, 1, **arguments)
    await asyncio.wait_for(canceller, PROMPT_SECONDS)
    with pytest.raises(RestoreNotFound):
        await asyncio.wait_for(upload, PROMPT_SECONDS)
    stall.set()

    assert instance.gate.phase == "open" and not instance.production.held
    assert not (instance.data_dir / "restore" / restore_id).exists()
    assert [instance.reason_of(r) for r in instance.actions("instance_restore_failed")] == [
        "cancelled"
    ]


async def test_a_second_good_upload_replaces_the_first_and_leaves_no_partial_file(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    restore_id = await begin_and_upload(instance, archive)

    await instance.coordinator.receive_archive(OPERATOR, 1, feed(archive))

    directory = instance.data_dir / "restore" / restore_id
    review = await instance.coordinator.review(OPERATOR, 1)
    assert review.manifest is not None
    assert sha(directory / "upload.tar.gz") == sha(archive)
    assert list(directory.glob("*.part")) == []


async def test_a_file_that_is_not_a_backup_is_refused_on_the_archive_field_and_kept_nowhere(
    instance: Instance,
) -> None:
    assert instance.coordinator is not None
    restore_id = await instance.coordinator.begin(OPERATOR, session_id=1)

    async def junk() -> AsyncIterator[bytes]:
        yield JUNK

    with pytest.raises(ValidationFailed) as raised:
        await instance.coordinator.receive_archive(OPERATOR, 1, junk())

    directory = instance.data_dir / "restore" / restore_id
    assert refusal_fields(raised.value) == ("archive",)
    assert sorted(path.name for path in directory.iterdir()) == []


async def test_an_upload_is_refused_when_the_volume_has_too_little_free_space(
    instance: Instance, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert instance.coordinator is not None
    await instance.coordinator.begin(OPERATOR, session_id=1)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=1))

    with pytest.raises(ValidationFailed) as raised:
        await instance.coordinator.receive_archive(OPERATOR, 1, feed(archive))

    assert "free space" in raised.value.fields["archive"]


@pytest.mark.parametrize(
    "caller", [OTHER_SESSION, STANDARD], ids=["another-session", "standard-user"]
)
async def test_only_the_session_that_began_a_restore_may_use_it(
    instance: Instance, archive: Path, caller: Principal
) -> None:
    assert instance.coordinator is not None
    await begin_and_upload(instance, archive)

    with pytest.raises(PermissionDenied):
        await instance.coordinator.review(caller, caller.session_id or 0)


async def test_the_status_tells_the_owner_the_stage_and_other_sessions_that_it_is_not_theirs(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    await begin_and_upload(instance, archive)

    mine = instance.coordinator.status(OPERATOR, 1)
    theirs = instance.coordinator.status(OTHER_SESSION, 77)

    assert mine is not None and (mine.stage, mine.owned_by_caller) == ("awaiting_key", True)
    assert theirs is not None and theirs.owned_by_caller is False


async def test_the_review_shows_the_manifest_the_digest_the_live_counts_and_every_path(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    restore_id = await begin_and_upload(instance, archive)
    environments = instance.live_rows("SELECT count(*) FROM environment")[0][0]

    review = await instance.coordinator.review(OPERATOR, 1)

    directory = instance.data_dir / "restore" / restore_id
    assert review.manifest is not None and review.refusals == ()
    assert review.archive_sha256 == sha(archive) and review.operator_matched
    assert review.counts.environments == environments
    assert (review.swap.previous, review.swap.staged) == (
        directory / "previous",
        directory / "staging",
    )
    assert instance.data_dir / "dockmin.db" in review.swap.live
    assert instance.data_dir / "stacks" in review.swap.live


async def test_a_newer_schema_is_a_refusal_naming_both_versions_and_confirm_stops_before_the_key(
    instance: Instance, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert instance.coordinator is not None
    await begin_and_upload(instance, archive)
    arguments = await confirm_arguments(instance)
    monkeypatch.setattr(restore_module, "_supported_schema", lambda: 3)

    review = await instance.coordinator.review(OPERATOR, 1)
    with pytest.raises(ValidationFailed) as raised:
        await instance.coordinator.confirm(OPERATOR, 1, **arguments)

    assert len(review.refusals) == 1 and "up to 3" in review.refusals[0]
    assert "schema version" in review.refusals[0]
    assert "up to 3" in raised.value.fields["archive"]
    assert instance.actions("instance_restore_key_rejected") == []


async def test_an_operator_the_archive_does_not_list_is_refused_up_front(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    stranger = Principal(1, "someone-else", "administrator", 1)
    await instance.coordinator.begin(stranger, session_id=1)
    await instance.coordinator.receive_archive(stranger, 1, feed(archive))

    review = await instance.coordinator.review(stranger, 1)
    with pytest.raises(ValidationFailed) as raised:
        await instance.coordinator.confirm(
            stranger,
            1,
            key_text=key_for_archive(),
            archive_sha256=review.archive_sha256 or "",
            replace_confirmed=True,
        )

    assert not review.operator_matched and len(review.refusals) == 1
    assert "administrator" in raised.value.fields["archive"]


KEY_ATTEMPTS = [
    pytest.param("", "key_missing", id="missing"),
    pytest.param("not-a-key", "key_invalid", id="not-base64"),
    pytest.param("d3JvbmcgbGVuZ3Ro", "key_invalid", id="wrong-length"),
]


@pytest.mark.parametrize(("typed", "reason"), KEY_ATTEMPTS)
async def test_a_key_that_is_unusable_is_a_form_error_on_the_key_field_and_a_recorded_rejection(
    instance: Instance, archive: Path, typed: str, reason: str
) -> None:
    assert instance.coordinator is not None
    restore_id = await begin_and_upload(instance, archive)
    arguments = await confirm_arguments(instance) | {"key_text": SecretStr(typed)}

    with pytest.raises(ValidationFailed) as raised:
        await instance.coordinator.confirm(OPERATOR, 1, **arguments)

    rejected = instance.actions("instance_restore_key_rejected")
    assert refusal_fields(raised.value) == ("key",)
    assert [instance.reason_of(r) for r in rejected] == [reason]
    assert not (instance.data_dir / "restore" / restore_id / "staging").exists()
    assert instance.gate.phase == "engaged"


async def test_a_well_formed_wrong_key_names_the_archives_key_version_and_fingerprint(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    await begin_and_upload(instance, archive)
    review = await instance.coordinator.review(OPERATOR, 1)
    arguments = await confirm_arguments(instance) | {
        "key_text": SecretStr(base64.b64encode(THIRD_KEY).decode())
    }

    with pytest.raises(ValidationFailed) as raised:
        await instance.coordinator.confirm(OPERATOR, 1, **arguments)

    assert review.manifest is not None
    assert "key version" in raised.value.fields["key"]
    assert review.manifest.key_fingerprint in raised.value.fields["key"]
    assert instance.reason_of(instance.actions("instance_restore_key_rejected")[-1]) == (
        "fingerprint_mismatch"
    )


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"archive_sha256": "0" * 64}, "archive"),
        ({"replace_confirmed": False}, "replace_confirmed"),
    ],
    ids=["archive-changed", "not-confirmed"],
)
async def test_a_changed_archive_or_a_missing_confirmation_is_refused_before_the_key_is_read(
    instance: Instance, archive: Path, changes: dict[str, object], field: str
) -> None:
    assert instance.coordinator is not None
    await begin_and_upload(instance, archive)
    arguments = await confirm_arguments(instance) | changes

    with pytest.raises(ValidationFailed) as raised:
        await instance.coordinator.confirm(OPERATOR, 1, **arguments)

    assert refusal_fields(raised.value) == (field,)
    assert instance.actions("instance_restore_key_rejected") == []
    assert instance.coordinator.status(OPERATOR, 1) is not None


async def test_shutting_down_releases_an_engaged_restore_as_shutdown(instance: Instance) -> None:
    assert instance.coordinator is not None
    await instance.coordinator.begin(OPERATOR, session_id=1)

    await instance.coordinator.aclose()

    assert instance.gate.phase == "open" and not instance.production.held
    assert instance.reason_of(instance.actions("instance_restore_failed")[-1]) == "shutdown"
    assert instance.suspension.entered == instance.suspension.exited == 1


class BlockedVerify:
    """Replaces verify_members with a loop that ends only when asked to stop."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    def __call__(self, *arguments: object, stop: Callable[[], bool] | None = None) -> None:
        while not self.release.is_set():
            if stop is not None and stop():
                raise StopRequested
            time.sleep(0.01)


@pytest.fixture
def blocked_verify(monkeypatch: pytest.MonkeyPatch) -> BlockedVerify:
    blocked = BlockedVerify()
    monkeypatch.setattr(pipeline_module, "verify_members", blocked)
    return blocked


async def test_a_restore_that_is_running_cannot_be_cancelled(
    instance: Instance, archive: Path, blocked_verify: BlockedVerify
) -> None:
    assert instance.coordinator is not None
    await begin_and_upload(instance, archive)
    await instance.coordinator.confirm(OPERATOR, 1, **await confirm_arguments(instance))

    with pytest.raises(RestoreBusy):
        await instance.coordinator.cancel(OPERATOR)
    blocked_verify.release.set()


async def test_shutting_down_during_verification_stops_it_promptly_and_removes_the_work(
    instance: Instance, archive: Path, blocked_verify: BlockedVerify
) -> None:
    assert instance.coordinator is not None
    restore_id = await begin_and_upload(instance, archive)
    await instance.coordinator.confirm(OPERATOR, 1, **await confirm_arguments(instance))
    await asyncio.sleep(0.2)

    await asyncio.wait_for(instance.coordinator.aclose(), PROMPT_SECONDS * 3)

    progress = instance.coordinator.progress(restore_id, session_id=1)
    assert progress is not None and progress.reason == "shutdown"
    assert instance.gate.phase == "open" and not instance.production.held
    assert not (instance.data_dir / "restore" / restore_id).exists()


def test_the_startup_sweep_removes_work_and_empty_restore_directories_and_keeps_moved_data(
    tmp_path: Path,
) -> None:
    (tmp_path / "backup-work" / "abc").mkdir(parents=True)
    (tmp_path / "restore" / ("a" * 32) / "staging").mkdir(parents=True)
    (tmp_path / "restore" / ("b" * 32) / "previous").mkdir(parents=True)
    (tmp_path / "restore" / ("c" * 32) / "rejected").mkdir(parents=True)
    (tmp_path / "restore" / ("d" * 32)).mkdir(parents=True)
    (tmp_path / "restore" / "file.txt").write_text("x", encoding="utf-8")

    sweep_restore_leftovers(tmp_path, keep=tmp_path / "restore" / ("d" * 32))
    kept = sorted(entry.name for entry in (tmp_path / "restore").iterdir())
    sweep_restore_leftovers(tmp_path)

    assert not (tmp_path / "backup-work").exists()
    assert kept == ["b" * 32, "c" * 32, "d" * 32, "file.txt"]
    assert sorted(entry.name for entry in (tmp_path / "restore").iterdir()) == [
        "b" * 32,
        "c" * 32,
        "file.txt",
    ]
