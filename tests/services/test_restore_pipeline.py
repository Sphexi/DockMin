"""The shared restore pipeline as the web restore drives it: source, failure source, the pause."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import app.backup.pipeline as pipeline_module
import app.services.restore as restore_module
import app.services.restore_command as command_module
from app.auth.errors import PermissionDenied, ValidationFailed
from app.db.repositories.setting import RESTORE_AUTOMATION_PAUSED_KEY
from tests.restore_support import (
    OPERATOR,
    STANDARD,
    Instance,
    begin_and_upload,
    confirm_arguments,
    feed,
    make_instance,
    restore_fully,
    settle,
    source_archive,
)

TINY_CAP = 1024
SETTING_SQL = "SELECT value_type, value FROM setting WHERE key = ?"
FAIL_RESUME_ROW = (
    "CREATE TRIGGER refuse_resume_row BEFORE INSERT ON activity_event "
    "WHEN NEW.action = 'instance_automation_resumed' "
    "BEGIN SELECT RAISE(ABORT, 'refused'); END"
)


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    built, world = asyncio.run(source_archive(tmp_path_factory.mktemp("pipeline-source")))
    asyncio.run(world.database.aclose())
    return built


@pytest.fixture
async def instance(tmp_path: Path) -> AsyncIterator[Instance]:
    built = await make_instance(tmp_path / "live")
    yield built
    await built.close()


def restored_detail(instance: Instance) -> dict[str, Any]:
    [row] = instance.actions("instance_restored")
    detail: dict[str, Any] = json.loads(row["detail"])
    return detail


def test_the_web_restore_and_the_command_line_call_the_one_pipeline_function() -> None:
    assert vars(restore_module)["run_restore_steps"] is pipeline_module.run_restore_steps
    assert vars(command_module)["run_restore_steps"] is pipeline_module.run_restore_steps


async def test_a_web_restore_runs_the_pipeline_once_with_the_operator_and_the_upload_source(
    instance: Instance, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    real = pipeline_module.run_restore_steps

    async def spy(**options: Any) -> Any:
        calls.append(options)
        return await real(**options)

    monkeypatch.setattr(restore_module, "run_restore_steps", spy)

    progress = await restore_fully(instance, archive)

    [call] = calls
    assert progress.outcome == "restored"
    assert call["operator_username"] == OPERATOR.username
    assert call["record_context"].source == "upload"
    assert call["running_key"] == instance.runtime().running_key


async def test_an_uploaded_archive_is_recorded_as_the_upload_source_without_a_bucket_location(
    instance: Instance, archive: Path
) -> None:
    await restore_fully(instance, archive)

    detail = restored_detail(instance)
    assert detail["source"] == "upload"
    assert "object_storage_target_id" not in detail and "object_path" not in detail


async def test_a_restore_that_fails_records_its_source_on_the_failure_row(
    instance: Instance, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*arguments: object, **options: object) -> None:
        raise OSError("stage failed")

    monkeypatch.setattr(pipeline_module, "stage_members", broken)

    progress = await restore_fully(instance, archive)

    [row] = instance.actions("instance_restore_failed")
    assert progress.outcome == "failed"
    assert json.loads(row["detail"])["source"] == "upload"


async def test_finalising_writes_the_pause_setting_with_the_restore_id_beside_the_restored_row(
    instance: Instance, archive: Path
) -> None:
    restore_id = await begin_and_upload(instance, archive)
    assert instance.coordinator is not None
    await instance.coordinator.confirm(OPERATOR, 1, **await confirm_arguments(instance))
    await settle(instance, restore_id)

    [setting] = instance.live_rows(SETTING_SQL, (RESTORE_AUTOMATION_PAUSED_KEY,))
    assert (setting["value_type"], setting["value"]) == ("string", restore_id)
    assert len(instance.actions("instance_restored")) == 1
    assert instance.coordinator.automation_paused


async def test_a_failed_restore_leaves_no_pause_setting(
    instance: Instance, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*arguments: object, **options: object) -> None:
        raise OSError("stage failed")

    monkeypatch.setattr(pipeline_module, "stage_members", broken)

    await restore_fully(instance, archive)

    assert instance.live_rows(SETTING_SQL, (RESTORE_AUTOMATION_PAUSED_KEY,)) == []


async def test_resuming_deletes_the_pause_setting_and_writes_one_resumed_row(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    await restore_fully(instance, archive)

    await instance.coordinator.resume_automation(OPERATOR)
    await instance.coordinator.resume_automation(OPERATOR)

    assert instance.live_rows(SETTING_SQL, (RESTORE_AUTOMATION_PAUSED_KEY,)) == []
    assert len(instance.actions("instance_automation_resumed")) == 1
    assert not instance.coordinator.automation_paused


async def test_a_standard_user_cannot_resume_and_the_setting_stays(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    await restore_fully(instance, archive)

    with pytest.raises(PermissionDenied):
        await instance.coordinator.resume_automation(STANDARD)

    assert len(instance.live_rows(SETTING_SQL, (RESTORE_AUTOMATION_PAUSED_KEY,))) == 1
    assert instance.coordinator.automation_paused


async def test_a_resume_whose_row_cannot_be_written_keeps_the_setting_and_the_hold(
    instance: Instance, archive: Path
) -> None:
    assert instance.coordinator is not None
    await restore_fully(instance, archive)
    instance.world.run(FAIL_RESUME_ROW)

    with pytest.raises(Exception, match="refused"):
        await instance.coordinator.resume_automation(OPERATOR)

    assert len(instance.live_rows(SETTING_SQL, (RESTORE_AUTOMATION_PAUSED_KEY,))) == 1
    assert instance.actions("instance_automation_resumed") == []
    assert instance.coordinator.automation_paused


async def test_an_upload_above_the_archive_cap_is_refused_and_kept_nowhere(
    instance: Instance, archive: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert instance.coordinator is not None
    restore_id = await instance.coordinator.begin(OPERATOR, session_id=1)
    monkeypatch.setattr(restore_module, "MAX_ARCHIVE_BYTES", TINY_CAP)

    with pytest.raises(ValidationFailed) as raised:
        await instance.coordinator.receive_archive(OPERATOR, 1, feed(archive))

    directory = instance.data_dir / "restore" / restore_id
    assert "size limit" in raised.value.fields["archive"]
    assert sorted(entry.name for entry in directory.iterdir()) == []
