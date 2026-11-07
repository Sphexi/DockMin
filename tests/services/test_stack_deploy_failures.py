"""How a stack deploy ends when Compose fails, times out or the request is cancelled."""

import asyncio
import logging
import stat
from pathlib import Path
from typing import Any

import pytest

import app.services.stack_runs as stack_runs
from app.logging import REDACTED
from app.services.assembly import start_stack_job
from app.services.object_locks import ObjectKey
from app.services.stack_deploy import StackOperationFailed
from tests.access_support import ADMIN, ENV_A, ENV_B
from tests.docker_engine.container_engine import ENGINE_TEXT
from tests.image_support import REFERENCE, binding_rows, register, rows_for
from tests.stack_support import (
    Rig,
    activity_dump,
    create_then_deploy,
    files_containing,
    form_variables,
    logged_text,
    poll_until,
    read_records,
    recorded_call,
    removal_binding,
    wait_job,
)
from tests.support import present

SENTINEL = "SECRET_SENTINEL_VALUE"
TOKEN_VALUE = "hunter2secretvalue"
SHORT_TIMEOUT = 1.0
LATE_TIMEOUT = 2.0
SETTLE_WINDOW = 0.3
STALE_VERSION = "v2.0.0"
WORKING_PROGRESS = "Container shop-web-1 Working"
DAEMON_TEXT = "Starting"


PLAIN_STACK = """
services:
  web:
    image: nginx:1
"""
ABSENT_IMAGE_STACK = f"""
services:
  app:
    image: {REFERENCE}
"""


def scripted_stack(*, up: str, record: Path, image: str = "nginx:1") -> str:
    return f"""
services:
  web:
    image: {image}
x-fake:
  up: {up}
  record: {record}
"""


def up_has_started(record: Path, work_base: Path) -> bool:
    started = any(call["cmd"] == "up" for call in read_records(record))
    return started and any(work_base.glob("*/tls"))


