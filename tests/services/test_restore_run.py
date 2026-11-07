"""A restore from the key to the restart: what it replaces and every way it can end."""

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import app.backup.pipeline as pipeline_module
from app.auth.errors import PermissionDenied
from app.backup import paths
from app.backup.paths import SwapPlan
from app.crypto.registry import SECRET_FIELDS
from app.db.repositories.crypto_state import CryptoStateRepository
from app.services.restore import RestoreBusy, RestoreProgress
from tests.crypto.helpers import CredentialRepository
from tests.restore_support import (
    LIVE_ENVIRONMENT,
    LIVE_STACK,
    OPERATOR,
    STANDARD,
    Instance,
    begin_and_upload,
    confirm_arguments,
    feed,
    make_instance,
    settle,
    sha,
    source_archive,
)

ACTIVITY_GAP = 1_000_000_000
RESTORED_STACK_FILE = "services:\n  web: {image: x}\n"
NEW_STACK_DIRECTORY = "e" * 32
CHUNK = 65536
TRUNCATED_BY = 2000


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    built, world = asyncio.run(source_archive(tmp_path_factory.mktemp("run-source")))
    asyncio.run(world.database.aclose())
    return built


@dataclass
class Restored:
    """What a complete restore left behind, read once after it finished."""

    progress: RestoreProgress | None = None
    run_cancel_refused: bool = False
    published_before_first_move: SwapPlan | None = None
    logged_before_first_move: list[logging.LogRecord] = field(default_factory=list)
    phase_before_first_move: str = ""
    live_database_before_first_move: bool = False
    gate_phase: str = ""
    production_held: bool = True
    previous_environments: list[str] = field(default_factory=list)
    directory_entries: list[str] = field(default_factory=list)
    previous_stack_present: bool = False
    environments: list[str] = field(default_factory=list)
    stacks: set[tuple[str, str]] = field(default_factory=set)
    stack_directories: list[str] = field(default_factory=list)
    new_stack_text: str = ""
    expected_stack_directories: list[str] = field(default_factory=list)
    versions: set[int] = field(default_factory=set)
    revealed: dict[str, str | None] = field(default_factory=dict)
    expected_plaintexts: dict[str, str] = field(default_factory=dict)
    restored_rows: list[Any] = field(default_factory=list)
    key_rows: list[Any] = field(default_factory=list)
    started_rows: int = -1
    lowest_restore_row_id: int = 0
    sessions_left: int = -1
    paused: bool = False
    standard_resume_denied: bool = False
    resumed_rows: int = -1
    paused_after_resume: bool = True
    later_progress: RestoreProgress | None = None
    unknown_progress: RestoreProgress | None = None
    archive_sha: str = ""


def read_rows(database: Path, sql: str) -> list[Any]:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def truncated(source: Path, target: Path) -> Path:
    target.write_bytes(source.read_bytes()[:-TRUNCATED_BY])
    return target


def all_exist(names: tuple[str, ...]) -> bool:
    return all(Path(name).exists() for name in names)


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


