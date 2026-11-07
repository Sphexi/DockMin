"""Startup self-check: each failed precondition logs one structured line and raises once."""

import base64
import io
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from app.config import (
    MANAGED_KEY_FILENAME,
    PREVIOUS_SECRET_KEY_FILE_VAR,
    PREVIOUS_SECRET_KEY_VAR,
    SECRET_KEY_DIR_VAR,
    SECRET_KEY_FILE_VAR,
    SECRET_KEY_VAR,
    TLS_CERT_FILE_VAR,
    TLS_SELF_SIGNED_VAR,
    ConfigError,
    Settings,
    load_settings,
)
from app.crypto.managed_key import ManagedKeyRefused
from app.db.engine import Database, DatabaseError
from app.db.repositories.crypto_state import CryptoStateRepository
from app.logging import configure_logging
from app.main import create_app
from app.selfcheck import (
    StartupCheckFailed,
    check_compose,
    check_data_directory,
    check_sqlite_tmpdir,
    compose_directories_unusable,
    from_config_error,
    from_managed_key_refusal,
    log_check_failure,
)
from app.serving import launcher
from tests.conftest import COMPOSE_BINARY_VAR, DATA_DIR_VAR
from tests.crypto.helpers import FIRST_KEY, SECOND_KEY, start
from tests.support import VALID_KEY
from tests.tls_support import write_server_files

EVENT = "startup_check_failed"
EXIT_CONFIGURATION = 2
EXIT_STARTUP_CHECK = 3
FLOOR = SimpleNamespace(text="2.20.0")
OWNER_ONLY_DIRECTORY = 0o700
OWNER_READ_ONLY = 0o400
DATABASE_ERRORS = [
    pytest.param(sqlite3.OperationalError("database is locked"), id="sqlite"),
    pytest.param(DatabaseError("the database is closed"), id="engine"),
]

Logged = Callable[[str], list[dict[str, Any]]]


def build(env: dict[str, str], log_stream: io.StringIO, **overrides: str) -> FastAPI:
    application = create_app(load_settings(env | overrides))
    configure_logging("DEBUG", stream=log_stream)
    return application


async def failure_of(application: FastAPI) -> StartupCheckFailed:
    with pytest.raises(StartupCheckFailed) as raised:
        async with application.router.lifespan_context(application):
            pass
    return raised.value


def assert_one_line(
    logged: Logged, log_stream: io.StringIO, *, check: str, fatal: bool
) -> dict[str, Any]:
    [line] = logged(EVENT)
    assert (line["check"], line["fatal"]) == (check, fatal)
    assert line["problem"] and line["remedy"]
    assert "Traceback" not in log_stream.getvalue()
    return line


def fresh_database(settings: Settings, template: Path) -> Path:
    shutil.copyfile(template, settings.db_path)
    return settings.db_path


async def test_a_data_directory_that_does_not_exist_stops_the_start_with_one_line(
    env: dict[str, str],
    tmp_path: Path,
    log_stream: io.StringIO,
    logged: Logged,
) -> None:
    application = build(env, log_stream, **{DATA_DIR_VAR: str(tmp_path / "absent")})

    failure = await failure_of(application)

    line = assert_one_line(logged, log_stream, check="data_directory", fatal=True)
    assert "does not exist" in line["problem"]
    assert failure.check == "data_directory"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to any directory")
async def test_a_data_directory_the_process_cannot_write_stops_the_start_with_one_line(
    env: dict[str, str], data_dir: Path, log_stream: io.StringIO, logged: Logged
) -> None:
    application = build(env, log_stream)
    data_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)  # noqa: ASYNC240
    try:
        await failure_of(application)
    finally:
        data_dir.chmod(stat.S_IRWXU)  # noqa: ASYNC240

    line = assert_one_line(logged, log_stream, check="data_directory", fatal=True)
    assert "not writable" in line["problem"]


