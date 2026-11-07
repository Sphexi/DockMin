"""A start that finds the pause a restore left holds the scheduler until it is resumed."""

import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.auth.principal import Principal
from app.config import Settings
from app.db.repositories.setting import RESTORE_AUTOMATION_PAUSED_KEY
from tests.conftest import serving_app

ADMINISTRATOR = Principal(user_id=1, username="admin-user", role="administrator", session_id=1)
RESTORE_ID = "d" * 32
STAMP = "2026-01-01T00:00:00Z"


@pytest.fixture
def paused_template(provisioned_template: Path, tmp_path: Path) -> Path:
    path = tmp_path / "paused-template.db"
    shutil.copyfile(provisioned_template, path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO setting (key, value_type, value, updated_at) VALUES (?, 'string', ?, ?)",
            (RESTORE_AUTOMATION_PAUSED_KEY, RESTORE_ID, STAMP),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def suspensions(application: FastAPI) -> int:
    return application.state.scheduler.scheduler._suspended  # type: ignore[no-any-return]


async def setting_rows(application: FastAPI) -> int:
    async with application.state.database.session() as db:
        found = await db.fetch_limited(
            "SELECT 1 FROM setting WHERE key = ?", (RESTORE_AUTOMATION_PAUSED_KEY,), limit=2
        )
    return len(found)


async def test_a_start_that_finds_the_pause_setting_holds_the_scheduler_and_the_gate(
    app: FastAPI, settings: Settings, paused_template: Path
) -> None:
    async with serving_app(app, settings, paused_template):
        held = (suspensions(app), app.state.restore_gate.automation_paused)

    assert held == (1, True)


async def test_a_start_without_the_pause_setting_runs_the_scheduler(
    app: FastAPI, settings: Settings, provisioned_template: Path
) -> None:
    async with serving_app(app, settings, provisioned_template):
        held = (suspensions(app), app.state.restore_gate.automation_paused)

    assert held == (0, False)


async def test_resuming_after_a_restart_releases_the_scheduler_and_deletes_the_setting(
    app: FastAPI, settings: Settings, paused_template: Path
) -> None:
    async with serving_app(app, settings, paused_template):
        await app.state.restore.resume_automation(ADMINISTRATOR)

        released = (
            suspensions(app),
            app.state.restore_gate.automation_paused,
            await setting_rows(app),
        )

    assert released == (0, False, 0)