async def full_restore(base: Path, archive: Path) -> Restored:
    facts = Restored(archive_sha=sha(archive))
    instance = await make_instance(base / "live")
    coordinator = instance.coordinator
    assert coordinator is not None
    data_dir = instance.data_dir
    capture, logger = Capture(), logging.getLogger("app.services.restore")
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    real_apply = paths.apply_swap
    restore_id = await begin_and_upload(instance, archive)

    def watching(plan: SwapPlan) -> Any:
        facts.logged_before_first_move = [
            r for r in capture.records if r.getMessage() == "restore_swap_paths"
        ]
        facts.published_before_first_move = coordinator.progress(restore_id, session_id=None).paths  # type: ignore[union-attr]
        facts.phase_before_first_move = instance.gate.phase
        facts.live_database_before_first_move = (data_dir / "dockmin.db").exists()
        return real_apply(plan)

    paths.apply_swap = watching
    try:
        await coordinator.confirm(OPERATOR, 1, **await confirm_arguments(instance))
        try:
            await coordinator.cancel(OPERATOR)
        except RestoreBusy:
            facts.run_cancel_refused = True
        facts.progress = await settle(instance, restore_id)
    finally:
        paths.apply_swap = real_apply
        logger.removeHandler(capture)
    facts.gate_phase = instance.gate.phase
    facts.production_held = instance.production.held
    directory = data_dir / "restore" / restore_id
    facts.previous_environments = [
        r[0]
        for r in read_rows(directory / "previous" / "dockmin.db", "SELECT name FROM environment")
    ]
    facts.directory_entries = sorted(entry.name for entry in directory.iterdir())
    facts.previous_stack_present = (
        directory / "previous" / "stacks" / LIVE_STACK / "compose.yaml"
    ).exists()
    facts.environments = [r[0] for r in instance.live_rows("SELECT name FROM environment")]
    facts.stacks = {
        (r["name"], r["kind"]) for r in instance.live_rows("SELECT name, kind FROM stack")
    }
    facts.expected_stack_directories = sorted(
        r[0] for r in instance.live_rows("SELECT project_directory FROM stack")
    )
    facts.stack_directories = sorted(entry.name for entry in (data_dir / "stacks").iterdir())
    facts.new_stack_text = (data_dir / "stacks" / NEW_STACK_DIRECTORY / "compose.yaml").read_text(
        encoding="utf-8"
    )
    async with instance.database.session() as session:
        facts.versions = set(await CryptoStateRepository(session).count_by_version())
        repository = CredentialRepository(session, instance.world.vault)
        for field_ in SECRET_FIELDS:
            row = await session.fetch_one(f"SELECT id FROM {field_.table} ORDER BY id LIMIT 1")
            assert row is not None
            key = f"{field_.table}.{field_.column}"
            facts.revealed[key] = await repository.reveal(field_, row["id"])
            facts.expected_plaintexts[key] = f"plain-{field_.table}-{field_.column}"
    facts.restored_rows = instance.actions("instance_restored")
    facts.key_rows = instance.actions("encryption_key_changed")
    facts.started_rows = len(instance.actions("instance_restore_started"))
    facts.lowest_restore_row_id = instance.live_rows(
        "SELECT min(id) FROM activity_event WHERE action IN "
        "('instance_restored', 'encryption_key_changed')"
    )[0][0]
    facts.sessions_left = instance.live_rows("SELECT count(*) FROM session")[0][0]
    facts.paused = coordinator.automation_paused
    try:
        await coordinator.resume_automation(STANDARD)
    except PermissionDenied:
        facts.standard_resume_denied = True
    await coordinator.resume_automation(OPERATOR)
    await coordinator.resume_automation(OPERATOR)
    facts.resumed_rows = len(instance.actions("instance_automation_resumed"))
    facts.paused_after_resume = coordinator.automation_paused
    facts.later_progress = coordinator.progress(restore_id, session_id=None)
    facts.unknown_progress = coordinator.progress("0" * 32, session_id=1)
    await instance.close()
    return facts


@pytest.fixture(scope="module")
def restored(archive: Path, tmp_path_factory: pytest.TempPathFactory) -> Restored:
    return asyncio.run(full_restore(tmp_path_factory.mktemp("restore-run"), archive))


@pytest.fixture
async def instance(tmp_path: Path) -> AsyncIterator[Instance]:
    built = await make_instance(tmp_path / "live")
    yield built
    await built.close()


def test_a_complete_restore_finishes_as_restored_with_every_step_done(restored: Restored) -> None:
    assert restored.progress is not None
    assert (restored.progress.outcome, restored.progress.reason) == ("restored", None)
    assert all(state == "done" for _, state in restored.progress.steps)


def test_a_running_restore_cannot_be_cancelled(restored: Restored) -> None:
    assert restored.run_cancel_refused


def test_both_paths_are_logged_and_published_before_the_first_move(restored: Restored) -> None:
    plan = restored.published_before_first_move

    assert len(restored.logged_before_first_move) == 1
    assert plan is not None and plan.previous.name == "previous" and plan.staged.name == "staging"
    assert restored.phase_before_first_move == "swapping"
    assert restored.live_database_before_first_move
    logged = restored.logged_before_first_move[0]
    assert logged.previous_path == str(plan.previous)  # type: ignore[attr-defined]
    assert logged.staged_path == str(plan.staged)  # type: ignore[attr-defined]


def test_the_gate_reopens_and_the_production_lock_is_free(restored: Restored) -> None:
    assert restored.gate_phase == "open"
    assert not restored.production_held


def test_the_data_it_replaced_is_kept_and_everything_else_in_the_restore_directory_is_gone(
    restored: Restored,
) -> None:
    assert LIVE_ENVIRONMENT in restored.previous_environments
    assert restored.previous_stack_present
    assert restored.directory_entries == ["previous"]


def test_the_restored_data_replaced_the_instances_own(restored: Restored) -> None:
    assert LIVE_ENVIRONMENT not in restored.environments
    assert len(restored.environments) >= 2


def test_the_compose_and_the_swarm_stack_are_listed_and_their_working_copies_come_from_the_archive(
    restored: Restored,
) -> None:
    assert {("stack-one", "compose"), ("stack-two", "swarm")} <= restored.stacks
    assert restored.stack_directories == restored.expected_stack_directories
    assert restored.new_stack_text == RESTORED_STACK_FILE