def test_a_data_directory_that_is_a_symbolic_link_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(StartupCheckFailed) as raised:
        check_data_directory(link)

    assert "symbolic link" in raised.value.problem


def test_a_data_directory_that_is_a_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_text("x", encoding="utf-8")

    with pytest.raises(StartupCheckFailed) as raised:
        check_data_directory(path)

    assert "not a directory" in raised.value.problem


def test_a_sqlite_temporary_directory_is_created_when_configured_and_refused_when_it_cannot_be(
    tmp_path: Path,
) -> None:
    check_sqlite_tmpdir({"SQLITE_TMPDIR": str(tmp_path / "scratch" / "sqlite")})
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")

    with pytest.raises(StartupCheckFailed) as raised:
        check_sqlite_tmpdir({"SQLITE_TMPDIR": str(blocker / "sqlite")})

    assert (tmp_path / "scratch" / "sqlite").is_dir()
    assert raised.value.check == "data_directory"


async def test_a_key_that_does_not_open_the_stored_data_stops_the_start_with_one_line(
    env: dict[str, str],
    settings: Settings,
    migrated_template: Path,
    log_stream: io.StringIO,
    logged: Logged,
) -> None:
    await start(Database(fresh_database(settings, migrated_template)), SECOND_KEY)
    application = build(env, log_stream)

    await failure_of(application)

    line = assert_one_line(logged, log_stream, check="encryption_key", fatal=True)
    assert line["problem"].startswith(f"the configured {SECRET_KEY_VAR} does not match")
    assert VALID_KEY not in log_stream.getvalue()


async def test_a_database_written_by_a_newer_version_stops_the_start_with_one_line(
    env: dict[str, str],
    settings: Settings,
    migrated_template: Path,
    log_stream: io.StringIO,
    logged: Logged,
) -> None:
    path = fresh_database(settings, migrated_template)
    with sqlite3.connect(path) as connection:
        [(supported,)] = connection.execute("SELECT max(version) FROM schema_migration")
        connection.execute(
            "INSERT INTO schema_migration (version, name, checksum, applied_at) "
            "SELECT max(version) + 1, 'from_the_future', 'x', applied_at FROM schema_migration"
        )
    application = build(env, log_stream)

    await failure_of(application)

    line = assert_one_line(logged, log_stream, check="database", fatal=True)
    assert line["problem"] == (
        "The database schema check failed: This database was written by a newer dockmin "
        f"(schema {supported + 1}; this build supports {supported}). "
        "Run that version or restore a backup."
    )
    assert "restore a backup" not in line["remedy"]
    assert "Run that version" not in line["remedy"]


async def test_a_database_file_that_is_not_a_database_stops_the_start_with_one_line(
    env: dict[str, str],
    settings: Settings,
    log_stream: io.StringIO,
    logged: Logged,
) -> None:
    settings.db_path.write_bytes(b"this is not a sqlite database" * 100)
    application = build(env, log_stream)

    await failure_of(application)

    assert_one_line(logged, log_stream, check="database", fatal=True)


async def test_a_configured_compose_binary_that_is_not_trusted_stops_the_start_with_one_line(
    env: dict[str, str],
    tmp_path: Path,
    migrated_template: Path,
    settings: Settings,
    log_stream: io.StringIO,
    logged: Logged,
) -> None:
    fresh_database(settings, migrated_template)
    binary = tmp_path / "compose"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o777)
    application = build(env, log_stream, **{COMPOSE_BINARY_VAR: str(binary)})

    await failure_of(application)

    assert_one_line(logged, log_stream, check="compose_cli", fatal=True)


