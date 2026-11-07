"""Tests for app/services/compose_setup.py: the Compose runtime, stack file directories, targets."""

import asyncio
import json
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import app.services.compose_setup as compose_setup
from app.compose_cli.binary import ComposeBinary
from app.compose_cli.cli import ComposeCli, ComposeProject
from app.compose_cli.errors import (
    ComposeBinaryInvalid,
    ComposeUnavailable,
    ComposeVersionUnsupported,
)
from app.compose_cli.workdir import write_private
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.environment import EnvironmentRepository
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.services.compose_setup import (
    COMPOSE_FILE_NAME,
    DETECTION_RETRY_SECONDS,
    STACKS_DIRECTORY,
    ComposeRuntime,
    StackFiles,
    build_target,
)
from app.services.object_inspector import EnvironmentUnavailable
from app.services.stack_deploy import StackRejected
from app.services.stack_document import CompanionFile
from tests.access_support import ENV_A, ENV_B, REMOTE_URL, SOCKET_PATH
from tests.auth_support import FakeClock
from tests.crypto.helpers import SECOND_KEY, vault_with
from tests.docker_engine.tls_material import valid_material
from tests.support import present

FLOOR_TEXT = "v2.32.0"
INSTALLED = "v2.33.0"
REPLACEMENT = "v2.32.1"
BELOW_FLOOR = "v2.29.9"
TIMEOUT_SECONDS = 30.0
TOKEN = "0123456789abcdef0123456789abcdef"
OTHER_TOKEN = "fedcba9876543210fedcba9876543210"
PRIVATE_DIRECTORY = 0o700
PRIVATE_FILE = 0o600
COMPOSE_TEXT = "services:\n  web:\n    image: x\n"
POLL_ATTEMPTS = 300
POLL_SECONDS = 0.01
HOUR = 3600


class FakeCompose:
    """A trusted executable that counts its runs and prints whatever answer file it is given."""

    def __init__(self, directory: Path, version: str = INSTALLED) -> None:
        directory.mkdir(mode=PRIVATE_DIRECTORY)
        self.binary = directory / "docker-compose"
        self._runs = directory / "runs"
        self._answer = directory / "answer"
        self.binary.write_text(
            f"#!/bin/sh\necho run >> '{self._runs}'\ncat '{self._answer}'\n", encoding="utf-8"
        )
        self.binary.chmod(0o755)
        self.say_version(version)

    def say_version(self, version: str) -> None:
        self._answer.write_text(json.dumps({"version": version}) + "\n", encoding="utf-8")

    def say_garbage(self) -> None:
        self._answer.write_text("not json\n", encoding="utf-8")

    @property
    def runs(self) -> int:
        return (
            len(self._runs.read_text(encoding="utf-8").splitlines()) if self._runs.exists() else 0
        )


class StepClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(POLL_ATTEMPTS):
        if condition():
            return
        await asyncio.sleep(POLL_SECONDS)
    raise AssertionError("the condition did not become true")


def runtime_for(
    tmp_path: Path,
    compose: FakeCompose,
    clock: Callable[[], float] = time.monotonic,
    versions: list[str | None] | None = None,
) -> ComposeRuntime:
    return ComposeRuntime(
        data_dir=tmp_path / "data",
        configured=compose.binary,
        timeout_seconds=TIMEOUT_SECONDS,
        clock=clock,
        on_version=None if versions is None else versions.append,
    )


@pytest.fixture
def compose(tmp_path: Path) -> FakeCompose:
    return FakeCompose(tmp_path / "bin")


