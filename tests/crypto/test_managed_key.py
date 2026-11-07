"""Tests for app/crypto/managed_key.py: the generated key file and the rules for creating it."""

import asyncio
import base64
import errno
import io
import os
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from app.config import (
    KEY_BYTES,
    MANAGED_KEY_FILENAME,
    PREVIOUS_SECRET_KEY_FILE_VAR,
    PREVIOUS_SECRET_KEY_VAR,
    SECRET_KEY_DIR_VAR,
    SECRET_KEY_FILE_VAR,
    SECRET_KEY_VAR,
    Settings,
    load_settings,
)
from app.crypto import managed_key
from app.crypto.keys import MasterKeys, key_fingerprint, key_forms
from app.crypto.managed_key import (
    ManagedKeyRefused,
    filesystem_at,
    mount_point,
    mounted_filesystem,
    resolve_master_keys,
)
from app.db.engine import Database
from app.logging import REDACTED, Scrubber
from app.services.restore import restore_leftover
from tests.crypto.helpers import (
    FIRST_KEY,
    SECOND_KEY,
    THIRD_KEY,
    execute_sql,
    leaked,
    seed_row,
    start,
)
from tests.support import KEY_MATERIAL, VALID_KEY

OWNER_READ_ONLY = 0o400
BROADLY_READABLE = 0o644
OWNER_ONLY_DIRECTORY = 0o700
GROUP_WRITABLE_DIRECTORY = 0o770
NO_ACCESS = 0o000
FIFO_SECONDS = 10
GENERATED_EVENT = "encryption_key_generated"
PERMISSIONS_EVENT = "encryption_key_file_permissions"
RESTORE_ID = "r" * 32
LEFTOVER_TEMPORARY = f".{MANAGED_KEY_FILENAME}.tmp-0123456789abcdef"
UNREADABLE_AS_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
MISSING_KEY_REMEDY = (
    "Mount the key volume this instance was created with at {directory}, or supply its key "
    f"with {SECRET_KEY_FILE_VAR}. A new key cannot read the stored credentials"
)
MOUNT_REMEDY = (
    "Mount a volume at {directory}, for example -v <volume-name>:{directory}, "
    f"or set {SECRET_KEY_FILE_VAR}"
)
UNUSABLE_FILE_REMEDY = (
    "Restore the key file from its copy. dockmin never replaces an existing key file"
)


@pytest.fixture
def key_dir(tmp_path: Path) -> Path:
    path = tmp_path / "key"
    path.mkdir(mode=OWNER_ONLY_DIRECTORY)
    path.chmod(OWNER_ONLY_DIRECTORY)
    return path


@pytest.fixture
def key_file(key_dir: Path) -> Path:
    return key_dir / MANAGED_KEY_FILENAME


@pytest.fixture
def mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed_key, "mount_point", lambda path: True)


@pytest.fixture
def generated(env: dict[str, str], key_dir: Path, mounted: None) -> Settings:
    """Settings that name no key, so one is generated in key_dir."""
    return generated_settings(env, key_dir)


def generated_settings(env: dict[str, str], key_dir: Path, **extra: str) -> Settings:
    without_key = {name: value for name, value in env.items() if name != SECRET_KEY_VAR}
    return load_settings(without_key | {SECRET_KEY_DIR_VAR: str(key_dir)} | extra)


