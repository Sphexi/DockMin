"""python -m app restore: review, replace, each refusal with its exit status, a failed start."""

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import app.backup.pipeline as pipeline_module
import app.services.restore_command as command_module
from app.__main__ import main
from app.backup.archive import read_manifest
from app.config import ENV_PREFIX, SECRET_KEY_VAR
from app.db.repositories.setting import RESTORE_AUTOMATION_PAUSED_KEY
from app.instance_lock import InstanceLock
from app.logging import DEFAULT_SCRUBBER
from tests.conftest import install_compose_stub, minimal_env
from tests.restore_support import (
    LIVE_ENVIRONMENT,
    LIVE_STACK,
    SECOND_KEY_TEXT,
    key_for_archive,
    sha,
    source_archive,
    target_template,
)

OK = 0
REFUSED = 1
USAGE = 2
BUSY = 3
UNRECOVERABLE = 4
OPERATOR = "alice"
STANDARD_USER = "bob"
LOCK_FILE = "dockmin.lock"
OVERSIZE_KEY = "A" * 2048
PIPED = False
TERMINAL = True


class Stdin:
    """Stands in for standard input: a binary buffer that is or is not a terminal."""

    def __init__(self, data: bytes, *, terminal: bool) -> None:
        self.buffer = io.BytesIO(data)
        self._terminal = terminal

    def isatty(self) -> bool:
        return self._terminal


@dataclass
class Result:
    status: int
    out: str
    err: str