async def test_a_failing_deploy_surfaces_scrubbed_stderr_and_records_the_partial_progress(
    stack_rig: Rig,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    set_id = await register(rig, "only", environments=(ENV_A,))
    variables = form_variables(("API_SECRET", SENTINEL, True), ("TOKEN", TOKEN_VALUE, True))
    text = f"""
services:
  web:
    image: nginx:1
  app:
    image: {REFERENCE}
x-fake:
  up: fail
  record: {record}
"""
    await rig.services.actions.create(
        ADMIN, ENV_A, name="shop", compose_text=text, variables=variables
    )

    await rig.db.execute(
        "UPDATE environment SET compose_cli_version = ? WHERE id = ?", (STALE_VERSION, ENV_A)
    )

    with pytest.raises(StackOperationFailed) as failed:
        await rig.fresh().actions.deploy(ADMIN, ENV_A, "shop")

    error = failed.value
    [row] = await rig.rows("stack_deployed")
    stack = present(
        await rig.db.fetch_one("SELECT status, last_operation, last_outcome FROM stack")
    )
    environment = await rig.db.fetch_one(
        "SELECT compose_cli_version FROM environment WHERE id = ?", (ENV_A,)
    )
    [binding] = await binding_rows(rig.db)
    cert_path = Path(recorded_call(record, "up")["env"]["DOCKER_CERT_PATH"])
    assert error.reason == "compose_failed"
    assert error.exit_code == 3
    assert error.event_count == 1
    assert "port is already allocated" in error.stderr
    assert REDACTED in error.stderr
    assert SENTINEL not in error.stderr
    assert TOKEN_VALUE not in error.stderr
    assert str(rig.data_dir) not in error.stderr
    assert "<data>" in error.stderr
    assert row["result"] == "failure"
    assert row["detail"]["reason"] == "compose_failed"
    assert row["detail"]["exit_code"] == 3
    assert row["detail"]["events"] == 1
    assert row["detail"]["progress"] == WORKING_PROGRESS
    assert dict(stack) == {"status": "error", "last_operation": "deploy", "last_outcome": "failure"}
    assert environment is not None
    assert environment["compose_cli_version"] == STALE_VERSION
    assert (binding["image_reference"], binding["registry_credential_id"]) == (REFERENCE, set_id)
    assert len(await rows_for(rig.db, "image_bound")) == 1
    assert cert_path.is_relative_to(rig.compose.work_base)
    assert list(rig.compose.work_base.iterdir()) == []
    dump = await activity_dump(rig)
    assert SENTINEL not in dump + logged_text(caplog)
    assert TOKEN_VALUE not in dump + logged_text(caplog)
    assert "port is already allocated" not in dump
    assert DAEMON_TEXT not in dump
    assert files_containing(tmp_path, SENTINEL) == []

    job_id = start_stack_job(
        rig.app,
        ADMIN,
        ENV_A,
        "redeploy",
        "shop",
        lambda services, progress: services.actions.redeploy(
            ADMIN, ENV_A, "shop", progress=progress
        ),
    )
    view = await wait_job(rig, ADMIN, job_id)

    assert view.state == "failed"
    assert view.outcome.reason == "compose_failed"
    assert view.outcome.exit_code == 3
    assert view.outcome.events == 1
    assert view.outcome.progress == WORKING_PROGRESS
    assert "port is already allocated" in view.outcome.stderr
    assert SENTINEL not in view.outcome.stderr
    assert TOKEN_VALUE not in view.outcome.stderr


async def test_a_timeout_is_outcome_unknown_and_the_tls_material_is_gone_afterwards(
    stack_rig: Rig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    work_base = rig.compose.work_base
    await rig.services.actions.create(
        ADMIN, ENV_A, name="shop", compose_text=scripted_stack(up="hang", record=record)
    )
    rig.sim.operation_timeout = SHORT_TIMEOUT
    deploy = asyncio.ensure_future(rig.fresh().actions.deploy(ADMIN, ENV_A, "shop"))

    await poll_until(lambda: up_has_started(record, work_base))

    [tls_dir] = work_base.glob("*/tls")
    invocation = tls_dir.parent
    up = read_records(record)[-1]
    assert stat.S_IMODE(tls_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(invocation.stat().st_mode) == 0o700
    assert {f.name: stat.S_IMODE(f.stat().st_mode) for f in tls_dir.iterdir()} == {
        "ca.pem": 0o600,
        "cert.pem": 0o600,
        "key.pem": 0o600,
    }
    empty_env = invocation / "empty.env"
    assert (stat.S_IMODE(empty_env.stat().st_mode), empty_env.stat().st_size) == (0o600, 0)
    assert up["dir_files"] == ["compose.yaml"]

    with pytest.raises(StackOperationFailed) as timed_out:
        await deploy

    [row] = await rig.rows("stack_deployed")
    stack = present(await rig.db.fetch_one("SELECT status, last_outcome FROM stack"))
    assert timed_out.value.reason == "outcome_unknown"
    assert row["result"] == "failure"
    assert row["detail"]["reason"] == "outcome_unknown"
    assert row["detail"]["events"] == 1
    assert dict(stack) == {"status": "error", "last_outcome": "outcome_unknown"}
    assert not tls_dir.exists()
    assert not invocation.exists()
    assert list(work_base.iterdir()) == []
    assert len(rig.engine.locks) == 0


async def cancelled(task: "asyncio.Future[Any]") -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def stack_deployed_rows(rig: Rig, count: int) -> bool:
    return len(await rig.rows("stack_deployed")) >= count


def up_is_running(record: Path) -> bool:
    return any(call["cmd"] == "up" for call in read_records(record))


async def test_a_cancelled_deploy_is_recorded_as_interrupted_and_leaves_no_lock_or_file(
    stack_rig: Rig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    await rig.services.actions.create(
        ADMIN, ENV_B, name="shop", compose_text=scripted_stack(up="hang", record=record)
    )
    rig.sim.operation_timeout = SHORT_TIMEOUT
    deploy = asyncio.ensure_future(rig.fresh().actions.deploy(ADMIN, ENV_B, "shop"))
    await poll_until(lambda: up_is_running(record))

    await cancelled(deploy)

    [row] = await rig.rows("stack_deployed")
    stack = present(await rig.db.fetch_one("SELECT status FROM stack"))
    assert row["result"] == "failure"
    assert row["detail"]["interrupted"] is True
    assert row["detail"]["reason"] == "outcome_unknown"
    assert stack["status"] == "error"
    assert len(rig.engine.locks) == 0
    assert list(rig.compose.work_base.iterdir()) == []


async def test_a_cancelled_deploy_that_times_out_leaves_the_stack_outcome_unknown(
    stack_rig: Rig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    await rig.services.actions.create(
        ADMIN, ENV_B, name="shop", compose_text=scripted_stack(up="hang", record=record)
    )
    rig.sim.operation_timeout = SHORT_TIMEOUT
    deploy = asyncio.ensure_future(rig.fresh().actions.deploy(ADMIN, ENV_B, "shop"))
    await poll_until(lambda: up_is_running(record))

    await cancelled(deploy)

    stack = present(await rig.db.fetch_one("SELECT last_outcome FROM stack"))
    assert stack["last_outcome"] == "outcome_unknown"


async def test_a_deploy_cancelled_before_it_starts_leaves_no_row_and_no_state(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    await rig.services.actions.create(ADMIN, ENV_B, name="shop", compose_text=PLAIN_STACK)
    deploy = asyncio.ensure_future(rig.fresh().actions.deploy(ADMIN, ENV_B, "shop"))
    await asyncio.sleep(0)

    await cancelled(deploy)

    stack = present(
        await rig.db.fetch_one("SELECT status, last_operation, last_outcome FROM stack")
    )
    assert await rig.rows("stack_deployed") == []
    assert dict(stack) == {"status": "unknown", "last_operation": None, "last_outcome": None}
    assert rig.host.mutations() == []
    assert len(rig.engine.locks) == 0
    assert list(rig.compose.work_base.iterdir()) == []


async def test_a_deploy_cancelled_after_its_first_pull_runs_to_its_end_and_records_everything(
    stack_rig: Rig,
) -> None:
    rig = stack_rig
    set_id = await register(rig, "only", environments=(ENV_B,))
    await rig.services.actions.create(ADMIN, ENV_B, name="shop", compose_text=ABSENT_IMAGE_STACK)
    gate = rig.host.hold("pull")
    deploy = asyncio.ensure_future(rig.fresh().actions.deploy(ADMIN, ENV_B, "shop"))
    async with asyncio.timeout(5):
        await gate.entered.wait()
    deploy.cancel()
    gate.release.set()
    with pytest.raises(asyncio.CancelledError):
        await deploy

    [row] = await rig.rows("stack_deployed")
    stack = present(
        await rig.db.fetch_one("SELECT status, last_operation, last_outcome FROM stack")
    )
    [binding] = await binding_rows(rig.db)
    sub_resources = present(
        await rig.db.fetch_one(
            "SELECT sub_resource_ids FROM resource_control WHERE resource_type = 'stack'"
        )
    )
    assert row["result"] == "success"
    assert row["detail"]["interrupted"] is True
    assert row["detail"]["pulled"] == 1
    assert row["detail"]["binding"] == "created"
    assert dict(stack) == {
        "status": "active",
        "last_operation": "deploy",
        "last_outcome": "success",
    }
    assert binding["registry_credential_id"] == set_id
    assert "container:" in sub_resources["sub_resource_ids"]
    assert len(await rows_for(rig.db, "image_bound")) == 1
    assert len(rig.engine.locks) == 0
    assert list(rig.compose.work_base.iterdir()) == []


async def test_a_cancel_that_outlasts_the_settle_window_keeps_the_locks_and_writes_one_late_success(
    stack_rig: Rig, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack_runs, "SETTLE_MARGIN_SECONDS", SETTLE_WINDOW - LATE_TIMEOUT)
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    await register(rig, "only", environments=(ENV_B,))
    await rig.services.actions.create(
        ADMIN,
        ENV_B,
        name="shop",
        compose_text=scripted_stack(up="slow", record=record, image=REFERENCE),
    )
    rig.sim.operation_timeout = LATE_TIMEOUT
    deploy = asyncio.ensure_future(rig.fresh().actions.deploy(ADMIN, ENV_B, "shop"))
    await poll_until(lambda: up_is_running(record))

    await cancelled(deploy)

    [unknown] = await rig.rows("stack_deployed")
    assert rig.engine.locks.holder_of(ObjectKey(ENV_B, "stack", "shop")) is not None
    assert unknown["result"] == "failure"
    assert unknown["detail"]["reason"] == "outcome_unknown"
    assert unknown["detail"]["interrupted"] is True
    assert "settled_late" not in unknown["detail"]

    await poll_until(lambda: len(rig.engine.locks) == 0)
    await poll_until(lambda: stack_deployed_rows(rig, 2))

    rows = await rig.rows("stack_deployed")
    [late] = [row for row in rows if row["result"] == "success"]
    stack = present(
        await rig.db.fetch_one("SELECT status, last_operation, last_outcome FROM stack")
    )
    assert len(rows) == 2
    assert late["detail"]["settled_late"] is True
    assert late["detail"]["interrupted"] is True
    assert late["detail"]["pulled"] == 1
    assert late["detail"]["binding"] == "kept"
    assert dict(stack) == {
        "status": "active",
        "last_operation": "deploy",
        "last_outcome": "success",
    }
    assert len(await binding_rows(rig.db)) == 1
    assert len(await rows_for(rig.db, "image_bound")) == 1
    assert list(rig.compose.work_base.iterdir()) == []


async def test_a_cancel_that_outlasts_the_settle_window_and_times_out_writes_one_late_failure(
    stack_rig: Rig, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack_runs, "SETTLE_MARGIN_SECONDS", SETTLE_WINDOW - LATE_TIMEOUT)
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    await rig.services.actions.create(
        ADMIN, ENV_B, name="shop", compose_text=scripted_stack(up="hang", record=record)
    )
    rig.sim.operation_timeout = SHORT_TIMEOUT
    deploy = asyncio.ensure_future(rig.fresh().actions.deploy(ADMIN, ENV_B, "shop"))
    await poll_until(lambda: up_is_running(record))

    await cancelled(deploy)

    [first] = await rig.rows("stack_deployed")
    assert first["detail"]["reason"] == "outcome_unknown"
    assert "settled_late" not in first["detail"]

    await poll_until(lambda: stack_deployed_rows(rig, 2))
    await poll_until(lambda: len(rig.engine.locks) == 0)

    rows = await rig.rows("stack_deployed")
    assert [row["result"] for row in rows] == ["failure", "failure"]
    assert rows[1]["detail"]["settled_late"] is True
    assert rows[1]["detail"]["reason"] == "outcome_unknown"
    assert list(rig.compose.work_base.iterdir()) == []


async def test_a_cancel_during_the_final_transaction_leaves_exactly_one_success_row(
    stack_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = stack_rig
    await rig.services.actions.create(ADMIN, ENV_B, name="shop", compose_text=PLAIN_STACK)
    actions = rig.fresh().actions
    entered = asyncio.Event()
    runner = actions._runner
    finalise = runner._finalise

    async def stalled_once(*args: Any) -> None:
        if not entered.is_set():
            entered.set()
            await asyncio.Event().wait()
        await finalise(*args)

    monkeypatch.setattr(runner, "_finalise", stalled_once)
    deploy = asyncio.ensure_future(actions.deploy(ADMIN, ENV_B, "shop"))
    async with asyncio.timeout(10):
        await entered.wait()

    await cancelled(deploy)

    [row] = await rig.rows("stack_deployed")
    stack = present(await rig.db.fetch_one("SELECT status, last_outcome FROM stack"))
    assert row["result"] == "success"
    assert row["detail"]["interrupted"] is True
    assert dict(stack) == {"status": "active", "last_outcome": "success"}
    assert len(rig.engine.locks) == 0


async def test_one_deadline_covers_the_pulls_and_a_hung_pull_ends_as_outcome_unknown(
    stack_rig: Rig, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack_runs, "DEADLINE_SLACK_SECONDS", 0.2)
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    await rig.services.actions.create(
        ADMIN,
        ENV_B,
        name="shop",
        compose_text=scripted_stack(up="ok", record=record, image=REFERENCE),
    )
    rig.sim.operation_timeout = SHORT_TIMEOUT
    rig.host.hold("pull")

    with pytest.raises(StackOperationFailed) as timed_out:
        await rig.fresh().actions.deploy(ADMIN, ENV_B, "shop")

    rig.host.release_gates()
    [row] = await rig.rows("stack_deployed")
    stack = present(await rig.db.fetch_one("SELECT status, last_outcome FROM stack"))
    assert timed_out.value.reason == "outcome_unknown"
    assert row["detail"]["reason"] == "outcome_unknown"
    assert dict(stack) == {"status": "error", "last_outcome": "outcome_unknown"}
    assert {call["cmd"] for call in read_records(record)} == {"config"}
    assert len(rig.engine.locks) == 0


async def test_a_failed_pull_ends_the_deploy_before_compose_runs_and_shows_no_engine_text(
    stack_rig: Rig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    await rig.services.actions.create(
        ADMIN,
        ENV_B,
        name="shop",
        compose_text=scripted_stack(up="ok", record=record, image=REFERENCE),
    )
    rig.host.pull_status = 500

    with pytest.raises(StackOperationFailed) as failed:
        await rig.fresh().actions.deploy(ADMIN, ENV_B, "shop")

    [row] = await rig.rows("stack_deployed")
    stack = present(await rig.db.fetch_one("SELECT status, last_outcome FROM stack"))
    assert failed.value.reason == "pull_failed"
    assert ENGINE_TEXT not in str(failed.value)
    assert ENGINE_TEXT not in await activity_dump(rig)
    assert row["detail"]["reason"] == "pull_failed"
    assert dict(stack) == {"status": "error", "last_outcome": "failure"}
    assert {call["cmd"] for call in read_records(record)} == {"config"}


async def test_a_failed_remove_marks_the_stack_in_error_and_keeps_it(
    stack_rig: Rig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    rig = stack_rig
    record = tmp_path_factory.mktemp("records") / "calls.jsonl"
    text = scripted_stack(up="ok", record=record) + "  down: fail\n"
    await create_then_deploy(rig.services.actions, ADMIN, ENV_B, name="shop", compose_text=text)

    bound = await removal_binding(rig, ADMIN, ENV_B, "shop")

    with pytest.raises(StackOperationFailed) as failed:
        await rig.fresh().actions.remove(ADMIN, ENV_B, "shop", **bound)

    [row] = await rig.rows("stack_removed")
    stack = present(
        await rig.db.fetch_one("SELECT status, last_operation, last_outcome FROM stack")
    )
    control = await rig.db.fetch_one("SELECT 1 FROM resource_control WHERE resource_type = 'stack'")
    assert failed.value.reason == "compose_failed"
    assert failed.value.exit_code == 4
    assert "down failed" in failed.value.stderr
    assert row["detail"]["reason"] == "compose_failed"
    assert dict(stack) == {"status": "error", "last_operation": "remove", "last_outcome": "failure"}
    assert control is not None
    assert list(rig.compose.work_base.iterdir()) == []
