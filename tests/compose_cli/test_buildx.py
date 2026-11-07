"""Tests for app/compose_cli/buildx.py: location, argument list, environment and rawjson parsing."""

import asyncio
import base64
import json
import os
import shutil
import socket
import stat
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

import app.compose_cli.buildx as buildx_module
from app.compose_cli.buildx import (
    MAX_LOG_BYTES,
    MAX_PROGRESS_LINE_BYTES,
    BuildEvent,
    BuildFailed,
    BuildResult,
    BuildSpec,
    BuildTimedOut,
    BuildxBinary,
    BuildxCli,
    BuildxUnavailable,
    LogLine,
    VertexUpdate,
    build_argv,
    locate_buildx,
)
from app.compose_cli.errors import ComposeHostRefused
from app.compose_cli.target import ComposeTarget, TargetMaterial
from app.compose_cli.workdir import create_invocation_dir
from app.config import load_settings
from app.logging import Scrubber
from app.netcheck import AddrInfo, Resolver
from app.selfcheck import BUILDX_BINARY_VAR, check_buildx
from tests.support import VALID_KEY

STUB = Path(__file__).resolve().parents[1] / "buildx_stub.sh"
EXECUTABLE = 0o755
SAFE_DIRECTORY = 0o755
OTHER_USER = 4242
NO_PATH = ""
TAG = "stack-a-web"
SHELL_PAYLOAD = "a b;$(touch pwned)"
SECRET = "hunter2-secret-value"
LOCAL = ComposeTarget("unix:///var/run/docker.sock", None)
REMOTE_ADDRESS = "203.0.113.9"
REMOTE = ComposeTarget(
    f"tcp://{REMOTE_ADDRESS}:2376", TargetMaterial("CA\n", "CERT\n", "KEY-BODY\n")
)
STUB_RECORDED_NOISE = {"PWD", "OLDPWD", "SHLVL", "_"}
CONSTRUCTED_NAMES = {
    "PATH",
    "HOME",
    "DOCKER_CONFIG",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "TZ",
    "NO_COLOR",
    "DOCKER_CLI_HINTS",
    "DOCKER_HOST",
    "BUILDX_CONFIG",
}
AMBIENT_VARIABLES = {
    "HTTPS_PROXY": "http://proxy.invalid:3128",
    "https_proxy": "http://proxy.invalid:3128",
    "HTTP_PROXY": "http://proxy.invalid:3128",
    "NO_PROXY": "localhost",
    "DOCKMIN_SECRET_KEY": VALID_KEY,
    "BUILDKIT_PROGRESS": "plain",
    "DOCKER_CONTEXT": "elsewhere",
    "DOCKER_CONFIG": "/ambient/docker",
    "SSL_CERT_FILE": "/ambient/ca.pem",
}
UNTRUSTED_MODES = [
    pytest.param(0o775, SAFE_DIRECTORY, id="group-writable-file"),
    pytest.param(0o757, SAFE_DIRECTORY, id="world-writable-file"),
    pytest.param(EXECUTABLE, 0o775, id="group-writable-directory"),
    pytest.param(EXECUTABLE, 0o777, id="world-writable-directory"),
    pytest.param(0o644, SAFE_DIRECTORY, id="not-executable"),
]
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
STARTED = "2026-11-06T10:00:00Z"
COMPLETED = "2026-11-06T10:00:05Z"

type Installer = Callable[..., Path]
type Scenario = Callable[..., None]
type Builder = Callable[..., BuildResult]


def vertex(digest: str, name: str, **fields: object) -> str:
    return json.dumps({"vertexes": [{"digest": digest, "name": name, **fields}]})


def output(digest: str, text: str) -> str:
    data = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return json.dumps({"logs": [{"vertex": digest, "stream": 1, "data": data}]})


def resolving_to(*addresses: str) -> Resolver:
    async def resolve(host: str, port: int) -> Sequence[AddrInfo]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    return resolve


@pytest.fixture
def install(tmp_path: Path) -> Installer:
    def make(
        directory: str,
        *,
        name: str = "docker-buildx",
        file_mode: int = EXECUTABLE,
        directory_mode: int = SAFE_DIRECTORY,
    ) -> Path:
        parent = tmp_path / directory
        parent.mkdir(exist_ok=True)
        parent.chmod(directory_mode)
        binary = parent / name
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(file_mode)
        return binary

    return make


