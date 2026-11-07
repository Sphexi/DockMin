"""Tests for app/compose_cli/cli.py: argv construction, environment, slots and cleanup."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.compose_cli.binary import ComposeBinary
from app.compose_cli.cli import (
    ComposeCli,
    ComposeProject,
    build_argv,
    down_tail,
    up_tail,
)
from app.compose_cli.errors import ComposeBusy, ComposeUnavailable
from app.compose_cli.parsers import ProgressEvent
from app.compose_cli.sources import SOURCES_FILE, ComposeSource
from app.compose_cli.target import ComposeTarget, TargetMaterial
from app.compose_cli.workdir import EMPTY_ENV_FILE
from app.logging import Scrubber

Records = Callable[[], list[dict[str, Any]]]

LOCAL = ComposeTarget("unix:///var/run/docker.sock", None)
TLS = ComposeTarget("tcp://127.0.0.1:2376", TargetMaterial("CA\n", "CERT\n", "KEY\n"))
TLS_FILES = {"ca.pem": "0o600", "cert.pem": "0o600", "key.pem": "0o600"}
WORKDIR_ENTRIES = {
    "config": "0o700",
    "empty.env": "0o600",
    "home": "0o700",
    "tls": "0o700",
    "tmp": "0o700",
}
CONSTRUCTED_KEYS = {
    "PATH",
    "HOME",
    "DOCKER_CONFIG",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TZ",
    "NO_COLOR",
    "DOCKER_CLI_HINTS",
    "COMPOSE_MENU",
}
TLS_KEYS = {"DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}
SERVER_SENTINEL = "must-not-reach-compose"
HOSTILE_DIRECTORY = "p;$(touch canary-dir)`touch canary-dir`'\"|&>\nz"
INVALID_NAMES = [
    "",
    "-x",
    "_x",
    "Shop",
    "a b",
    "a;b",
    "a/b",
    "a.b",
    "$(touch x)",
    "`id`",
    "a\nb",
    "x" * 64,
]
PROJECT_DIRECTORY = Path("/srv/project")
EMPTY_ENV = Path("/srv/work/empty.env")


def is_empty(directory: Path) -> bool:
    return not any(directory.iterdir())


def hostile_value(canary: Path) -> str:
    return f"; $(touch {canary}) `touch {canary}` ' \" | & > {canary}\n# \\ ☃"


def expected_argv(
    project: ComposeProject, work_dir: str, *tail: str, profile: bool = False
) -> list[str]:
    return [
        "--progress",
        "json",
        "--ansi",
        "never",
        "--project-name",
        project.name,
        "--project-directory",
        str(project.directory),
        "--file",
        str(project.file),
        "--env-file",
        str(Path(work_dir) / EMPTY_ENV_FILE),
        *(["--profile", "*"] if profile else []),
        *tail,
    ]


def test_build_argv_orders_the_global_flags_then_the_command() -> None:
    project = ComposeProject("stack-a", PROJECT_DIRECTORY, PROJECT_DIRECTORY / "compose.yaml")

    argv = build_argv(project, EMPTY_ENV, ("config", "--format", "json"), all_profiles=True)

    assert argv == (
        "--progress",
        "json",
        "--ansi",
        "never",
        "--project-name",
        "stack-a",
        "--project-directory",
        "/srv/project",
        "--file",
        "/srv/project/compose.yaml",
        "--env-file",
        "/srv/work/empty.env",
        "--profile",
        "*",
        "config",
        "--format",
        "json",
    )


def test_build_argv_adds_no_profile_flag_unless_all_profiles_is_requested() -> None:
    project = ComposeProject("stack-a", PROJECT_DIRECTORY, PROJECT_DIRECTORY / "compose.yaml")

    assert "--profile" not in build_argv(project, EMPTY_ENV, ("ps",))


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (up_tail(remove_orphans=False), ("up", "--detach", "--pull", "never", "--no-build")),
        (
            up_tail(remove_orphans=True),
            ("up", "--detach", "--pull", "never", "--no-build", "--remove-orphans"),
        ),
        (down_tail(remove_orphans=False), ("down",)),
        (down_tail(remove_orphans=True), ("down", "--remove-orphans")),
    ],
)
def test_up_and_down_carry_only_the_documented_flags(
    tail: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    assert tail == expected
    assert not {"--rmi", "--volumes", "-v", "--build"} & set(tail)


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_a_project_name_outside_the_grammar_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="grammar"):
        ComposeProject(name, PROJECT_DIRECTORY, PROJECT_DIRECTORY / "compose.yaml")


@pytest.mark.parametrize("name", ["a", "stack-a", "a--b_-c", "0abc", "x" * 63])
def test_a_project_name_inside_the_grammar_is_accepted(name: str) -> None:
    assert ComposeProject(name, PROJECT_DIRECTORY, PROJECT_DIRECTORY / "compose.yaml").name == name


def test_project_paths_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ComposeProject("stack-a", Path("project"), Path("project/compose.yaml"))


@pytest.mark.usefixtures("permit_loopback")
async def test_every_command_runs_with_its_exact_argv_directory_and_environment(
    cli: ComposeCli,
    project: ComposeProject,
    scrubber: Scrubber,
    work_base: Path,
    record_path: Path,
    records: Records,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVER_SENTINEL", SERVER_SENTINEL)
    variables = {"FAKE_RECORD": str(record_path)}
    seen: list[ProgressEvent] = []

    version = await cli.version()
    version_leftovers = is_empty(work_base)
    async with cli.invocation(project, TLS, variables, scrubber) as run:
        resolved = await run.config()
        containers = await run.ps()
        completed = await run.compose_up(remove_orphans=True, on_event=seen.append)
        await run.compose_up(remove_orphans=False)
        await run.compose_down(remove_orphans=False)
        await run.compose_down(remove_orphans=True)
        await run.compose_start()
        await run.compose_stop()

    config, ps, *mutations = records()
    work = config["workdir"]
    assert version_leftovers
    assert is_empty(work_base)
    assert config["argv"] == expected_argv(
        project, work, "config", "--format", "json", profile=True
    )
    assert ps["argv"] == expected_argv(project, work, "ps", "--all", "--format", "json")
    assert [m["argv"] for m in mutations] == [
        expected_argv(
            project, work, "up", "--detach", "--pull", "never", "--no-build", "--remove-orphans"
        ),
        expected_argv(project, work, "up", "--detach", "--pull", "never", "--no-build"),
        expected_argv(project, work, "down"),
        expected_argv(project, work, "down", "--remove-orphans"),
        expected_argv(project, work, "start"),
        expected_argv(project, work, "stop"),
    ]
    assert {r["cwd"] for r in [config, ps, *mutations]} == {str(project.directory)}
    assert version.text == "v5.5.1"
    assert resolved.name == "dockmin-e2e-sample"
    assert [c.service for c in containers] == ["once", "web"]
    assert len(completed.events) == len(seen) == 5
    assert seen[-1].text == "Started"
    assert "was not created by compose" in completed.stderr
    assert completed.stdout == b""


@pytest.mark.usefixtures("permit_loopback")
async def test_the_process_sees_a_constructed_environment_and_private_work_files(
    cli: ComposeCli,
    project: ComposeProject,
    scrubber: Scrubber,
    work_base: Path,
    record_path: Path,
    records: Records,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERVER_SENTINEL", SERVER_SENTINEL)
    variables = {"FAKE_RECORD": str(record_path), "STACK_VALUE": "kept"}

    async with cli.invocation(project, TLS, variables, scrubber) as run:
        await run.ps()

    (seen,) = records()
    assert is_empty(work_base)
    environment = seen["env"]
    assert set(environment) == CONSTRUCTED_KEYS | TLS_KEYS | set(variables)
    assert SERVER_SENTINEL not in environment.values()
    assert environment["STACK_VALUE"] == "kept"
    assert environment["DOCKER_HOST"] == TLS.docker_host
    assert environment["DOCKER_TLS_VERIFY"] == "1"
    assert environment["DOCKER_CERT_PATH"] == str(Path(seen["workdir"]) / "tls")
    assert seen["workdir_mode"] == "0o700"
    assert seen["entries"] == WORKDIR_ENTRIES
    assert seen["tls"] == TLS_FILES
    assert seen["env_file_size"] == 0
    assert seen["docker_config"] == "{}\n"


async def test_no_invocation_passes_through_a_shell_and_metacharacters_execute_nothing(
    fake_binary: ComposeBinary,
    work_base: Path,
    tmp_path: Path,
    scrubber: Scrubber,
    record_path: Path,
    records: Records,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = tmp_path / "canary"
    directory = tmp_path / HOSTILE_DIRECTORY
    directory.mkdir()
    compose_file = directory / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    project = ComposeProject("stack-a", directory, compose_file)
    hostile = hostile_value(canary)
    spawns: list[tuple[Any, ...]] = []
    real_spawn = asyncio.create_subprocess_exec

    async def spy(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        spawns.append(args)
        assert "shell" not in kwargs
        return await real_spawn(*args, **kwargs)

    def refuse_shell(*_: Any, **__: Any) -> None:
        raise AssertionError("a shell was requested")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse_shell)
    cli = ComposeCli(fake_binary, work_base=work_base)

    async with cli.invocation(
        project, LOCAL, {"FAKE_RECORD": str(record_path), "HOSTILE": hostile}, scrubber
    ) as run:
        await run.config()
        await run.compose_up(remove_orphans=False)

    config_spawn, up_spawn = spawns
    binary_prefix = fake_binary.argv0
    assert config_spawn[: len(binary_prefix)] == binary_prefix
    assert up_spawn[: len(binary_prefix)] == binary_prefix
    argument_list = config_spawn[len(binary_prefix) :]
    assert all(isinstance(a, str) for a in argument_list)
    assert argument_list[argument_list.index("--project-directory") + 1] == str(directory)
    assert argument_list[argument_list.index("--file") + 1] == str(compose_file)
    config_run, up_run = records()
    assert config_run["argv"] == list(argument_list)
    assert config_run["cwd"] == str(directory)
    assert config_run["env"]["HOSTILE"] == hostile
    assert up_run["env"]["HOSTILE"] == hostile
    assert not canary.exists()
    assert not (tmp_path / "canary-dir").exists()
    assert not (directory / "canary-dir").exists()


async def test_a_reserved_or_malformed_variable_is_refused_and_leaves_no_work_directory(
    cli: ComposeCli, project: ComposeProject, scrubber: Scrubber, work_base: Path
) -> None:
    with pytest.raises(ValueError, match="reserved or malformed"):
        async with cli.invocation(
            project, LOCAL, {"DOCKER_HOST": "tcp://203.0.113.9:2375"}, scrubber
        ):
            pytest.fail("the invocation must not start")

    assert is_empty(work_base)


async def test_a_full_slot_pool_raises_busy_once_the_wait_has_passed(
    fake_binary: ComposeBinary,
    project: ComposeProject,
    scrubber: Scrubber,
    work_base: Path,
) -> None:
    cli = ComposeCli(fake_binary, work_base=work_base, slots=1)
    waited = 0.05

    async with cli.invocation(project, LOCAL, {}, scrubber) as run, cli.slot(None):
        with pytest.raises(ComposeBusy):
            await run.config(slot_wait=waited)
        with pytest.raises(ComposeBusy):
            async with cli.slot(waited):
                pytest.fail("no slot should be free")

    async with cli.slot(waited):
        pass
    assert is_empty(work_base)


async def test_a_missing_binary_is_reported_as_unavailable_and_cleans_up(
    project: ComposeProject, scrubber: Scrubber, work_base: Path
) -> None:
    cli = ComposeCli(ComposeBinary(("/nonexistent/docker-compose",)), work_base=work_base)

    async with cli.invocation(project, LOCAL, {}, scrubber) as run:
        with pytest.raises(ComposeUnavailable):
            await run.ps()

    assert is_empty(work_base)


def test_build_argv_passes_each_extra_file_after_the_projects_own() -> None:
    project = ComposeProject(
        "stack-a",
        PROJECT_DIRECTORY,
        PROJECT_DIRECTORY / "compose.yaml",
        extra_files=(Path("/srv/work/sources.yaml"),),
    )

    argv = build_argv(project, EMPTY_ENV, ("ps",))

    start = argv.index("--file")
    assert argv[start : start + 4] == (
        "--file",
        "/srv/project/compose.yaml",
        "--file",
        "/srv/work/sources.yaml",
    )


def test_an_extra_file_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ComposeProject(
            "stack-a",
            PROJECT_DIRECTORY,
            PROJECT_DIRECTORY / "compose.yaml",
            extra_files=(Path("sources.yaml"),),
        )


@pytest.mark.usefixtures("permit_loopback")
async def test_sources_reach_compose_through_the_environment_and_a_private_override(
    cli: ComposeCli,
    project: ComposeProject,
    scrubber: Scrubber,
    work_base: Path,
    record_path: Path,
    records: Records,
) -> None:
    content = "line $HOME ${X} $$\n"
    secret = "sentinel-secret-content"
    sourced = ComposeProject(
        project.name,
        project.directory,
        project.file,
        sources=(
            ComposeSource("secrets", "token", secret, "t.txt", True),
            ComposeSource("configs", "app", content, "app.conf"),
        ),
    )

    async with cli.invocation(sourced, LOCAL, {"FAKE_RECORD": str(record_path)}, scrubber) as run:
        await run.ps()

    (seen,) = records()
    override = str(Path(seen["workdir"]) / SOURCES_FILE)
    assert seen["argv"][seen["argv"].index("--file") + 2 : seen["argv"].index("--file") + 4] == [
        "--file",
        override,
    ]
    assert seen["env"]["DOCKMIN_SOURCE_1"] == content
    assert seen["env"]["DOCKMIN_SOURCE_2"] == secret
    assert seen["entries"][SOURCES_FILE] == "0o600"
    assert is_empty(work_base)


@pytest.mark.usefixtures("permit_loopback")
async def test_sensitive_strings_of_a_project_are_redacted_from_compose_output(
    cli: ComposeCli,
    project: ComposeProject,
    scrubber: Scrubber,
) -> None:
    sensitive = ComposeProject(
        project.name, project.directory, project.file, sensitive=frozenset({"alpha-secret-line"})
    )

    async with cli.invocation(sensitive, LOCAL, {}, scrubber):
        pass

    assert scrubber.scrub_text("x alpha-secret-line y") == "x [REDACTED] y"