def test_every_secret_was_resealed_to_the_running_key_and_decrypts_to_its_original(
    restored: Restored,
) -> None:
    assert restored.versions == {3}
    assert restored.revealed == restored.expected_plaintexts


def test_the_restored_log_holds_the_restore_and_the_key_change_naming_the_operator(
    restored: Restored,
) -> None:
    detail = json.loads(restored.restored_rows[0]["detail"])

    assert len(restored.restored_rows) == len(restored.key_rows) == 1
    assert restored.restored_rows[0]["actor_user_id"] == OPERATOR.user_id
    assert detail["operator_username"] == OPERATOR.username
    assert detail["key_changed"] is True
    assert detail["archive_sha256"] == restored.archive_sha
    assert detail["previous_path"].endswith("/previous")
    assert json.loads(restored.key_rows[0]["detail"])["key_version_after"] == 3


def test_the_abandoned_logs_start_row_is_not_in_the_restored_log_and_ids_jump_a_gap(
    restored: Restored,
) -> None:
    assert restored.started_rows == 0
    assert restored.lowest_restore_row_id > ACTIVITY_GAP


def test_every_session_was_cleared_by_the_restore(restored: Restored) -> None:
    assert restored.sessions_left == 0


def test_automation_stays_paused_until_an_administrator_resumes_it_once(restored: Restored) -> None:
    assert restored.paused
    assert restored.standard_resume_denied
    assert restored.resumed_rows == 1
    assert not restored.paused_after_resume


def test_the_progress_is_served_by_id_alone_after_the_swap_and_an_unknown_id_is_unknown(
    restored: Restored,
) -> None:
    assert restored.later_progress is not None and restored.later_progress.outcome == "restored"
    assert restored.unknown_progress is None


async def ready_to_confirm(instance: Instance, archive: Path) -> tuple[str, dict[str, Any]]:
    restore_id = await begin_and_upload(instance, archive)
    return restore_id, await confirm_arguments(instance)


async def run_to_end(instance: Instance, archive: Path) -> tuple[str, RestoreProgress]:
    assert instance.coordinator is not None
    restore_id, arguments = await ready_to_confirm(instance, archive)
    await instance.coordinator.confirm(OPERATOR, 1, **arguments)
    return restore_id, await settle(instance, restore_id)


def environment_names(instance: Instance) -> list[str]:
    return [r[0] for r in instance.live_rows("SELECT name FROM environment")]


async def test_a_truncated_archive_fails_at_verification_and_changes_nothing(
    instance: Instance, archive: Path, tmp_path: Path
) -> None:
    assert instance.coordinator is not None
    cut = truncated(archive, tmp_path / "truncated.tar.gz")

    restore_id, progress = await run_to_end(instance, cut)

    detail = json.loads(instance.actions("instance_restore_failed")[-1]["detail"])
    assert (progress.outcome, progress.reason) == ("failed", "digest_mismatch")
    assert dict(progress.steps)["verify"] == "failed" and dict(progress.steps)["stage"] == "pending"
    assert (detail["reason"], detail["stage"]) == ("digest_mismatch", "verify")
    assert instance.gate.phase == "open" and not instance.production.held
    assert instance.suspension.entered == instance.suspension.exited
    assert LIVE_ENVIRONMENT in environment_names(instance)
    assert not (instance.data_dir / "restore" / restore_id).exists()


async def test_an_unexpected_error_in_a_step_is_reported_by_step_with_no_detail_of_the_error(
    instance: Instance, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*arguments: object, **options: object) -> None:
        raise OSError("disk exploded at /secret/path")

    monkeypatch.setattr(pipeline_module, "stage_members", broken)

    _, progress = await run_to_end(instance, archive)

    assert (progress.outcome, progress.reason) == ("failed", "archive_invalid")
    assert progress.message is not None and "stage step" in progress.message
    assert "secret/path" not in progress.message
    assert instance.gate.phase == "open" and not instance.production.held
    assert environment_names(instance).count(LIVE_ENVIRONMENT) == 1