@pytest.mark.parametrize(
    ("compose", "fragment"),
    [
        pytest.param(
            SimpleNamespace(floor=FLOOR, binary=None, version=None, supported=False),
            "was not found",
            id="not-found",
        ),
        pytest.param(
            SimpleNamespace(floor=FLOOR, binary=object(), version=None, supported=False),
            "version could not be read",
            id="version-unreadable",
        ),
        pytest.param(
            SimpleNamespace(
                floor=FLOOR, binary=object(), version=SimpleNamespace(text="2.1.0"), supported=False
            ),
            "older than 2.20.0",
            id="below-the-floor",
        ),
    ],
)
def test_a_compose_that_cannot_run_stacks_is_reported_without_being_fatal(
    compose: Any, fragment: str
) -> None:
    failure = check_compose(compose)

    assert failure is not None
    assert (failure.check, failure.fatal) == ("compose_cli", False)
    assert fragment in failure.problem
    assert "2.20.0" in failure.remedy


def test_a_usable_compose_reports_nothing() -> None:
    usable: Any = SimpleNamespace(
        floor=FLOOR, binary=object(), version=SimpleNamespace(text="2.33.0"), supported=True
    )

    assert check_compose(usable) is None


def test_a_non_fatal_failure_is_logged_as_a_warning_and_says_it_is_not_fatal(
    log_stream: io.StringIO, logged: Logged
) -> None:
    failure = StartupCheckFailed("compose_cli", "problem text", "remedy text", fatal=False)

    log_check_failure(failure)

    [line] = logged(EVENT)
    assert (line["level"], line["fatal"], line["during"]) == ("WARNING", False, "startup")


def test_a_fatal_failure_during_a_restore_is_logged_as_not_fatal(
    log_stream: io.StringIO, logged: Logged
) -> None:
    failure = StartupCheckFailed("database", "problem text", "remedy text")

    log_check_failure(failure, during="restore")

    [line] = logged(EVENT)
    assert (line["level"], line["fatal"], line["during"]) == ("WARNING", False, "restore")


@pytest.fixture
def process_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> Callable[..., None]:
    for name in [name for name in os.environ if name.startswith("DOCKMIN_")]:
        monkeypatch.delenv(name)

    def apply(**overrides: str | None) -> None:
        for name, value in (env | overrides).items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    return apply


def stdout_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