class TestComposeRuntimeDetection:
    async def test_ready_returns_the_cli_and_status_never_spawns_after_detection(
        self, tmp_path: Path, compose: FakeCompose
    ) -> None:
        versions: list[str | None] = []
        runtime = runtime_for(tmp_path, compose, versions=versions)

        await runtime.detect()
        cli = await runtime.ready()
        statuses = [runtime.status() for _ in range(3)]
        await asyncio.sleep(0)

        assert isinstance(cli, ComposeCli)
        assert compose.runs == 1
        assert versions == [INSTALLED]
        assert {s.ready for s in statuses} == {True}
        assert statuses[0].installed == INSTALLED
        assert statuses[0].required == FLOOR_TEXT
        assert statuses[0].message is None
        assert runtime.supported
        assert runtime.version_text == INSTALLED

    async def test_use_binary_forgets_the_version_and_the_callback_follows_the_runtime(
        self, tmp_path: Path, compose: FakeCompose
    ) -> None:
        replacement = FakeCompose(tmp_path / "replacement", REPLACEMENT)
        versions: list[str | None] = []
        runtime = runtime_for(tmp_path, compose, versions=versions)
        await runtime.detect()

        runtime.use_binary(compose_binary(replacement))
        after_swap = runtime.status()
        await runtime.ready()

        assert after_swap.ready is False
        assert after_swap.installed is None
        assert versions == [INSTALLED, None, REPLACEMENT]
        assert runtime.version_text == REPLACEMENT
        assert (compose.runs, replacement.runs) == (1, 1)

    @pytest.mark.parametrize(
        ("installed", "supported"), [(FLOOR_TEXT, True), (INSTALLED, True), (BELOW_FLOOR, False)]
    )
    async def test_the_floor_is_inclusive(
        self, tmp_path: Path, compose: FakeCompose, installed: str, supported: bool
    ) -> None:
        compose.say_version(installed)
        runtime = runtime_for(tmp_path, compose)

        await runtime.detect()

        assert runtime.supported is supported
        assert runtime.version_text == installed

    async def test_a_version_below_the_floor_is_refused_naming_both_versions(
        self, tmp_path: Path, compose: FakeCompose
    ) -> None:
        compose.say_version(BELOW_FLOOR)
        runtime = runtime_for(tmp_path, compose)

        with pytest.raises(ComposeVersionUnsupported) as raised:
            await runtime.ready()
        status = runtime.status()

        assert (raised.value.installed, raised.value.required) == (BELOW_FLOOR, FLOOR_TEXT)
        assert BELOW_FLOOR in str(raised.value)
        assert FLOOR_TEXT in str(raised.value)
        assert status.ready is False
        assert (status.installed, status.required) == (BELOW_FLOOR, FLOOR_TEXT)
        assert BELOW_FLOOR in present(status.message)
        assert FLOOR_TEXT in present(status.message)

    async def test_a_missing_binary_is_unavailable_and_nothing_is_started(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(compose_setup, "locate", lambda configured: None)
        versions: list[str | None] = []
        runtime = ComposeRuntime(
            data_dir=tmp_path / "data",
            configured=None,
            timeout_seconds=TIMEOUT_SECONDS,
            on_version=versions.append,
        )

        with pytest.raises(ComposeUnavailable):
            await runtime.ready()
        status = runtime.status()
        await asyncio.sleep(0)

        assert runtime.binary is None
        assert status.ready is False
        assert status.installed is None
        assert status.message == str(ComposeUnavailable())
        assert versions == []

    async def test_unreadable_output_leaves_compose_unavailable(
        self, tmp_path: Path, compose: FakeCompose
    ) -> None:
        compose.say_garbage()
        runtime = runtime_for(tmp_path, compose)

        with pytest.raises(ComposeUnavailable):
            await runtime.ready()

        assert runtime.version_text is None
        assert runtime.supported is False


class TestComposeRuntimeRetry:
    async def test_detection_is_retried_at_most_once_per_interval(
        self, tmp_path: Path, compose: FakeCompose
    ) -> None:
        compose.say_garbage()
        clock = StepClock()
        runtime = runtime_for(tmp_path, compose, clock)
        with pytest.raises(ComposeUnavailable):
            await runtime.ready()
        compose.say_version(INSTALLED)

        with pytest.raises(ComposeUnavailable):
            await runtime.ready()
        clock.now += DETECTION_RETRY_SECONDS - 0.1
        with pytest.raises(ComposeUnavailable):
            await runtime.ready()
        runs_before = compose.runs
        clock.now += 0.1
        cli = await runtime.ready()

        assert runs_before == 1
        assert compose.runs == 2
        assert isinstance(cli, ComposeCli)

    async def test_status_starts_one_background_retry_per_interval(
        self, tmp_path: Path, compose: FakeCompose
    ) -> None:
        compose.say_garbage()
        clock = StepClock()
        runtime = runtime_for(tmp_path, compose, clock)
        with pytest.raises(ComposeUnavailable):
            await runtime.ready()
        compose.say_version(INSTALLED)
        before_interval = runtime.status()
        clock.now += DETECTION_RETRY_SECONDS

        first = runtime.status()
        second = runtime.status()
        await until(lambda: runtime.supported)

        assert before_interval.ready is False
        assert (first.ready, second.ready) == (False, False)
        assert compose.runs == 2
        assert runtime.status().ready is True
        assert compose.runs == 2

    async def test_aclose_cancels_a_pending_retry(
        self, tmp_path: Path, compose: FakeCompose
    ) -> None:
        compose.say_garbage()
        clock = StepClock()
        runtime = runtime_for(tmp_path, compose, clock)
        with pytest.raises(ComposeUnavailable):
            await runtime.ready()
        clock.now += DETECTION_RETRY_SECONDS
        runtime.status()

        await runtime.aclose()
        await asyncio.sleep(POLL_SECONDS * 5)

        assert compose.runs == 1
        assert runtime.version_text is None

    async def test_aclose_without_a_retry_does_nothing(
        self, tmp_path: Path, compose: FakeCompose
    ) -> None:
        runtime = runtime_for(tmp_path, compose)

        await runtime.aclose()

        assert compose.runs == 0


def compose_binary(compose: FakeCompose) -> ComposeBinary:
    return ComposeBinary((str(compose.binary),))


class TestConfiguredBinary:
    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(lambda root: root / "absent", id="missing"),
            pytest.param(lambda root: Path("docker-compose"), id="relative"),
            pytest.param(lambda root: not_executable(root), id="not executable"),
            pytest.param(lambda root: in_open_directory(root), id="directory writable by others"),
        ],
    )
    def test_a_configured_path_that_is_not_a_trusted_executable_is_refused(
        self, tmp_path: Path, make: Callable[[Path], Path]
    ) -> None:
        configured = make(tmp_path)

        with pytest.raises(ComposeBinaryInvalid):
            ComposeRuntime(
                data_dir=tmp_path / "data", configured=configured, timeout_seconds=TIMEOUT_SECONDS
            )

    def test_a_trusted_executable_is_accepted_without_running_it(
        self, tmp_path: Path, compose: FakeCompose
    ) -> None:
        runtime = runtime_for(tmp_path, compose)

        assert runtime.binary == compose_binary(compose)
        assert compose.runs == 0