async def test_a_swap_that_fails_after_its_first_move_is_reverted_and_the_instance_runs_again(
    instance: Instance, archive: Path
) -> None:
    restore_id = await begin_and_upload(instance, archive)
    (instance.data_dir / "restore" / restore_id / "previous" / "stacks").mkdir(parents=True)
    stacks_before = sorted(entry.name for entry in (instance.data_dir / "stacks").iterdir())
    assert instance.coordinator is not None
    await instance.coordinator.confirm(OPERATOR, 1, **await confirm_arguments(instance))

    progress = await settle(instance, restore_id)

    row = instance.actions("instance_restore_failed")[-1]
    assert (progress.outcome, progress.reason) == ("failed", "swap_failed")
    assert sorted(entry.name for entry in (instance.data_dir / "stacks").iterdir()) == stacks_before
    assert LIVE_ENVIRONMENT in environment_names(instance)
    assert instance.gate.phase == "open" and not instance.production.held
    assert instance.reason_of(row) == "swap_failed"


async def test_a_runtime_that_will_not_stop_aborts_the_swap_before_anything_moves(
    instance: Instance, archive: Path
) -> None:
    instance.failing_stops = 1

    _, progress = await run_to_end(instance, archive)

    assert (progress.outcome, progress.reason) == ("failed", "swap_failed")
    assert progress.message is not None and "did not stop cleanly" in progress.message
    assert LIVE_ENVIRONMENT in environment_names(instance)
    assert instance.gate.phase == "open"


async def test_a_restored_instance_that_will_not_start_is_rejected_and_the_previous_data_returns(
    instance: Instance, archive: Path
) -> None:
    instance.failing_starts = 1

    restore_id, progress = await run_to_end(instance, archive)

    rejected = instance.data_dir / "restore" / restore_id / "rejected"
    assert (progress.outcome, progress.reason) == ("failed", "restart_failed")
    assert instance.starts == 2
    assert (rejected / "dockmin.db").exists()
    assert (rejected / "stacks" / ("e" * 32)).exists()
    assert LIVE_ENVIRONMENT in environment_names(instance)
    assert (instance.data_dir / "stacks" / LIVE_STACK).exists()
    assert instance.gate.phase == "open" and not instance.production.held
    assert instance.reason_of(instance.actions("instance_restore_failed")[-1]) == "restart_failed"
    assert progress.message is not None and str(rejected) in progress.message


async def test_two_failed_starts_leave_the_gate_failed_with_both_paths_and_the_lock_held(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    instance.failing_starts = 99

    restore_id, progress = await run_to_end(instance, archive)

    directory = instance.data_dir / "restore" / restore_id
    assert (instance.gate.phase, progress.reason) == ("failed", "restart_failed")
    assert len(instance.gate.failed_paths) == 2
    assert all(str(instance.data_dir) in path for path in instance.gate.failed_paths)
    assert all_exist(instance.gate.failed_paths)
    assert {"previous", "rejected"} <= {entry.name for entry in directory.iterdir()}
    assert instance.production.held
    assert progress.message is not None and "by hand" in progress.message
    with pytest.raises(RestoreBusy):
        await instance.coordinator.begin(OPERATOR, session_id=1)


async def test_the_second_restore_after_a_failed_one_runs_to_the_end(
    instance: Instance, archive: Path
) -> None:
    instance.failing_starts = 1
    await run_to_end(instance, archive)

    _, progress = await run_to_end(instance, archive)

    assert progress.outcome == "restored"


async def test_an_archive_uploaded_in_pieces_restores_the_same_as_one_sent_whole(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    restore_id = await instance.coordinator.begin(OPERATOR, session_id=1)
    await instance.coordinator.receive_archive(OPERATOR, 1, feed(archive, CHUNK // 8))
    await instance.coordinator.confirm(OPERATOR, 1, **await confirm_arguments(instance))

    progress = await settle(instance, restore_id)

    assert progress.outcome == "restored"


def test_the_plan_published_before_the_move_names_no_database_journal_files(
    restored: Restored,
) -> None:
    assert restored.published_before_first_move is not None
    names = [path.name for path in restored.published_before_first_move.live]
    assert not any(name.endswith(("-wal", "-shm")) for name in names)
    assert {"dockmin.db", "stacks"} <= set(names)


async def test_a_swap_that_cannot_be_reverted_keeps_every_path_its_message_names(
    instance: Instance, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_apply = paths.apply_swap

    def moved_then_failed(plan: SwapPlan) -> object:
        journal = real_apply(plan)
        raise paths.SwapFailed("the swap broke after moving", journal)

    def stuck(journal: object) -> None:
        raise paths.SwapFailed("the revert broke", journal)  # type: ignore[arg-type]

    monkeypatch.setattr(paths, "apply_swap", moved_then_failed)
    monkeypatch.setattr(paths, "revert_swap", stuck)

    _, progress = await run_to_end(instance, archive)

    assert (instance.gate.phase, progress.reason) == ("failed", "swap_failed")
    assert len(instance.gate.failed_paths) == 2
    assert all_exist(instance.gate.failed_paths)
    assert instance.production.held