@pytest.mark.usefixtures("root_logger_restored")
def test_two_key_sources_exit_with_the_configuration_status_and_one_line(
    process_env: Callable[..., None], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    supplied = tmp_path / "supplied.key"
    supplied.write_text(VALID_KEY, encoding="utf-8")
    process_env(**{SECRET_KEY_FILE_VAR: str(supplied)})

    status = launcher.main([])

    [line] = stdout_lines(capsys)
    assert status == EXIT_CONFIGURATION
    assert (line["message"], line["check"], line["fatal"]) == (EVENT, "encryption_key", True)
    assert SECRET_KEY_VAR in line["problem"] and SECRET_KEY_FILE_VAR in line["problem"]
    assert SECRET_KEY_VAR in line["remedy"] and SECRET_KEY_DIR_VAR in line["remedy"]


@pytest.mark.usefixtures("root_logger_restored")
def test_two_previous_key_sources_exit_with_the_configuration_status_and_one_line(
    process_env: Callable[..., None], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    previous = tmp_path / "previous.key"
    previous.write_text(base64.b64encode(SECOND_KEY).decode("ascii"), encoding="utf-8")
    process_env(
        **{
            PREVIOUS_SECRET_KEY_VAR: base64.b64encode(SECOND_KEY).decode("ascii"),
            PREVIOUS_SECRET_KEY_FILE_VAR: str(previous),
        }
    )

    status = launcher.main([])

    [line] = stdout_lines(capsys)
    assert status == EXIT_CONFIGURATION
    assert (line["message"], line["check"], line["fatal"]) == (EVENT, "encryption_key", True)
    assert PREVIOUS_SECRET_KEY_VAR in line["problem"]
    assert PREVIOUS_SECRET_KEY_FILE_VAR in line["problem"]


@pytest.mark.usefixtures("root_logger_restored")
def test_an_unreadable_previous_key_file_exits_with_the_configuration_status_naming_its_variable(
    process_env: Callable[..., None], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    process_env(**{PREVIOUS_SECRET_KEY_FILE_VAR: str(tmp_path / "absent.key")})

    status = launcher.main([])

    [line] = stdout_lines(capsys)
    assert status == EXIT_CONFIGURATION
    assert (line["message"], line["check"]) == (EVENT, "encryption_key")
    assert PREVIOUS_SECRET_KEY_FILE_VAR in line["problem"]


@pytest.mark.usefixtures("root_logger_restored")
def test_no_key_and_no_key_volume_exit_with_the_check_status_and_one_line(
    process_env: Callable[..., None], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    absent = tmp_path / "no-key-volume"
    process_env(**{SECRET_KEY_VAR: None, SECRET_KEY_DIR_VAR: str(absent)})

    status = launcher.main([])

    [line] = [entry for entry in stdout_lines(capsys) if entry["message"] == EVENT]
    assert status == EXIT_STARTUP_CHECK
    assert (line["check"], line["fatal"]) == ("encryption_key", True)
    assert line["problem"] == f"The key directory {absent} does not exist"
    assert SECRET_KEY_FILE_VAR in line["remedy"]
    assert not absent.exists()


@pytest.mark.usefixtures("root_logger_restored")
def test_a_configuration_error_that_is_not_a_check_is_logged_as_invalid_configuration(
    process_env: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    process_env(DOCKMIN_PORT="not-a-port")

    status = launcher.main([])

    [line] = stdout_lines(capsys)
    assert status == EXIT_CONFIGURATION
    assert line["message"] == "configuration_invalid"
    assert "DOCKMIN_PORT" in line["problem"]


@pytest.mark.usefixtures("root_logger_restored")
def test_a_certificate_pair_that_does_not_match_exits_with_the_check_status_and_one_line(
    process_env: Callable[..., None], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = write_server_files(tmp_path)
    (tmp_path / "other").mkdir()
    other = write_server_files(tmp_path / "other", key_variant=1)
    process_env(DOCKMIN_TLS_CERT_FILE=str(pair.cert), DOCKMIN_TLS_KEY_FILE=str(other.key))

    status = launcher.main([])

    [line] = stdout_lines(capsys)
    assert status == EXIT_STARTUP_CHECK
    assert (line["message"], line["check"], line["fatal"]) == (EVENT, "tls_material", True)
    assert "does not match" in line["problem"]
    assert "uid 10001" in line["remedy"]


@pytest.mark.usefixtures("root_logger_restored")
def test_a_certificate_file_that_is_missing_exits_with_the_check_status_and_one_line(
    process_env: Callable[..., None], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pair = write_server_files(tmp_path)
    pair.cert.unlink()
    process_env(DOCKMIN_TLS_CERT_FILE=str(pair.cert), DOCKMIN_TLS_KEY_FILE=str(pair.key))

    status = launcher.main([])

    [line] = stdout_lines(capsys)
    assert status == EXIT_STARTUP_CHECK
    assert line["check"] == "tls_material"


@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        pytest.param((SECRET_KEY_VAR,), "encryption_key", id="key"),
        pytest.param((SECRET_KEY_DIR_VAR,), "encryption_key", id="key-directory"),
        pytest.param(("DOCKMIN_TLS_CERT_FILE",), "tls_material", id="tls"),
        pytest.param((TLS_SELF_SIGNED_VAR,), None, id="self-signed-alone"),
        pytest.param(
            (TLS_SELF_SIGNED_VAR, TLS_CERT_FILE_VAR), "tls_material", id="self-signed-and-tls"
        ),
        pytest.param(("DOCKMIN_HTTP_MODE", TLS_SELF_SIGNED_VAR), None, id="mode-and-self-signed"),
        pytest.param(("DOCKMIN_PORT",), None, id="other"),
        pytest.param((), None, id="no-variable"),
    ],
)
def test_a_configuration_failure_is_attributed_to_its_check_or_to_none(
    variables: tuple[str, ...], expected: str | None
) -> None:
    failure = from_config_error(ConfigError("invalid configuration: x", variables))

    assert (None if failure is None else failure.check) == expected


def test_a_refused_generated_key_is_reported_as_an_encryption_key_failure_with_its_own_text() -> (
    None
):
    refusal = ManagedKeyRefused("the problem", "the remedy")

    failure = from_managed_key_refusal(refusal)

    assert (failure.check, failure.problem, failure.remedy, failure.fatal) == (
        "encryption_key",
        "the problem",
        "the remedy",
        True,
    )


async def test_a_compose_directory_that_cannot_be_prepared_stops_the_start_with_one_line(
    env: dict[str, str],
    settings: Settings,
    migrated_template: Path,
    log_stream: io.StringIO,
    logged: Logged,
) -> None:
    fresh_database(settings, migrated_template)
    (settings.data_dir / "stacks").write_text("not a directory", encoding="utf-8")
    application = build(env, log_stream)

    failure = await failure_of(application)

    line = assert_one_line(logged, log_stream, check="data_directory", fatal=True)
    assert line["problem"] == compose_directories_unusable().problem
    assert failure.check == "data_directory"


def raising(error: Exception) -> Callable[..., Any]:
    async def read(self: CryptoStateRepository) -> None:
        raise error

    return read


def key_volume_settings(env: dict[str, str], volume: Path) -> Settings:
    without_key = {name: value for name, value in env.items() if name != SECRET_KEY_VAR}
    return load_settings(without_key | {SECRET_KEY_DIR_VAR: str(volume)})


@pytest.mark.parametrize("error", DATABASE_ERRORS)
async def test_a_database_error_while_the_generated_key_is_sought_fails_the_database_check(
    env: dict[str, str],
    tmp_path: Path,
    migrated_template: Path,
    log_stream: io.StringIO,
    logged: Logged,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    volume = tmp_path / "key-volume"
    volume.mkdir(mode=OWNER_ONLY_DIRECTORY)
    settings = key_volume_settings(env, volume)
    fresh_database(settings, migrated_template)
    monkeypatch.setattr(CryptoStateRepository, "get", raising(error))
    application = create_app(settings)
    configure_logging("DEBUG", stream=log_stream)

    failure = await failure_of(application)

    line = assert_one_line(logged, log_stream, check="database", fatal=True)
    assert failure.check == "database"
    assert line["problem"].startswith(
        "The database could not be read while checking the encryption key:"
    )
    assert list(volume.iterdir()) == []


@pytest.mark.parametrize("error", DATABASE_ERRORS)
async def test_a_database_error_while_the_vault_is_established_fails_the_database_check(
    env: dict[str, str],
    tmp_path: Path,
    migrated_template: Path,
    log_stream: io.StringIO,
    logged: Logged,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    volume = tmp_path / "key-volume"
    volume.mkdir(mode=OWNER_ONLY_DIRECTORY)
    key_file = volume / MANAGED_KEY_FILENAME
    key_file.write_text(base64.b64encode(FIRST_KEY).decode("ascii"), encoding="utf-8")
    key_file.chmod(OWNER_READ_ONLY)
    settings = key_volume_settings(env, volume)
    fresh_database(settings, migrated_template)
    monkeypatch.setattr(CryptoStateRepository, "get", raising(error))
    application = create_app(settings)
    configure_logging("DEBUG", stream=log_stream)

    failure = await failure_of(application)

    line = assert_one_line(logged, log_stream, check="database", fatal=True)
    assert failure.check == "database"
    assert line["problem"].startswith(
        "The database could not be read while checking the encryption key:"
    )
    assert [entry.name for entry in volume.iterdir()] == [MANAGED_KEY_FILENAME]
    assert key_file.read_text(encoding="utf-8") == base64.b64encode(FIRST_KEY).decode("ascii")