def not_executable(root: Path) -> Path:
    directory = root / "plain"
    directory.mkdir(mode=PRIVATE_DIRECTORY)
    path = directory / "docker-compose"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(PRIVATE_FILE)
    return path


def in_open_directory(root: Path) -> Path:
    directory = root / "open"
    directory.mkdir()
    path = directory / "docker-compose"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    directory.chmod(0o777)
    return path


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


class TestStackFilesPrepare:
    def test_it_creates_a_private_resolved_base_and_tightens_an_existing_one(
        self, tmp_path: Path
    ) -> None:
        base = tmp_path / STACKS_DIRECTORY
        base.mkdir()
        base.chmod(0o777)

        files = StackFiles.prepare(tmp_path)

        assert files.base == base.resolve()
        assert mode_of(base) == PRIVATE_DIRECTORY

    def test_it_creates_missing_parents(self, tmp_path: Path) -> None:
        files = StackFiles.prepare(tmp_path / "a" / "b")

        assert files.base.is_dir()

    def test_it_clears_everything_but_the_compose_file_from_every_project_directory(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / STACKS_DIRECTORY / TOKEN
        directory.mkdir(parents=True)
        (directory / COMPOSE_FILE_NAME).write_text(COMPOSE_TEXT, encoding="utf-8")
        (directory / "s.env").write_text("SECRET", encoding="utf-8")
        (directory / "link").symlink_to(tmp_path)
        (directory / "dir").mkdir()
        (directory / "dir" / "inner").write_text("x", encoding="utf-8")

        StackFiles.prepare(tmp_path)

        assert names_of(directory) == [COMPOSE_FILE_NAME]
        assert (directory / COMPOSE_FILE_NAME).read_text(encoding="utf-8") == COMPOSE_TEXT
        assert tmp_path.exists()

    def test_a_symlinked_base_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere"
        target.mkdir()
        (tmp_path / STACKS_DIRECTORY).symlink_to(target)

        with pytest.raises(ValueError, match="symlink"):
            StackFiles.prepare(tmp_path)


class TestStackFilesProject:
    @pytest.fixture
    def files(self, tmp_path: Path) -> StackFiles:
        return StackFiles.prepare(tmp_path)

    def test_a_token_names_a_directory_and_file_under_the_base(self, files: StackFiles) -> None:
        project = files.project("shop", TOKEN)

        assert project == ComposeProject(
            "shop", files.base / TOKEN, files.base / TOKEN / COMPOSE_FILE_NAME
        )

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "0" * 31,
            "0" * 33,
            TOKEN.upper(),
            "g" * 32,
            "../" + "0" * 29,
            "0" * 31 + "/",
            "0" * 31 + "\n",
            "\N{ARABIC-INDIC DIGIT ONE}" * 32,
        ],
    )
    def test_anything_but_32_lower_case_hex_digits_is_refused(
        self, files: StackFiles, token: str
    ) -> None:
        with pytest.raises(ValueError, match="hex"):
            files.project("shop", token)

    def test_new_tokens_are_valid_and_distinct(self, files: StackFiles) -> None:
        tokens = {StackFiles.new_token() for _ in range(5)}

        assert len(tokens) == 5
        assert [files.project("shop", token).directory.parent for token in tokens] == [
            files.base
        ] * 5