@pytest.fixture
def stub_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "stub"
    directory.mkdir()
    directory.chmod(SAFE_DIRECTORY)
    shutil.copyfile(STUB, directory / "docker-buildx")
    (directory / "docker-buildx").chmod(EXECUTABLE)
    return directory


@pytest.fixture
def scenario(stub_dir: Path) -> Scenario:
    def write(
        *lines: str, exit_code: int | None = None, hang: bool = False, stdout: str | None = None
    ) -> None:
        if lines:
            (stub_dir / "stderr.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if exit_code is not None:
            (stub_dir / "exit").write_text(str(exit_code), encoding="utf-8")
        if hang:
            (stub_dir / "hang").write_text("", encoding="utf-8")
        if stdout is not None:
            (stub_dir / "stdout.txt").write_text(stdout, encoding="utf-8")

    return write


@pytest.fixture
def workdir(work_base: Path) -> Path:
    path = create_invocation_dir(work_base)
    context = path / "context"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return path


@pytest.fixture
def spec(workdir: Path) -> BuildSpec:
    return BuildSpec(tag=TAG, context_dir=workdir / "context")


@pytest.fixture
def buildx(stub_dir: Path) -> BuildxCli:
    binary = BuildxBinary((str(stub_dir / "docker-buildx"),))
    return BuildxCli(binary, resolver=resolving_to(REMOTE_ADDRESS))


def entry_names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def recorded(stub_dir: Path, name: str) -> str:
    return (stub_dir / name).read_text(encoding="utf-8")


def recorded_environment(stub_dir: Path) -> dict[str, str]:
    pairs = (line.partition("=") for line in recorded(stub_dir, "env").splitlines())
    return {name: value for name, _, value in pairs if name not in STUB_RECORDED_NOISE}


def test_a_trusted_configured_binary_is_returned_as_given(install: Installer) -> None:
    binary = install("good")

    assert locate_buildx(binary) == BuildxBinary((str(binary),))


@pytest.mark.parametrize(("file_mode", "directory_mode"), UNTRUSTED_MODES)
def test_a_configured_binary_that_is_untrusted_is_unavailable_and_not_replaced_by_a_search(
    install: Installer, file_mode: int, directory_mode: int
) -> None:
    bad = install("bad", file_mode=file_mode, directory_mode=directory_mode)
    good = install("good")

    assert locate_buildx(bad, search_paths=[good], path_variable=str(good.parent)) is None


def test_a_configured_path_that_is_relative_missing_or_a_directory_is_unavailable(
    install: Installer, tmp_path: Path
) -> None:
    present = install("good")

    assert locate_buildx(Path("docker-buildx")) is None
    assert locate_buildx(tmp_path / "absent" / "docker-buildx") is None
    assert locate_buildx(present.parent) is None


@pytest.mark.parametrize(("file_mode", "directory_mode"), UNTRUSTED_MODES)
def test_an_untrusted_candidate_in_the_search_paths_or_on_path_is_skipped(
    install: Installer, file_mode: int, directory_mode: int
) -> None:
    candidate = install("bad", file_mode=file_mode, directory_mode=directory_mode)

    assert locate_buildx(None, search_paths=[candidate], path_variable=NO_PATH) is None
    assert locate_buildx(None, search_paths=[], path_variable=str(candidate.parent)) is None


def test_the_search_paths_are_tried_in_order_and_before_path(install: Installer) -> None:
    bad = install("bad", file_mode=0o775)
    first = install("first")
    second = install("second")
    on_path = install("on-path")

    found = locate_buildx(
        None, search_paths=[bad, second, first], path_variable=str(on_path.parent)
    )

    assert found == BuildxBinary((str(second),))


def test_path_is_consulted_only_when_no_search_path_is_usable(install: Installer) -> None:
    on_path = install("on-path")
    missing = on_path.parent.parent / "absent" / "docker-buildx"

    found = locate_buildx(None, search_paths=[missing], path_variable=str(on_path.parent))

    assert found == BuildxBinary((str(on_path),))


def test_a_compose_binary_is_not_mistaken_for_buildx(install: Installer) -> None:
    compose_only = install("compose", name="docker-compose")

    assert locate_buildx(None, search_paths=[], path_variable=str(compose_only.parent)) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root-owned files are trusted for every user")
def test_a_binary_owned_by_neither_root_nor_the_server_user_is_unavailable(
    install: Installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = install("good")
    monkeypatch.setattr(os, "geteuid", lambda: OTHER_USER)

    assert locate_buildx(binary) is None
    assert locate_buildx(None, search_paths=[binary], path_variable=NO_PATH) is None


def test_the_argument_list_has_the_fixed_flags_then_each_option_as_its_own_element(
    tmp_path: Path,
) -> None:
    context = tmp_path / "context"
    spec = BuildSpec(
        tag=TAG,
        context_dir=context,
        dockerfile="docker/Dockerfile.prod",
        target="runtime",
        no_cache=True,
        args={"B": "2", "A": SHELL_PAYLOAD},
        labels={"com.docker.compose.project": "stack-a", "app.digest": "0f"},
    )

    assert build_argv(spec) == (
        "build",
        "--progress",
        "rawjson",
        "--pull=false",
        "--provenance=false",
        "--sbom=false",
        "--tag",
        TAG,
        "--file",
        f"{context}/docker/Dockerfile.prod",
        "--target",
        "runtime",
        "--no-cache",
        "--build-arg",
        f"A={SHELL_PAYLOAD}",
        "--build-arg",
        "B=2",
        "--label",
        "app.digest=0f",
        "--label",
        "com.docker.compose.project=stack-a",
        str(context),
    )


def test_the_argument_list_omits_target_and_no_cache_when_they_are_not_set(
    tmp_path: Path,
) -> None:
    argv = build_argv(BuildSpec(tag=TAG, context_dir=tmp_path))

    assert "--target" not in argv
    assert "--no-cache" not in argv
    assert "--build-arg" not in argv
    assert "--label" not in argv


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"tag": "-rm"}, id="tag-flag"),
        pytest.param({"tag": "Upper"}, id="tag-upper-case"),
        pytest.param({"tag": "a b"}, id="tag-space"),
        pytest.param({"tag": "a" * 256}, id="tag-too-long"),
        pytest.param({"context_dir": Path("relative")}, id="relative-context"),
        pytest.param({"dockerfile": ""}, id="empty-dockerfile"),
        pytest.param({"dockerfile": "/etc/passwd"}, id="absolute-dockerfile"),
        pytest.param({"dockerfile": "../Dockerfile"}, id="dockerfile-parent"),
        pytest.param({"dockerfile": "a//Dockerfile"}, id="dockerfile-empty-segment"),
        pytest.param({"dockerfile": "Docker\0file"}, id="dockerfile-nul"),
        pytest.param({"target": "--rm"}, id="target-flag"),
        pytest.param({"args": {"BUILDKIT_SYNTAX": "x"}}, id="reserved-argument"),
        pytest.param({"args": {"DOCKER_HOST": "x"}}, id="reserved-docker-argument"),
        pytest.param({"args": {"1BAD": "x"}}, id="malformed-argument"),
        pytest.param({"args": {"A": "x\0y"}}, id="argument-nul"),
        pytest.param({"labels": {"": "x"}}, id="empty-label-key"),
        pytest.param({"labels": {"k=v": "x"}}, id="label-key-equals"),
        pytest.param({"labels": {"k": "x\0"}}, id="label-nul"),
    ],
)
def test_a_build_spec_outside_the_grammar_is_refused(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    base: dict[str, object] = {"tag": TAG, "context_dir": tmp_path}

    with pytest.raises(ValueError, match=r"permitted|absolute|inside|reserved|malformed|NUL"):
        BuildSpec(**(base | fields))  # type: ignore[arg-type]


async def test_build_runs_the_binary_directly_with_the_argument_list_and_a_constructed_environment(
    buildx: BuildxCli,
    stub_dir: Path,
    workdir: Path,
    scenario: Scenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in AMBIENT_VARIABLES.items():
        monkeypatch.setenv(name, value)
    scenario(vertex(DIGEST_A, "[1/1] FROM scratch", started=STARTED))
    spec = BuildSpec(
        tag=TAG,
        context_dir=workdir / "context",
        target="runtime",
        args={"NAME": SHELL_PAYLOAD},
        labels={"a": "b"},
    )

    await buildx.build(LOCAL, workdir, spec)

    context = (workdir / "context").resolve()
    environment = recorded_environment(stub_dir)
    assert recorded(stub_dir, "argv").splitlines() == list(
        build_argv(replace(spec, context_dir=context))
    )
    assert not (context / "pwned").exists()
    assert recorded(stub_dir, "cwd").strip() == str(context)
    assert set(environment) == CONSTRUCTED_NAMES
    assert environment["DOCKER_HOST"] == LOCAL.docker_host
    assert environment["DOCKER_CONFIG"] == str(workdir / "config")
    assert environment["BUILDX_CONFIG"] == str(workdir / "buildx")
    assert environment["HOME"] == str(workdir / "home")
    probe = recorded(stub_dir, "probe").splitlines()
    assert probe == ["docker_config={}", "buildx_config=dir", "stdin_bytes=0"]


async def test_build_against_a_tls_host_gives_it_the_work_directory_certificate_files(
    buildx: BuildxCli, stub_dir: Path, workdir: Path, scenario: Scenario, spec: BuildSpec
) -> None:
    scenario()

    await buildx.build(REMOTE, workdir, spec)

    environment = recorded_environment(stub_dir)
    assert environment["DOCKER_HOST"] == REMOTE.docker_host
    assert environment["DOCKER_TLS_VERIFY"] == "1"
    assert environment["DOCKER_CERT_PATH"] == str(workdir / "tls")
    assert "tls=ca.pem cert.pem key.pem " in recorded(stub_dir, "probe")
    assert stat.S_IMODE((workdir / "tls" / "key.pem").stat().st_mode) == 0o600


async def test_build_reports_vertex_progress_from_rawjson_lines_and_parses_nothing_else(
    buildx: BuildxCli, workdir: Path, scenario: Scenario, spec: BuildSpec
) -> None:
    injected = vertex(DIGEST_C, "injected from build output", started=STARTED)
    scenario(
        vertex(DIGEST_A, "[1/2] FROM docker.io/library/busybox", started=STARTED),
        "#1 plain text that is not json",
        '{"vertexes": [',
        '{"unrelated": "object"}',
        '{"vertexes": "not a list"}',
        vertex(
            DIGEST_A, "[1/2] FROM docker.io/library/busybox", started=STARTED, completed=COMPLETED
        ),
        vertex(DIGEST_B, "[2/2] RUN true", started=STARTED, completed=COMPLETED, cached=True),
        vertex("not-a-digest", "bad digest"),
        output(DIGEST_B, injected + "\n"),
        vertex(DIGEST_B, "[2/2] RUN true", started=STARTED, completed=COMPLETED, cached=True),
    )
    events: list[BuildEvent] = []

    result = await buildx.build(LOCAL, workdir, spec, on_event=events.append)

    assert result.vertices == (
        VertexUpdate(DIGEST_A, "[1/2] FROM docker.io/library/busybox", "done", False, None),
        VertexUpdate(DIGEST_B, "[2/2] RUN true", "done", True, None),
    )
    assert events == [
        VertexUpdate(DIGEST_A, "[1/2] FROM docker.io/library/busybox", "running", False, None),
        VertexUpdate(DIGEST_A, "[1/2] FROM docker.io/library/busybox", "done", False, None),
        VertexUpdate(DIGEST_B, "[2/2] RUN true", "done", True, None),
        LogLine("[2/2] RUN true", injected),
    ]
    assert result.log == injected


async def test_build_marks_a_vertex_that_reports_an_error(
    buildx: BuildxCli, workdir: Path, scenario: Scenario, spec: BuildSpec
) -> None:
    reason = 'process "/bin/sh -c false" did not complete successfully: exit code: 1'
    scenario(vertex(DIGEST_A, "[1/1] RUN false", started=STARTED, error=reason))

    result = await buildx.build(LOCAL, workdir, spec)

    assert result.vertices == (VertexUpdate(DIGEST_A, "[1/1] RUN false", "error", False, reason),)


async def test_a_status_line_over_the_line_limit_is_not_parsed(
    buildx: BuildxCli, workdir: Path, scenario: Scenario, spec: BuildSpec
) -> None:
    scenario(vertex(DIGEST_A, "n" * (MAX_PROGRESS_LINE_BYTES + 1), started=STARTED))

    result = await buildx.build(LOCAL, workdir, spec)

    assert result.vertices == ()


async def test_build_output_is_scrubbed_including_a_secret_split_across_chunks(
    buildx: BuildxCli, workdir: Path, scenario: Scenario, spec: BuildSpec
) -> None:
    scrubber = Scrubber()
    scrubber.register_secret(SECRET)
    key_block = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----\n"
    scenario(
        vertex(DIGEST_A, f"[1/1] RUN echo {SECRET}", started=STARTED),
        output(DIGEST_A, "token=hunter2-sec"),
        output(DIGEST_A, "ret-value done\n"),
        output(DIGEST_A, key_block),
    )
    events: list[BuildEvent] = []

    result = await buildx.build(
        LOCAL, workdir, spec, scrub=scrubber.scrub_text, on_event=events.append
    )

    assert SECRET not in result.log
    assert "MIIEvQIBADANBgkq" not in result.log
    assert "[REDACTED]" in result.log
    assert result.vertices[0].name == "[1/1] RUN echo [REDACTED]"
    assert all(SECRET not in repr(event) for event in events)


async def test_build_output_is_capped_keeping_its_tail_and_marks_the_cut(
    buildx: BuildxCli, workdir: Path, scenario: Scenario, spec: BuildSpec
) -> None:
    chunk_lines = 500
    chunks = 40
    lines = [
        output(
            DIGEST_A,
            "".join(
                f"L{n:06d} {'x' * 90}\n" for n in range(i * chunk_lines, (i + 1) * chunk_lines)
            ),
        )
        for i in range(chunks)
    ]
    scenario(*lines)
    last = chunks * chunk_lines - 1

    result = await buildx.build(LOCAL, workdir, spec)

    assert len(result.log.encode("utf-8")) <= MAX_LOG_BYTES
    assert result.log_truncated
    assert f"L{last:06d}" in result.log
    assert "L000000" not in result.log


async def test_a_failed_build_raises_with_the_exit_code_and_the_scrubbed_text(
    buildx: BuildxCli, workdir: Path, scenario: Scenario, spec: BuildSpec
) -> None:
    scrubber = Scrubber()
    scrubber.register_secret(SECRET)
    scenario(
        vertex(DIGEST_A, "[1/1] RUN false", started=STARTED, error="exit code: 1"),
        output(DIGEST_A, f"leaked {SECRET}\n"),
        f"ERROR: failed to build: failed to solve: {SECRET}",
        exit_code=1,
    )

    with pytest.raises(BuildFailed) as failure:
        await buildx.build(LOCAL, workdir, spec, scrub=scrubber.scrub_text)

    assert failure.value.exit_code == 1
    assert failure.value.reason == "build_failed"
    assert failure.value.stderr == "ERROR: failed to build: failed to solve: [REDACTED]"
    assert failure.value.log == "leaked [REDACTED]"
    assert failure.value.vertices[0].status == "error"
    assert not isinstance(failure.value, BuildTimedOut)


async def test_a_build_past_its_deadline_is_killed_and_raises_timed_out(
    buildx: BuildxCli, stub_dir: Path, workdir: Path, scenario: Scenario, spec: BuildSpec
) -> None:
    scenario(hang=True)
    deadline = asyncio.get_running_loop().time() - 10.0

    with pytest.raises(BuildTimedOut) as failure:
        await buildx.build(LOCAL, workdir, spec, deadline=deadline)

    assert failure.value.exit_code is None
    assert (stub_dir / "argv").exists()


async def test_no_more_builds_than_the_slot_count_run_at_once(
    buildx: BuildxCli, work_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Semaphore(0)
    gate = asyncio.Event()
    running = 0
    peak = 0

    async def held(*_: object) -> tuple[int, bool]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        entered.release()
        await gate.wait()
        running -= 1
        return 0, False

    def prepared() -> tuple[Path, BuildSpec]:
        path = create_invocation_dir(work_base)
        (path / "context").mkdir()
        return path, BuildSpec(tag=TAG, context_dir=path / "context")

    monkeypatch.setattr(buildx_module, "_run", held)
    limited = BuildxCli(buildx.binary, resolver=resolving_to(REMOTE_ADDRESS), slots=2)
    tasks = [asyncio.ensure_future(limited.build(LOCAL, *prepared())) for _ in range(3)]
    await entered.acquire()
    await entered.acquire()
    assert limited._slots.locked()
    gate.set()
    await asyncio.gather(*tasks)

    assert peak == 2


async def test_a_failing_event_callback_is_dropped_and_the_build_still_finishes(
    buildx: BuildxCli, workdir: Path, scenario: Scenario, spec: BuildSpec
) -> None:
    calls: list[BuildEvent] = []

    def failing(event: BuildEvent) -> None:
        calls.append(event)
        raise RuntimeError("callback failed")

    scenario(
        vertex(DIGEST_A, "[1/1] RUN true", started=STARTED),
        vertex(DIGEST_A, "[1/1] RUN true", started=STARTED, completed=COMPLETED),
    )

    result = await buildx.build(LOCAL, workdir, spec, on_event=failing)

    assert len(calls) == 1
    assert result.vertices[0].status == "done"


async def test_without_a_binary_builds_are_unavailable_and_nothing_is_created(
    workdir: Path, spec: BuildSpec
) -> None:
    cli = BuildxCli(None)
    before = entry_names(workdir)

    with pytest.raises(BuildxUnavailable) as unavailable:
        await cli.build(LOCAL, workdir, spec)

    assert not cli.available
    assert unavailable.value.reason == "buildx_unavailable"
    assert entry_names(workdir) == before


async def test_a_binary_that_has_vanished_is_unavailable(
    tmp_path: Path, workdir: Path, spec: BuildSpec
) -> None:
    cli = BuildxCli(BuildxBinary((str(tmp_path / "gone" / "docker-buildx"),)))

    with pytest.raises(BuildxUnavailable):
        await cli.build(LOCAL, workdir, spec)


@pytest.mark.parametrize("escape", ["sibling", "workdir", "symlink", "dockerfile-link"])
async def test_a_context_that_leaves_the_work_directory_is_refused_before_anything_starts(
    buildx: BuildxCli, stub_dir: Path, workdir: Path, tmp_path: Path, escape: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    context = workdir / "context"
    match escape:
        case "sibling":
            context = outside
        case "workdir":
            context = workdir
        case "symlink":
            context = workdir / "link"
            context.symlink_to(outside)
        case "dockerfile-link":
            (context / "Dockerfile").unlink()
            (context / "Dockerfile").symlink_to(outside / "Dockerfile")

    with pytest.raises(ValueError, match="inside"):
        await buildx.build(LOCAL, workdir, BuildSpec(tag=TAG, context_dir=context))

    assert not (stub_dir / "argv").exists()
    assert not (workdir / "config").exists()


async def test_a_refused_tcp_host_starts_nothing_and_writes_no_key(
    buildx: BuildxCli, stub_dir: Path, workdir: Path, spec: BuildSpec
) -> None:
    internal = ComposeTarget("tcp://127.0.0.1:2376", REMOTE.tls)

    with pytest.raises(ComposeHostRefused):
        await buildx.build(internal, workdir, spec)

    assert not (stub_dir / "argv").exists()
    assert not (workdir / "tls").exists()


def test_a_blank_buildx_binary_setting_is_unset_and_a_path_is_kept(tmp_path: Path) -> None:
    environ = {"DOCKMIN_SECRET_KEY": VALID_KEY}

    assert load_settings({**environ, BUILDX_BINARY_VAR: ""}).buildx_binary is None
    assert load_settings(environ).buildx_binary is None
    assert load_settings({**environ, BUILDX_BINARY_VAR: str(tmp_path)}).buildx_binary == tmp_path


def test_the_startup_check_reports_missing_buildx_without_stopping_the_start() -> None:
    failure = check_buildx(available=False)

    assert check_buildx(available=True) is None
    assert failure is not None
    assert (failure.check, failure.fatal) == ("buildx", False)
    assert BUILDX_BINARY_VAR in failure.remedy