def encoded(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


async def refusal_of(settings: Settings, database: Database) -> ManagedKeyRefused:
    with pytest.raises(ManagedKeyRefused) as raised:
        await resolve_master_keys(
            settings, database, restore_leftover=restore_leftover, scrubber=Scrubber()
        )
    return raised.value


def names_in(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def stat_of(path: Path) -> os.stat_result:
    return path.stat()


def text_of(path: Path) -> str:
    return path.read_text(encoding="ascii")


def bytes_of(path: Path) -> bytes:
    return path.read_bytes()


def present(path: Path) -> bool:
    return path.exists()


def is_link(path: Path) -> bool:
    return path.is_symlink()


def link_to(link: Path, target: Path) -> None:
    link.symlink_to(target)


def make_directory(path: Path) -> None:
    path.mkdir()


def restrict(path: Path, mode: int) -> None:
    path.chmod(mode)


def write_key_file(path: Path, content: bytes, mode: int = OWNER_READ_ONLY) -> None:
    path.write_bytes(content)
    path.chmod(mode)


def plant_key_file(directory_fd: int, key: bytes) -> None:
    descriptor = os.open(
        MANAGED_KEY_FILENAME,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        OWNER_READ_ONLY,
        dir_fd=directory_fd,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(base64.b64encode(key) + b"\n")


async def test_a_new_database_gets_a_read_only_key_file_owned_by_the_process(
    generated: Settings,
    database: Database,
    key_file: Path,
    key_dir: Path,
    logged: Callable[[str], list[dict[str, Any]]],
    log_stream: io.StringIO,
) -> None:
    scrubber = Scrubber()

    keys = await resolve_master_keys(
        generated, database, restore_leftover=restore_leftover, scrubber=scrubber
    )

    status = stat_of(key_file)
    [line] = logged(GENERATED_EVENT)
    assert stat.S_IMODE(status.st_mode) == OWNER_READ_ONLY
    assert status.st_uid == os.geteuid()
    assert base64.b64decode(text_of(key_file)) == keys.current
    assert len(keys.current) == KEY_BYTES and keys.previous is None
    assert names_in(key_dir) == [MANAGED_KEY_FILENAME]
    assert (line["level"], line["key_file"]) == ("WARNING", str(key_file))
    assert line["key_fingerprint"] == key_fingerprint(keys.current)
    assert leaked(log_stream.getvalue(), keys.current) == []
    assert {scrubber.scrub_text(form) for form in key_forms(keys.current)} == {REDACTED}


async def test_an_existing_key_file_is_read_and_never_rewritten(
    generated: Settings,
    database: Database,
    key_file: Path,
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    write_key_file(key_file, encoded(KEY_MATERIAL).encode("ascii") + b"\n")
    before = stat_of(key_file)
    scrubber = Scrubber()

    keys = await resolve_master_keys(
        generated, database, restore_leftover=restore_leftover, scrubber=scrubber
    )

    after = stat_of(key_file)
    assert keys == MasterKeys(KEY_MATERIAL)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert text_of(key_file) == VALID_KEY + "\n"
    assert logged(GENERATED_EVENT) == [] and logged(PERMISSIONS_EVENT) == []
    assert {scrubber.scrub_text(form) for form in key_forms(KEY_MATERIAL)} == {REDACTED}


async def test_a_second_start_reads_the_key_the_first_generated(
    generated: Settings, database: Database, key_file: Path
) -> None:
    first = await resolve_master_keys(
        generated, database, restore_leftover=restore_leftover, scrubber=Scrubber()
    )
    before = stat_of(key_file)

    second = await resolve_master_keys(
        generated, database, restore_leftover=restore_leftover, scrubber=Scrubber()
    )

    assert second.current == first.current
    assert stat_of(key_file).st_ino == before.st_ino


async def test_a_previous_key_is_returned_beside_the_key_in_the_file(
    env: dict[str, str], key_dir: Path, key_file: Path, database: Database, mounted: None
) -> None:
    write_key_file(key_file, encoded(FIRST_KEY).encode("ascii"))
    settings = generated_settings(env, key_dir, **{PREVIOUS_SECRET_KEY_VAR: encoded(SECOND_KEY)})

    keys = await resolve_master_keys(
        settings, database, restore_leftover=restore_leftover, scrubber=Scrubber()
    )

    assert keys == MasterKeys(FIRST_KEY, SECOND_KEY)


@pytest.fixture
def previous_file(tmp_path: Path) -> Path:
    path = tmp_path / "previous.key"
    path.write_text(encoded(SECOND_KEY), encoding="utf-8")
    return path


async def test_a_previous_key_file_is_returned_beside_the_key_in_the_file(
    env: dict[str, str],
    key_dir: Path,
    key_file: Path,
    previous_file: Path,
    database: Database,
    mounted: None,
) -> None:
    write_key_file(key_file, encoded(FIRST_KEY).encode("ascii"))
    settings = generated_settings(
        env, key_dir, **{PREVIOUS_SECRET_KEY_FILE_VAR: str(previous_file)}
    )

    keys = await resolve_master_keys(
        settings, database, restore_leftover=restore_leftover, scrubber=Scrubber()
    )

    assert keys == MasterKeys(FIRST_KEY, SECOND_KEY)


async def test_a_previous_key_file_without_a_key_file_blocks_generation_and_names_its_source(
    env: dict[str, str],
    key_dir: Path,
    key_file: Path,
    previous_file: Path,
    database: Database,
    mounted: None,
) -> None:
    settings = generated_settings(
        env, key_dir, **{PREVIOUS_SECRET_KEY_FILE_VAR: str(previous_file)}
    )

    refused = await refusal_of(settings, database)

    assert refused.problem == (
        f"The previous key, from the key in {previous_file} ({PREVIOUS_SECRET_KEY_FILE_VAR}), "
        f"is set but {key_file} does not exist"
    )
    assert refused.remedy == MISSING_KEY_REMEDY.format(directory=key_dir)
    assert not present(key_file)


async def test_a_previous_key_file_equal_to_the_key_file_is_refused_naming_its_source(
    env: dict[str, str],
    key_dir: Path,
    key_file: Path,
    previous_file: Path,
    database: Database,
    mounted: None,
) -> None:
    write_key_file(key_file, encoded(SECOND_KEY).encode("ascii"))
    settings = generated_settings(
        env, key_dir, **{PREVIOUS_SECRET_KEY_FILE_VAR: str(previous_file)}
    )

    refused = await refusal_of(settings, database)

    assert refused.problem == (
        f"The previous key, from the key in {previous_file} ({PREVIOUS_SECRET_KEY_FILE_VAR}), "
        f"must differ from the key in {key_file}"
    )
    assert refused.remedy.startswith(f"Remove {PREVIOUS_SECRET_KEY_FILE_VAR}, or set it")


async def test_a_key_file_readable_by_others_warns_and_the_start_continues(
    generated: Settings,
    database: Database,
    key_file: Path,
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    write_key_file(key_file, encoded(FIRST_KEY).encode("ascii"), BROADLY_READABLE)

    keys = await resolve_master_keys(
        generated, database, restore_leftover=restore_leftover, scrubber=Scrubber()
    )

    [line] = logged(PERMISSIONS_EVENT)
    assert keys.current == FIRST_KEY
    assert (line["level"], line["mode"], line["key_file"]) == ("WARNING", "0644", str(key_file))


@pytest.mark.parametrize("source", ["variable", "file"])
async def test_a_supplied_key_never_touches_the_key_directory(
    env: dict[str, str], tmp_path: Path, database: Database, source: str
) -> None:
    supplied_file = tmp_path / "supplied.key"
    supplied_file.write_text(VALID_KEY, encoding="utf-8")
    absent = tmp_path / "no-such-key-directory"
    supplied = {
        "variable": {SECRET_KEY_VAR: VALID_KEY},
        "file": {SECRET_KEY_FILE_VAR: str(supplied_file)},
    }[source]
    settings = load_settings(
        {name: value for name, value in env.items() if name != SECRET_KEY_VAR}
        | supplied
        | {SECRET_KEY_DIR_VAR: str(absent)}
    )

    keys = await resolve_master_keys(
        settings, database, restore_leftover=restore_leftover, scrubber=Scrubber()
    )

    assert keys.current == KEY_MATERIAL
    assert not absent.exists()


def remove_directory(key_dir: Path) -> None:
    key_dir.rmdir()


def replace_with_a_file(key_dir: Path) -> None:
    key_dir.rmdir()
    key_dir.write_text("x", encoding="utf-8")


def replace_with_a_link(key_dir: Path) -> None:
    target = key_dir.parent / "elsewhere"
    target.mkdir(mode=OWNER_ONLY_DIRECTORY)
    key_dir.rmdir()
    key_dir.symlink_to(target)


@pytest.mark.parametrize(
    ("break_directory", "problem"),
    [
        pytest.param(remove_directory, "The key directory {directory} does not exist", id="absent"),
        pytest.param(
            replace_with_a_file, "The key directory {directory} is not a directory", id="file"
        ),
        pytest.param(
            replace_with_a_link, "The key directory {directory} is a symbolic link", id="link"
        ),
    ],
)
async def test_a_key_directory_that_is_absent_a_file_or_a_link_is_refused_with_the_volume_remedy(
    generated: Settings,
    database: Database,
    key_dir: Path,
    break_directory: Callable[[Path], None],
    problem: str,
) -> None:
    break_directory(key_dir)

    refused = await refusal_of(generated, database)

    assert refused.problem == problem.format(directory=key_dir)
    assert refused.remedy == MOUNT_REMEDY.format(directory=key_dir)


async def test_a_key_directory_that_is_not_a_mount_point_is_refused(
    env: dict[str, str], key_dir: Path, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed_key, "mount_point", lambda path: False)

    refused = await refusal_of(generated_settings(env, key_dir), database)

    assert refused.problem == (
        f"The key directory {key_dir} is not a mounted volume; a key written there is lost when "
        "the container is replaced"
    )
    assert refused.remedy == MOUNT_REMEDY.format(directory=key_dir)
    assert names_in(key_dir) == []


@pytest.mark.parametrize("filesystem", ["tmpfs", "ramfs"])
async def test_a_key_directory_on_memory_backed_storage_is_refused(
    generated: Settings,
    database: Database,
    key_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    filesystem: str,
) -> None:
    monkeypatch.setattr(managed_key, "mounted_filesystem", lambda directory: filesystem)

    refused = await refusal_of(generated, database)

    assert refused.problem == (
        f"The key directory {key_dir} is a {filesystem} mount; a key written there is lost when "
        "the container stops"
    )
    assert refused.remedy == MOUNT_REMEDY.format(directory=key_dir)
    assert names_in(key_dir) == []


def ownership_remedy() -> str:
    uid = os.geteuid()
    return (
        "A named volume takes this from the image, which runs as uid 10001. For a bind mount, "
        f"give the host directory to uid {uid}, the user this process runs as, with mode 0700"
    )


async def test_a_key_directory_others_can_write_to_is_refused(
    generated: Settings, database: Database, key_dir: Path
) -> None:
    restrict(key_dir, GROUP_WRITABLE_DIRECTORY)

    refused = await refusal_of(generated, database)

    assert refused.problem == (
        f"The key directory {key_dir} must be owned by uid {os.geteuid()} and writable only by it"
    )
    assert refused.remedy == ownership_remedy()
    assert names_in(key_dir) == []


@UNREADABLE_AS_ROOT
async def test_a_key_directory_the_process_cannot_enter_is_refused_as_an_ownership_problem(
    generated: Settings, database: Database, key_dir: Path
) -> None:
    restrict(key_dir, NO_ACCESS)
    try:
        refused = await refusal_of(generated, database)
    finally:
        restrict(key_dir, OWNER_ONLY_DIRECTORY)

    assert refused.problem.startswith(f"The key directory {key_dir} must be owned by uid")
    assert refused.remedy == ownership_remedy()


async def test_the_data_directory_as_the_key_directory_is_refused(
    generated: Settings, database: Database, key_dir: Path
) -> None:
    overlapping = generated.model_copy(update={"data_dir": key_dir})

    refused = await refusal_of(overlapping, database)

    assert refused.problem == (
        f"The key directory {key_dir} is the same directory as the data directory {key_dir}"
    )
    assert (
        refused.remedy == f"Give {key_dir} its own volume, for example -v <volume-name>:{key_dir}"
    )
    assert names_in(key_dir) == []


async def test_a_key_directory_inside_the_data_directory_is_refused(
    generated: Settings, database: Database, tmp_path: Path
) -> None:
    data_dir = tmp_path / "volume"
    inside = data_dir / "nested" / "key"
    inside.mkdir(parents=True, mode=OWNER_ONLY_DIRECTORY)
    overlapping = generated.model_copy(update={"data_dir": data_dir, "secret_key_dir": inside})

    refused = await refusal_of(overlapping, database)

    assert refused.problem == (
        f"The key directory {inside} is inside the data directory {data_dir}"
    )
    assert refused.remedy == f"Give {inside} its own volume, for example -v <volume-name>:{inside}"
    assert list(inside.iterdir()) == []


async def test_a_previous_key_without_a_key_file_is_refused(
    env: dict[str, str], key_dir: Path, key_file: Path, database: Database, mounted: None
) -> None:
    settings = generated_settings(env, key_dir, **{PREVIOUS_SECRET_KEY_VAR: encoded(SECOND_KEY)})

    refused = await refusal_of(settings, database)

    assert refused.problem == (
        f"The previous key, from the configured {PREVIOUS_SECRET_KEY_VAR}, is set but "
        f"{key_file} does not exist"
    )
    assert refused.remedy == MISSING_KEY_REMEDY.format(directory=key_dir)
    assert not present(key_file)


async def test_a_previous_key_equal_to_the_key_file_is_refused(
    env: dict[str, str], key_dir: Path, key_file: Path, database: Database, mounted: None
) -> None:
    write_key_file(key_file, encoded(FIRST_KEY).encode("ascii"))
    settings = generated_settings(env, key_dir, **{PREVIOUS_SECRET_KEY_VAR: encoded(FIRST_KEY)})

    refused = await refusal_of(settings, database)

    assert refused.problem == (
        f"The previous key, from the configured {PREVIOUS_SECRET_KEY_VAR}, must differ from "
        f"the key in {key_file}"
    )
    assert refused.remedy == (
        f"Remove {PREVIOUS_SECRET_KEY_VAR}, or set it to the key the stored data was sealed with "
        "before the current one"
    )


async def establish_a_vault(database: Database) -> None:
    await start(database, THIRD_KEY)


async def plant_a_lone_crypto_row(database: Database) -> None:
    execute_sql(
        database,
        "INSERT INTO setting (key, value_type, value, updated_at) "
        "VALUES ('crypto.canary', 'string', 'x', 't')",
    )


async def store_a_secret_value(database: Database) -> None:
    async with database.session() as session:
        await seed_row(session, "registry_credential")


@pytest.mark.parametrize(
    "leave_data",
    [
        pytest.param(establish_a_vault, id="vault-established"),
        pytest.param(plant_a_lone_crypto_row, id="lone-crypto-row"),
        pytest.param(store_a_secret_value, id="secret-value-without-state"),
    ],
)
async def test_a_database_that_holds_encrypted_data_never_gets_a_new_key(
    generated: Settings,
    database: Database,
    key_dir: Path,
    leave_data: Callable[[Database], Awaitable[None]],
) -> None:
    await leave_data(database)

    refused = await refusal_of(generated, database)

    assert refused.problem == (
        f"No encryption key is configured and {key_dir / MANAGED_KEY_FILENAME} does not exist, "
        "but the database already holds encrypted data"
    )
    assert refused.remedy == MISSING_KEY_REMEDY.format(directory=key_dir)
    assert names_in(key_dir) == []


@pytest.mark.parametrize("kept", ["previous", "rejected"])
async def test_a_directory_kept_by_an_earlier_restore_blocks_generation(
    generated: Settings, database: Database, key_dir: Path, kept: str
) -> None:
    leftover = generated.data_dir / "restore" / RESTORE_ID / kept
    leftover.mkdir(parents=True)

    refused = await refusal_of(generated, database)

    assert refused.problem == (
        f"No encryption key is configured and {key_dir / MANAGED_KEY_FILENAME} does not exist, "
        f"but {leftover} holds data kept by an earlier restore"
    )
    assert refused.remedy == (
        MISSING_KEY_REMEDY.format(directory=key_dir)
        + f"; if that data is no longer needed, remove {leftover}"
    )
    assert names_in(key_dir) == []


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"%%%not-base64%%%", id="not-base64"),
        pytest.param(base64.b64encode(bytes(KEY_BYTES - 1)), id="short"),
        pytest.param(base64.b64encode(bytes(KEY_BYTES + 1)), id="long"),
        pytest.param(b"A" * 2048, id="oversized"),
        pytest.param(b"\xff\xfe\x00", id="not-text"),
        pytest.param(b"", id="empty"),
    ],
)
async def test_a_key_file_with_unusable_content_is_refused_and_left_as_it_was(
    generated: Settings, database: Database, key_file: Path, content: bytes
) -> None:
    write_key_file(key_file, content)

    refused = await refusal_of(generated, database)

    assert refused.problem == f"The key file {key_file} does not hold base64 of 32 bytes"
    assert refused.remedy == UNUSABLE_FILE_REMEDY
    assert bytes_of(key_file) == content


async def test_a_key_file_that_is_a_link_is_refused_even_when_it_points_at_a_valid_key(
    generated: Settings, database: Database, key_file: Path, tmp_path: Path
) -> None:
    target = tmp_path / "copy.key"
    write_key_file(target, encoded(FIRST_KEY).encode("ascii"))
    link_to(key_file, target)

    refused = await refusal_of(generated, database)

    assert refused.problem == f"The key file {key_file} is a symbolic link"
    assert refused.remedy == UNUSABLE_FILE_REMEDY
    assert is_link(key_file)


async def test_a_key_file_that_is_a_dangling_link_is_refused_and_nothing_is_written_through_it(
    generated: Settings, database: Database, key_file: Path, tmp_path: Path
) -> None:
    target = tmp_path / "absent.key"
    link_to(key_file, target)

    refused = await refusal_of(generated, database)

    assert refused.problem == f"The key file {key_file} is a symbolic link"
    assert not target.exists()


async def test_a_key_file_that_is_a_directory_is_refused(
    generated: Settings, database: Database, key_file: Path
) -> None:
    make_directory(key_file)

    refused = await refusal_of(generated, database)

    assert refused.problem == f"The key file {key_file} is not a regular file"
    assert refused.remedy == UNUSABLE_FILE_REMEDY


async def test_a_key_file_that_is_a_fifo_is_refused_without_blocking(
    generated: Settings, database: Database, key_file: Path
) -> None:
    os.mkfifo(key_file)

    async with asyncio.timeout(FIFO_SECONDS):
        refused = await refusal_of(generated, database)

    assert refused.problem == f"The key file {key_file} is not a regular file"


@UNREADABLE_AS_ROOT
async def test_a_key_file_the_process_cannot_read_is_refused(
    generated: Settings, database: Database, key_file: Path
) -> None:
    write_key_file(key_file, encoded(FIRST_KEY).encode("ascii"), NO_ACCESS)

    refused = await refusal_of(generated, database)

    assert refused.problem == f"The key file {key_file} cannot be read"
    assert refused.remedy == UNUSABLE_FILE_REMEDY


async def test_a_leftover_temporary_file_is_never_used_as_the_key_and_is_removed(
    generated: Settings, database: Database, key_dir: Path
) -> None:
    leftover = key_dir / LEFTOVER_TEMPORARY
    write_key_file(leftover, encoded(FIRST_KEY).encode("ascii") + b"\n")

    keys = await resolve_master_keys(
        generated, database, restore_leftover=restore_leftover, scrubber=Scrubber()
    )

    assert keys.current != FIRST_KEY
    assert names_in(key_dir) == [MANAGED_KEY_FILENAME]


async def test_a_start_that_loses_the_race_to_create_the_file_uses_the_winners_key(
    generated: Settings, database: Database, key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link

    def link_after_a_winner(*args: Any, **kwargs: Any) -> None:
        plant_key_file(kwargs["dst_dir_fd"], SECOND_KEY)
        real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", link_after_a_winner)

    keys = await resolve_master_keys(
        generated, database, restore_leftover=restore_leftover, scrubber=Scrubber()
    )

    assert keys.current == SECOND_KEY
    assert names_in(key_dir) == [MANAGED_KEY_FILENAME]
    assert (key_dir / MANAGED_KEY_FILENAME).read_bytes() == base64.b64encode(SECOND_KEY) + b"\n"


async def test_a_link_failure_never_removes_a_key_file_another_start_created(
    generated: Settings, database: Database, key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def link_denied_after_a_winner(*args: Any, **kwargs: Any) -> None:
        plant_key_file(kwargs["dst_dir_fd"], SECOND_KEY)
        raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

    monkeypatch.setattr(os, "link", link_denied_after_a_winner)

    refused = await refusal_of(generated, database)

    key_file = key_dir / MANAGED_KEY_FILENAME
    assert refused.problem == (
        f"The key file {key_file} could not be used: {os.strerror(errno.EPERM)}"
    )
    assert refused.remedy == f"Check the permissions and free space of {key_dir}"
    assert bytes_of(key_file) == base64.b64encode(SECOND_KEY) + b"\n"
    assert names_in(key_dir) == [MANAGED_KEY_FILENAME]


async def test_a_failure_after_the_file_appears_removes_that_file_and_leaves_nothing_behind(
    generated: Settings, database: Database, key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    calls: list[int] = []

    def fail_on_the_directory(descriptor: int) -> None:
        calls.append(descriptor)
        if len(calls) == 2:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_on_the_directory)

    refused = await refusal_of(generated, database)

    assert refused.problem == (
        f"The key file {key_dir / MANAGED_KEY_FILENAME} could not be used: {os.strerror(errno.EIO)}"
    )
    assert names_in(key_dir) == []


def mountinfo_line(mount_path: str, filesystem: str, *, optional: str = "shared:1") -> str:
    fields = f"36 35 98:0 / {mount_path} rw,relatime {optional}".split()
    return " ".join([*fields, "-", filesystem, "/dev/source", "rw"])


@pytest.mark.parametrize(
    ("mountinfo", "path", "expected"),
    [
        pytest.param(mountinfo_line("/key", "ext4"), "/key", "ext4", id="one-mount"),
        pytest.param(
            mountinfo_line("/key", "tmpfs", optional=""), "/key", "tmpfs", id="no-options"
        ),
        pytest.param(
            mountinfo_line("/key", "xfs", optional="shared:1 master:2"), "/key", "xfs", id="many"
        ),
        pytest.param(mountinfo_line(r"/my\040key", "ext4"), "/my key", "ext4", id="space"),
        pytest.param(mountinfo_line(r"/a\011b", "ext4"), "/a\tb", "ext4", id="tab"),
        pytest.param(
            "\n".join([mountinfo_line("/key", "ext4"), mountinfo_line("/key", "tmpfs")]),
            "/key",
            "tmpfs",
            id="the-last-mount-covers-the-earlier",
        ),
        pytest.param(
            "\n".join([mountinfo_line("/key", "tmpfs"), mountinfo_line("/other", "ext4")]),
            "/key",
            "tmpfs",
            id="other-mounts-ignored",
        ),
        pytest.param(mountinfo_line("/", "ext4"), "/key", None, id="only-a-parent-is-mounted"),
        pytest.param(mountinfo_line("/key/deeper", "ext4"), "/key", None, id="only-a-child"),
        pytest.param("", "/key", None, id="empty"),
        pytest.param("not a mountinfo line", "/key", None, id="no-separator"),
        pytest.param("36 35 98:0 / /key rw -", "/key", None, id="nothing-after-the-separator"),
        pytest.param(
            "\n".join(["garbage", mountinfo_line("/key", "ext4")]),
            "/key",
            "ext4",
            id="malformed-line-before-a-good-one",
        ),
    ],
)
def test_the_filesystem_at_a_path_is_read_from_the_last_mountinfo_line_naming_it(
    mountinfo: str, path: str, expected: str | None
) -> None:
    assert filesystem_at(mountinfo, path) == expected


def test_the_filesystem_of_a_directory_is_read_from_the_mount_table_by_its_real_path(
    tmp_path: Path, key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = tmp_path / "mountinfo"
    table.write_text(mountinfo_line(os.path.realpath(key_dir), "tmpfs") + "\n", encoding="utf-8")
    monkeypatch.setattr(managed_key, "MOUNTINFO_FILE", table)

    assert mounted_filesystem(key_dir) == "tmpfs"


def test_the_filesystem_of_a_directory_is_unknown_when_the_mount_table_cannot_be_read(
    tmp_path: Path, key_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed_key, "MOUNTINFO_FILE", tmp_path / "absent")

    assert mounted_filesystem(key_dir) is None


def test_the_root_directory_is_a_mount_point_and_an_ordinary_directory_is_not(
    key_dir: Path,
) -> None:
    assert mount_point(Path("/")) is True
    assert mount_point(key_dir) is False