async def write(files: StackFiles, project: ComposeProject, text: str = COMPOSE_TEXT) -> None:
    async with files.materialise(project, text):
        pass


def companion(name: str, content: str = "A=1\n", *, secret: bool = False) -> CompanionFile:
    return CompanionFile(name, content, secret)


ENV_USE = "services:\n  web:\n    image: x\n    env_file: [{names}]\n"


def names_of(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


class TestStackFilesMaterialise:
    @pytest.fixture
    def files(self, tmp_path: Path) -> StackFiles:
        return StackFiles.prepare(tmp_path)

    async def test_it_writes_a_private_file_in_a_private_directory(self, files: StackFiles) -> None:
        project = files.project("shop", TOKEN)

        await write(files, project)

        assert project.file.read_text(encoding="utf-8") == COMPOSE_TEXT
        assert mode_of(project.file) == PRIVATE_FILE
        assert mode_of(project.directory) == PRIVATE_DIRECTORY
        assert names_of(project.directory) == [COMPOSE_FILE_NAME]

    async def test_it_replaces_the_file_and_leaves_no_temporary_entry(
        self, files: StackFiles
    ) -> None:
        project = files.project("shop", TOKEN)
        await write(files, project)

        await write(files, project, "services: {}\n")

        assert project.file.read_text(encoding="utf-8") == "services: {}\n"
        assert names_of(project.directory) == [COMPOSE_FILE_NAME]

    async def test_a_failed_write_keeps_the_old_file_and_removes_its_temporary(
        self, files: StackFiles, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = files.project("shop", TOKEN)
        await write(files, project)

        def failing(path: Path, data: bytes) -> None:
            path.write_bytes(data[:3])
            raise OSError("disk full")

        monkeypatch.setattr(compose_setup, "write_private", failing)

        with pytest.raises(OSError, match="disk full"):
            await write(files, project, "services: {}\n")

        assert project.file.read_text(encoding="utf-8") == COMPOSE_TEXT
        assert names_of(project.directory) == [COMPOSE_FILE_NAME]

    async def test_a_symlinked_project_directory_is_refused_and_its_target_untouched(
        self, files: StackFiles, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        project = files.project("shop", TOKEN)
        project.directory.symlink_to(outside)

        with pytest.raises(OSError, match="symlink"):
            await write(files, project)

        assert list(outside.iterdir()) == []

    async def test_a_text_the_pre_check_refuses_writes_nothing(self, files: StackFiles) -> None:
        project = files.project("shop", TOKEN)

        with pytest.raises(StackRejected) as raised:
            await write(files, project, COMPOSE_TEXT + "    env_file: /etc/passwd\n")

        assert raised.value.reason == "refused_construct"
        assert not project.directory.exists()

    async def test_a_missing_companion_is_refused_before_anything_is_written(
        self, files: StackFiles
    ) -> None:
        project = files.project("shop", TOKEN)

        with pytest.raises(StackRejected) as raised:
            async with files.materialise(project, ENV_USE.format(names="a.env")):
                pytest.fail("the body must not run")

        assert raised.value.reason == "file_missing"
        assert not project.directory.exists()

    async def test_env_and_compose_files_are_written_and_unused_ones_are_not(
        self, files: StackFiles
    ) -> None:
        project = files.project("shop", TOKEN)
        text = "include: [inc.yaml]\n" + ENV_USE.format(names="a.env, b.env")
        companions = [
            companion("a.env", "A=1\n"),
            companion("b.env", "B=2\n", secret=True),
            companion("inc.yaml", "services: {}\n"),
            companion("unused.txt"),
        ]

        async with files.materialise(project, text, companions) as materialised:
            assert names_of(project.directory) == ["a.env", "b.env", COMPOSE_FILE_NAME, "inc.yaml"]
            assert (project.directory / "b.env").read_text(encoding="utf-8") == "B=2\n"
            assert mode_of(project.directory / "b.env") == PRIVATE_FILE
            assert sorted(materialised.uses) == ["a.env", "b.env", "inc.yaml"]

        assert names_of(project.directory) == ["a.env", COMPOSE_FILE_NAME, "inc.yaml"]

    async def test_secret_files_are_removed_when_the_body_fails(self, files: StackFiles) -> None:
        project = files.project("shop", TOKEN)
        text = ENV_USE.format(names="s.env")

        with pytest.raises(RuntimeError, match="boom"):
            async with files.materialise(project, text, [companion("s.env", secret=True)]):
                raise RuntimeError("boom")

        assert names_of(project.directory) == [COMPOSE_FILE_NAME]

    async def test_secret_files_are_removed_when_the_body_is_cancelled(
        self, files: StackFiles
    ) -> None:
        project = files.project("shop", TOKEN)
        text = ENV_USE.format(names="s.env")
        entered = asyncio.Event()

        async def body() -> None:
            async with files.materialise(project, text, [companion("s.env", secret=True)]):
                entered.set()
                await asyncio.sleep(HOUR)

        task = asyncio.ensure_future(body())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert names_of(project.directory) == [COMPOSE_FILE_NAME]

    async def test_a_failed_write_of_a_later_file_removes_the_secret_written_before_it(
        self, files: StackFiles, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = files.project("shop", TOKEN)
        text = ENV_USE.format(names="a.env, s.env")
        real = write_private

        def failing(path: Path, data: bytes) -> None:
            if b"LAST" in data:
                raise OSError("disk full")
            real(path, data)

        monkeypatch.setattr(compose_setup, "write_private", failing)

        with pytest.raises(OSError, match="disk full"):
            async with files.materialise(
                project, text, [companion("a.env", "LAST=1\n"), companion("s.env", secret=True)]
            ):
                pytest.fail("the body must not run")

        assert not (project.directory / "s.env").exists()

    async def test_a_planted_symlink_directory_and_unknown_file_are_removed_unfollowed(
        self, files: StackFiles, tmp_path: Path
    ) -> None:
        project = files.project("shop", TOKEN)
        await write(files, project)
        key = tmp_path / "key"
        key.write_text("KEY", encoding="utf-8")
        (project.directory / "a.env").symlink_to(key)
        (project.directory / "stray").symlink_to(key)
        (project.directory / "dir").mkdir()
        (project.directory / "dir" / "inner").write_text("x", encoding="utf-8")
        (project.directory / "unknown.txt").write_text("x", encoding="utf-8")

        async with files.materialise(
            project, ENV_USE.format(names="a.env"), [companion("a.env", "A=1\n")]
        ):
            assert names_of(project.directory) == ["a.env", COMPOSE_FILE_NAME]
            assert not (project.directory / "a.env").is_symlink()
            assert (project.directory / "a.env").read_text(encoding="utf-8") == "A=1\n"

        assert key.read_text(encoding="utf-8") == "KEY"

    async def test_a_symlinked_compose_file_is_replaced_and_its_target_untouched(
        self, files: StackFiles, tmp_path: Path
    ) -> None:
        project = files.project("shop", TOKEN)
        project.directory.mkdir(mode=PRIVATE_DIRECTORY)
        target = tmp_path / "target"
        target.write_text("TARGET", encoding="utf-8")
        project.file.symlink_to(target)

        await write(files, project)

        assert not project.file.is_symlink()
        assert project.file.read_text(encoding="utf-8") == COMPOSE_TEXT
        assert target.read_text(encoding="utf-8") == "TARGET"

    async def test_a_leftover_secret_file_is_replaced_not_reused(self, files: StackFiles) -> None:
        project = files.project("shop", TOKEN)
        project.directory.mkdir(mode=PRIVATE_DIRECTORY)
        (project.directory / "s.env").write_text("OLD", encoding="utf-8")

        async with files.materialise(
            project, ENV_USE.format(names="s.env"), [companion("s.env", "NEW\n", secret=True)]
        ):
            assert (project.directory / "s.env").read_text(encoding="utf-8") == "NEW\n"

    async def test_a_name_outside_the_grammar_is_never_used_as_a_path(
        self, files: StackFiles
    ) -> None:
        project = files.project("shop", TOKEN)

        with pytest.raises(StackRejected):
            await write_with(files, project, [companion("../escape")])

        assert not project.directory.exists()


async def write_with(
    files: StackFiles, project: ComposeProject, companions: list[CompanionFile]
) -> None:
    async with files.materialise(project, COMPOSE_TEXT, companions):
        pass


class TestStackFilesScratch:
    @pytest.fixture
    def files(self, tmp_path: Path) -> StackFiles:
        return StackFiles.prepare(tmp_path)

    async def test_it_holds_the_candidate_and_every_file_it_reads_in_a_throwaway_directory(
        self, files: StackFiles
    ) -> None:
        companions = [companion("a.env"), companion("s.env", "S=1\n", secret=True)]
        text = ENV_USE.format(names="a.env, s.env")

        async with files.scratch("shop", text, companions) as project:
            directory = project.directory
            assert directory.parent == files.base
            assert project.file.read_text(encoding="utf-8") == text
            assert names_of(directory) == ["a.env", COMPOSE_FILE_NAME, "s.env"]

        assert not directory.exists()

    async def test_it_never_uses_a_stacks_own_directory(self, files: StackFiles) -> None:
        own = files.project("shop", TOKEN)
        await write(files, own)

        async with files.scratch("shop", "services: {}\n") as project:
            assert project.directory != own.directory

        assert own.file.read_text(encoding="utf-8") == COMPOSE_TEXT
        assert names_of(files.base) == [TOKEN]

    async def test_it_removes_the_directory_when_the_body_fails(self, files: StackFiles) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            async with files.scratch("shop", COMPOSE_TEXT, [companion("s.env", secret=True)]):
                raise RuntimeError("boom")

        assert list(files.base.iterdir()) == []

    async def test_a_file_set_that_breaks_the_rules_is_refused(self, files: StackFiles) -> None:
        with pytest.raises(StackRejected):
            async with files.scratch("shop", COMPOSE_TEXT, [companion("compose.yaml")]):
                pytest.fail("the body must not run")

        assert list(files.base.iterdir()) == []


class TestStackFilesDiscardAndSweep:
    @pytest.fixture
    def files(self, tmp_path: Path) -> StackFiles:
        return StackFiles.prepare(tmp_path)

    async def test_discard_removes_the_directory_and_tolerates_a_missing_one(
        self, files: StackFiles
    ) -> None:
        project = files.project("shop", TOKEN)
        await write(files, project)

        removed = await files.discard(TOKEN)
        again = await files.discard(TOKEN)

        assert (removed, again) == (True, True)
        assert not project.directory.exists()

    async def test_discard_unlinks_a_symlink_without_following_it(
        self, files: StackFiles, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep").write_text("x", encoding="utf-8")
        files.project("shop", TOKEN).directory.symlink_to(outside)

        removed = await files.discard(TOKEN)

        assert removed is True
        assert not (files.base / TOKEN).exists()
        assert (outside / "keep").read_text(encoding="utf-8") == "x"

    async def test_discard_refuses_a_token_that_is_not_hex(self, files: StackFiles) -> None:
        with pytest.raises(ValueError, match="hex"):
            await files.discard("../outside")

    async def test_sweep_removes_unknown_directories_and_keeps_known_ones(
        self, files: StackFiles
    ) -> None:
        for token in (TOKEN, OTHER_TOKEN):
            await write(files, files.project("shop", token))

        removed = await files.sweep({TOKEN})

        assert removed == 1
        assert [entry.name for entry in files.base.iterdir()] == [TOKEN]

    async def test_sweep_keeps_an_entry_younger_than_the_minimum_age(
        self, files: StackFiles
    ) -> None:
        old, young = files.base / OTHER_TOKEN, files.base / ("a" * 32)
        for directory in (old, young):
            directory.mkdir()
        two_hours_ago = time.time() - 2 * HOUR
        os.utime(old, (two_hours_ago, two_hours_ago))

        removed = await files.sweep(set(), min_age_seconds=HOUR)

        assert removed == 1
        assert [entry.name for entry in files.base.iterdir()] == [young.name]

    async def test_sweep_without_a_minimum_age_removes_every_unknown_entry(
        self, files: StackFiles
    ) -> None:
        (files.base / TOKEN).mkdir()

        removed = await files.sweep(set())

        assert removed == 1
        assert list(files.base.iterdir()) == []


class TestBuildTarget:
    async def test_a_remote_environment_gets_its_endpoint_and_decrypted_material(
        self, db: Session, vault: Vault, clock: FakeClock
    ) -> None:
        material = valid_material()
        tls = EnvironmentTlsRepository(db, vault, clock)
        await tls.replace(
            ENV_A,
            ca_certificate=material.ca,
            client_certificate=material.cert,
            client_key=material.key,
        )
        environment = present(await EnvironmentRepository(db, clock).get(ENV_A))

        target = await build_target(environment, tls)

        assert target.docker_host == REMOTE_URL
        assert target.tls is not None
        assert target.tls.key_pem == material.key
        assert target.tls.ca_pem == material.ca
        assert target.tls.cert_pem == material.cert

    async def test_a_local_environment_needs_no_material(
        self, db: Session, vault: Vault, clock: FakeClock
    ) -> None:
        tls = EnvironmentTlsRepository(db, vault, clock)
        environment = present(await EnvironmentRepository(db, clock).get(ENV_B))

        target = await build_target(environment, tls)

        assert target.docker_host == f"unix://{SOCKET_PATH}"
        assert target.tls is None

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(None, id="no material stored"),
            pytest.param("ca_certificate = NULL", id="no CA"),
            pytest.param("ca_certificate = ''", id="empty CA"),
            pytest.param("client_certificate = NULL", id="no certificate"),
            pytest.param("client_key_ciphertext = NULL", id="no key"),
            pytest.param("client_key_ciphertext = 'v1:AAAA:BBBB'", id="unreadable key"),
        ],
    )
    async def test_incomplete_or_unreadable_material_is_unavailable_credentials(
        self, db: Session, vault: Vault, clock: FakeClock, damage: str | None
    ) -> None:
        material = valid_material()
        tls = EnvironmentTlsRepository(db, vault, clock)
        if damage is not None:
            await tls.replace(
                ENV_A,
                ca_certificate=material.ca,
                client_certificate=material.cert,
                client_key=material.key,
            )
            await db.execute(f"UPDATE environment_tls_material SET {damage}", ())
        environment = present(await EnvironmentRepository(db, clock).get(ENV_A))

        with pytest.raises(EnvironmentUnavailable) as raised:
            await build_target(environment, tls)

        assert raised.value.kind == "credentials"

    async def test_a_key_sealed_under_another_vault_is_unavailable_credentials(
        self, db: Session, vault: Vault, clock: FakeClock
    ) -> None:
        material = valid_material()
        await EnvironmentTlsRepository(db, vault, clock).replace(
            ENV_A,
            ca_certificate=material.ca,
            client_certificate=material.cert,
            client_key=material.key,
        )
        other = EnvironmentTlsRepository(db, vault_with(SECOND_KEY), clock)
        environment = present(await EnvironmentRepository(db, clock).get(ENV_A))

        with pytest.raises(EnvironmentUnavailable) as raised:
            await build_target(environment, other)

        assert raised.value.kind == "credentials"