@dataclass
class Live:
    """A stopped instance's data directory, the key files and the environment pointing at them."""

    data_dir: Path
    archive: Path
    key_file: Path
    archive_key: str
    root: Path

    def run(self, capsys: pytest.CaptureFixture[str], *arguments: str) -> Result:
        status = main(["restore", *arguments])
        captured = capsys.readouterr()
        return Result(status, captured.out, captured.err)

    def restore(
        self,
        capsys: pytest.CaptureFixture[str],
        *,
        operator: str = OPERATOR,
        replace: str | None = None,
        key_file: str | None = None,
        archive: Path | None = None,
    ) -> Result:
        arguments = [
            "--archive",
            str(archive or self.archive),
            "--key-file",
            key_file or str(self.key_file),
            "--operator",
            operator,
        ]
        if replace is not None:
            arguments += ["--replace", replace]
        return self.run(capsys, *arguments)

    def files(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for path in sorted(self.data_dir.rglob("*")):
            if path.is_file() and path.name != LOCK_FILE:
                found[str(path.relative_to(self.data_dir))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return found

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.query(self.data_dir / "dockmin.db", sql, params)

    @staticmethod
    def query(database: Path, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def restored_row(self) -> tuple[int, dict[str, Any]]:
        [row] = self.rows(
            "SELECT actor_user_id, detail FROM activity_event WHERE action = 'instance_restored'"
        )
        return int(row["actor_user_id"]), json.loads(row["detail"])

    def environments(self) -> list[str]:
        return [row[0] for row in self.rows("SELECT name FROM environment")]

    def restore_directories(self) -> list[Path]:
        base = self.data_dir / "restore"
        return sorted(base.iterdir()) if base.exists() else []


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    built, world = asyncio.run(source_archive(tmp_path_factory.mktemp("command-source")))
    asyncio.run(world.database.aclose())
    return built


@pytest.fixture(autouse=True)
def _process_state_restored(root_logger_restored: None) -> Iterator[None]:
    held = DEFAULT_SCRUBBER._secret_values
    yield
    DEFAULT_SCRUBBER._secret_values = held


def build_live(
    root: Path,
    provisioned_template: Path,
    archive: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_key: str | None,
) -> Live:
    data_dir = root / "data"
    data_dir.mkdir()
    data_dir.chmod(0o700)
    template = target_template(root, provisioned_template)
    (data_dir / "dockmin.db").write_bytes(template.read_bytes())
    stacks = data_dir / "stacks" / LIVE_STACK
    stacks.mkdir(parents=True)
    (stacks / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    environment = minimal_env(data_dir, install_compose_stub(root))
    environment.pop(SECRET_KEY_VAR)
    for name in [name for name in os.environ if name.startswith(ENV_PREFIX)]:
        monkeypatch.delenv(name)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    if target_key is not None:
        monkeypatch.setenv(SECRET_KEY_VAR, target_key)
    archive_key = key_for_archive().get_secret_value()
    key_file = root / "archive.key"
    key_file.write_text(f"{archive_key}\n", encoding="utf-8")
    own_copy = root / "backup.tar.gz"
    own_copy.write_bytes(archive.read_bytes())
    return Live(data_dir, own_copy, key_file, archive_key, root)


@pytest.fixture
def live(
    tmp_path: Path,
    provisioned_template: Path,
    archive: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Live:
    return build_live(
        tmp_path, provisioned_template, archive, monkeypatch, target_key=SECOND_KEY_TEXT
    )


@pytest.fixture
def same_key_live(
    tmp_path: Path,
    provisioned_template: Path,
    archive: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Live:
    key = key_for_archive().get_secret_value()
    return build_live(tmp_path, provisioned_template, archive, monkeypatch, target_key=key)


def test_a_review_prints_the_archive_its_digest_and_the_paths_that_would_move_and_changes_nothing(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    before = live.files()

    result = live.restore(capsys)

    assert result.status == OK
    assert f"SHA-256: {sha(live.archive)}" in result.out
    assert "Key check: the key matches the archive and signs its manifest" in result.out
    assert f"Administrators: {OPERATOR}" in result.out
    assert str(live.data_dir / "dockmin.db") in result.out
    assert str(live.data_dir / "stacks") in result.out
    assert f"run again with --replace {sha(live.archive)}" in result.out
    assert "Nothing was changed." in result.out
    assert live.files() == before
    assert live.restore_directories() == []


def test_a_review_states_that_secrets_would_be_resealed_when_the_target_key_differs(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    result = live.restore(capsys)

    assert "would be re-sealed to the target key" in result.out


def test_a_review_states_that_secrets_stay_when_the_target_key_is_the_archives(
    same_key_live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    result = same_key_live.restore(capsys)

    assert "stay under the archive's key, which is the target key" in result.out


def test_a_review_reads_the_key_from_standard_input_when_it_is_piped(
    live: Live, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", Stdin(f"{live.archive_key}\n".encode(), terminal=PIPED))

    result = live.restore(capsys, key_file="-")

    assert result.status == OK and "Nothing was changed." in result.out


def test_a_replace_value_that_is_not_the_archives_digest_refuses_before_staging(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    before = live.files()

    result = live.restore(capsys, replace="0" * 64)

    assert result.status == REFUSED
    assert sha(live.archive) in result.err and "Nothing was changed." in result.err
    assert live.files() == before
    assert live.restore_directories() == []


def test_a_replace_with_the_archives_digest_swaps_the_data_in_and_keeps_the_previous_data(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    result = live.restore(capsys, replace=sha(live.archive))

    [directory] = live.restore_directories()
    kept = Live.query(directory / "previous" / "dockmin.db", "SELECT name FROM environment")
    assert result.status == OK
    assert "Restored." in result.out
    assert LIVE_ENVIRONMENT not in live.environments()
    assert [row[0] for row in kept] == [LIVE_ENVIRONMENT]
    assert sorted(entry.name for entry in directory.iterdir()) == ["previous"]
    assert not (live.data_dir / "stacks" / LIVE_STACK).exists()
    assert (directory / "previous" / "stacks" / LIVE_STACK).exists()


def test_a_replace_records_the_operator_command_source_the_named_administrator_and_the_pause(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    live.restore(capsys, replace=sha(live.archive))

    actor, detail = live.restored_row()
    [administrator] = live.rows("SELECT id FROM user WHERE username = ?", (OPERATOR,))
    [paused] = live.rows(
        "SELECT value_type, value FROM setting WHERE key = ?", (RESTORE_AUTOMATION_PAUSED_KEY,)
    )
    [directory] = live.restore_directories()
    assert actor == administrator["id"]
    assert detail["source"] == "operator_command"
    assert detail["operator_username"] == OPERATOR
    assert detail["archive_sha256"] == sha(live.archive)
    assert "object_storage_target_id" not in detail
    assert (paused["value_type"], paused["value"]) == ("string", directory.name)


def test_the_operator_is_matched_to_an_administrator_of_the_archive_without_regard_to_case(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    result = live.restore(capsys, operator=OPERATOR.upper(), replace=sha(live.archive))

    _, detail = live.restored_row()
    assert result.status == OK
    assert detail["operator_username"] == OPERATOR.upper()


def test_a_restore_prints_each_step_and_never_the_key(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    result = live.restore(capsys, replace=sha(live.archive))

    assert "[1/4] Verifying the archive members" in result.out
    assert "[4/4] Decrypting and finalising the staged database" in result.out
    assert "Checking that the restored data starts" in result.out
    assert "Automation is held at the next start" in result.out
    assert live.archive_key not in result.out + result.err
    assert SECOND_KEY_TEXT not in result.out + result.err


def test_the_file_replaced_after_it_was_hashed_still_restores_the_reviewed_bytes(
    live: Live, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewed = sha(live.archive)
    real = read_manifest

    def replaced_then_read(path: Path) -> Any:
        live.archive.write_bytes(b"replaced after the hash was taken")
        return real(path)

    monkeypatch.setattr(command_module, "read_manifest", replaced_then_read)

    result = live.restore(capsys, replace=reviewed)

    _, detail = live.restored_row()
    assert result.status == OK
    assert detail["archive_sha256"] == reviewed
    assert LIVE_ENVIRONMENT not in live.environments()


def test_a_restore_is_refused_while_the_instance_lock_is_held(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = InstanceLock(live.data_dir)
    lock.acquire()
    before = live.files()

    try:
        result = live.restore(capsys, replace=sha(live.archive))
    finally:
        lock.release()

    assert result.status == BUSY and "Another dockmin process" in result.err
    assert live.files() == before


def test_a_restore_is_refused_while_an_update_directory_exists(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    (live.data_dir / "update" / ("ab" * 16)).mkdir(parents=True)
    before = live.files()

    result = live.restore(capsys, replace=sha(live.archive))

    assert result.status == BUSY and "update is in progress" in result.err
    assert live.files() == before
    assert live.restore_directories() == []


def test_a_restore_is_refused_when_the_effective_user_does_not_own_the_data_directory(
    live: Live, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = live.data_dir.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    before = live.files()

    result = live.restore(capsys, replace=sha(live.archive))

    assert result.status == USAGE and "Run as the user that owns" in result.err
    assert live.files() == before


def test_a_restore_is_refused_when_no_target_key_can_be_resolved_and_none_is_created(
    live: Live, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(SECRET_KEY_VAR)
    before = live.files()

    result = live.restore(capsys, replace=sha(live.archive))

    assert result.status == USAGE and "never creates one" in result.err
    assert live.files() == before
    assert not (live.data_dir / "secret.key").exists()


@pytest.mark.parametrize(
    "operator", [STANDARD_USER, "nobody"], ids=["a-standard-user", "an-unknown-name"]
)
def test_an_operator_who_is_not_an_administrator_of_the_archive_is_refused(
    live: Live, capsys: pytest.CaptureFixture[str], operator: str
) -> None:
    before = live.files()

    result = live.restore(capsys, operator=operator, replace=sha(live.archive))

    assert result.status == REFUSED and "--operator" in result.err
    assert live.files() == before
    assert live.restore_directories() == []


def test_a_key_read_from_a_terminal_is_refused(
    live: Live, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", Stdin(b"", terminal=TERMINAL))

    result = live.restore(capsys, key_file="-")

    assert result.status == USAGE and "not read from a terminal" in result.err


def test_a_key_that_does_not_belong_to_the_archive_is_refused_and_changes_nothing(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    live.key_file.write_text(SECOND_KEY_TEXT, encoding="utf-8")
    before = live.files()

    result = live.restore(capsys, replace=sha(live.archive))

    assert result.status == REFUSED
    assert live.files() == before


def test_a_key_file_longer_than_the_limit_is_refused(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    live.key_file.write_text(OVERSIZE_KEY, encoding="utf-8")

    result = live.restore(capsys)

    assert result.status == USAGE and "longer than" in result.err


def test_a_key_file_that_does_not_exist_is_refused(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    result = live.restore(capsys, key_file=str(live.root / "absent.key"))

    assert result.status == USAGE and "key file cannot be read" in result.err


def test_an_archive_that_does_not_exist_is_refused(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    result = live.restore(capsys, archive=live.root / "absent.tar.gz")

    assert result.status == USAGE and "archive cannot be read" in result.err


def test_a_file_that_is_not_a_backup_is_refused_and_changes_nothing(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    junk = live.root / "junk.tar.gz"
    junk.write_bytes(b"\x1f\x8b" + b"junk" * 1000)
    before = live.files()

    result = live.restore(capsys, archive=junk, replace=sha(junk))

    assert result.status == REFUSED
    assert live.files() == before
    assert live.restore_directories() == []


def test_an_archive_cut_short_is_refused_at_verification_and_changes_nothing(
    live: Live, capsys: pytest.CaptureFixture[str]
) -> None:
    cut = live.root / "cut.tar.gz"
    cut.write_bytes(live.archive.read_bytes()[:-2000])
    before = live.files()

    result = live.restore(capsys, archive=cut, replace=sha(cut))

    assert result.status == REFUSED
    assert live.files() == before


def test_a_missing_required_argument_ends_with_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["restore", "--operator", OPERATOR])

    assert raised.value.code == USAGE


def break_start(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    async def failing(*arguments: object, **options: object) -> None:
        raise error

    monkeypatch.setattr(command_module, "establish_vault", failing)


def assert_previous_data_is_back_and_restored_data_is_rejected(live: Live) -> None:
    [directory] = live.restore_directories()
    rejected = Live.query(directory / "rejected" / "dockmin.db", "SELECT name FROM environment")
    assert live.environments() == [LIVE_ENVIRONMENT]
    assert (live.data_dir / "stacks" / LIVE_STACK).exists()
    assert LIVE_ENVIRONMENT not in [row[0] for row in rejected]
    assert (directory / "rejected" / "stacks" / ("e" * 32)).exists()
    assert not (directory / "previous" / "dockmin.db").exists()


def test_a_failing_start_check_moves_the_restored_data_to_rejected_and_returns_the_previous(
    live: Live, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    break_start(monkeypatch, sqlite3.OperationalError("disk I/O error"))

    result = live.restore(capsys, replace=sha(live.archive))

    assert result.status == REFUSED
    assert "did not start" in result.err and "previous data was put back" in result.err
    assert_previous_data_is_back_and_restored_data_is_rejected(live)


def test_a_start_check_that_fails_unexpectedly_also_puts_the_previous_data_back(
    live: Live, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    break_start(monkeypatch, RuntimeError("boom"))

    result = live.restore(capsys, replace=sha(live.archive))

    assert result.status == UNRECOVERABLE
    assert "previous data was put back" in result.err
    assert_previous_data_is_back_and_restored_data_is_rejected(live)


def test_a_failing_pipeline_step_changes_nothing_and_leaves_no_restore_directory(
    live: Live, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*arguments: object, **options: object) -> None:
        raise OSError("stage failed")

    monkeypatch.setattr(pipeline_module, "stage_members", broken)
    before = live.files()

    result = live.restore(capsys, replace=sha(live.archive))

    assert result.status == REFUSED and "Nothing was changed." in result.err
    assert live.files() == before
    assert live.restore_directories() == []
