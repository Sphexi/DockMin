"""Tests for app/compose_cli/process.py: capture limits, scrubbing, group kill and cleanup."""

import asyncio
import json
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.compose_cli import process
from app.compose_cli.binary import ComposeBinary
from app.compose_cli.cli import ComposeCli, ComposeProject, up_tail
from app.compose_cli.errors import (
    ComposeCommandFailed,
    ComposeOutputTooLarge,
    ComposeTimedOut,
)
from app.compose_cli.parsers import MAX_EVENT_ID_CHARS, MAX_EVENT_TEXT_CHARS, ProgressEvent
from app.compose_cli.process import (
    MAX_EVENTS,
    MAX_LINE_BYTES,
    MAX_STDERR_BYTES,
    MAX_STDERR_LINES,
    Invocation,
    _Collector,
    run,
    uninterrupted,
)
from app.compose_cli.target import ComposeTarget, TargetMaterial
from app.logging import REDACTED, Scrubber
from app.services.stack_deploy import make_scrubber

Records = Callable[[], list[dict[str, Any]]]
Sample = Callable[[str], bytes]

LOCAL = ComposeTarget("unix:///var/run/docker.sock", None)
TLS = ComposeTarget("tcp://127.0.0.1:2376", TargetMaterial("CA\n", "CERT\n", "KEY\n"))
TLS_FILES = {"ca.pem": "0o600", "cert.pem": "0o600", "key.pem": "0o600"}
FAIL_SAMPLE_EVENTS = 11
REGISTERED_SECRET = "SECRET_SENTINEL_VALUE"
DOCKER_SECRET = 'Pa"ss<w>&0rd\\42'
SECRET_TAIL = "0rd"
HUNTING_TIMEOUT_SECONDS = 1.0
SHORT_TERM_GRACE_SECONDS = 0.3
START_WAIT_SECONDS = 5.0
GONE_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.02
OUTPUT_CAP_BYTES = 1024 * 1024
PIPE_GRACE_SECONDS = 0.3
EVENT_LINE = b'{"id":"Container c","status":"Working","text":"Starting"}'


def go_json(document: dict[str, Any]) -> bytes:
    """Encode like Go's HTML-safe JSON encoder, which escapes <, > and &."""
    text = json.dumps(document, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").encode()


def is_empty(directory: Path) -> bool:
    return not any(directory.iterdir())


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return stat.rsplit(") ", 1)[1][0] != "Z"


def read_pids(path: Path) -> tuple[int, int] | None:
    parts = path.read_text(encoding="utf-8").split() if path.exists() else []
    return (int(parts[0]), int(parts[1])) if len(parts) == 2 else None


async def started(path: Path) -> tuple[int, int]:
    async with asyncio.timeout(START_WAIT_SECONDS):
        while (pids := read_pids(path)) is None:  # noqa: ASYNC110 - the fake signals through a file
            await asyncio.sleep(POLL_SECONDS)
    return pids


async def survivors(pids: tuple[int, ...]) -> list[int]:
    try:
        async with asyncio.timeout(GONE_WAIT_SECONDS):
            while any(alive(pid) for pid in pids):  # noqa: ASYNC110 - the state is read from /proc
                await asyncio.sleep(POLL_SECONDS)
    except TimeoutError:
        pass
    return [pid for pid in pids if alive(pid)]


@pytest.fixture
def short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "TERM_GRACE_SECONDS", SHORT_TERM_GRACE_SECONDS)
    monkeypatch.setattr(process, "EOF_GRACE_SECONDS", PIPE_GRACE_SECONDS)


def direct(fake_binary: ComposeBinary, tmp_path: Path, scenario: str, **extra: str) -> Invocation:
    return Invocation(
        argv=("config",),
        env={"FAKE_SCENARIO": scenario, **extra},
        cwd=tmp_path,
        timeout=20.0,
        stdout_cap=OUTPUT_CAP_BYTES,
        command=("config",),
    )


async def hung_up(
    cli: ComposeCli, project: ComposeProject, scrubber: Scrubber, variables: dict[str, str]
) -> None:
    async with cli.invocation(project, TLS, variables, scrubber) as running:
        await running.compose_up(remove_orphans=False)


@pytest.mark.usefixtures("permit_loopback")
async def test_a_failing_run_raises_with_scrubbed_stderr_events_and_the_exit_code(
    cli: ComposeCli,
    project: ComposeProject,
    scrubber: Scrubber,
    work_base: Path,
    record_path: Path,
    records: Records,
) -> None:
    scrubber.register_secret(REGISTERED_SECRET)
    data_dir = work_base.parent
    leak = f"token=hunter2secretvalue in {data_dir}/stacks/abc/compose.yaml and {REGISTERED_SECRET}"
    variables = {
        "FAKE_SCENARIO": "fail",
        "FAKE_RECORD": str(record_path),
        "FAKE_STDERR_RAW": leak,
    }

    async with cli.invocation(project, TLS, variables, scrubber) as running:
        with pytest.raises(ComposeCommandFailed) as failed:
            await running.compose_up(remove_orphans=False)

    exc = failed.value
    assert type(exc) is ComposeCommandFailed
    assert (exc.exit_code, str(exc)) == (1, "Docker Compose exited with status 1.")
    assert exc.command == up_tail(remove_orphans=False)
    assert len(exc.events) == FAIL_SAMPLE_EVENTS
    assert "port is already allocated" in exc.stderr
    assert '{"error"' not in exc.stderr
    assert "<data>/stacks/abc/compose.yaml" in exc.stderr
    assert str(data_dir) not in exc.stderr
    assert "hunter2secretvalue" not in exc.stderr
    assert REGISTERED_SECRET not in exc.stderr
    assert REDACTED in exc.stderr
    assert "port is already allocated" not in repr(exc) + str(exc)
    assert [r["tls"] for r in records()] == [TLS_FILES]
    assert is_empty(work_base)


@pytest.mark.usefixtures("permit_loopback")
async def test_a_timeout_kills_every_process_in_the_group_and_reports_partial_progress(
    fake_binary: ComposeBinary,
    project: ComposeProject,
    scrubber: Scrubber,
    work_base: Path,
    tmp_path: Path,
    records: Records,
    record_path: Path,
    short_grace: None,
) -> None:
    cli = ComposeCli(fake_binary, work_base=work_base, operation_timeout=HUNTING_TIMEOUT_SECONDS)

    async def hang(scenario: str) -> tuple[ComposeTimedOut, tuple[int, int]]:
        pids_file = tmp_path / f"pids-{scenario}"
        variables = {
            "FAKE_SCENARIO": scenario,
            "FAKE_PIDS": str(pids_file),
            "FAKE_RECORD": str(record_path),
        }
        async with cli.invocation(project, TLS, variables, scrubber) as running:
            with pytest.raises(ComposeTimedOut) as timed_out:
                await running.compose_up(remove_orphans=False)
        return timed_out.value, await started(pids_file)

    (polite, polite_pids), (stubborn, stubborn_pids) = await asyncio.gather(
        hang("hang"), hang("hang_ignore")
    )

    assert isinstance(polite, ComposeCommandFailed)
    assert str(polite) == "Docker Compose did not finish in time."
    assert len(polite.events) == len(stubborn.events) == 1
    assert polite.exit_code == -signal.SIGTERM
    assert stubborn.exit_code == -signal.SIGKILL
    assert await survivors(polite_pids + stubborn_pids) == []
    assert [r["tls"] for r in records()] == [TLS_FILES, TLS_FILES]
    assert is_empty(work_base)


@pytest.mark.usefixtures("permit_loopback")
async def test_cancelling_the_caller_kills_the_group_and_removes_the_work_directory(
    cli: ComposeCli,
    project: ComposeProject,
    scrubber: Scrubber,
    work_base: Path,
    tmp_path: Path,
) -> None:
    pids_file = tmp_path / "pids"
    caller = asyncio.ensure_future(
        hung_up(cli, project, scrubber, {"FAKE_SCENARIO": "hang", "FAKE_PIDS": str(pids_file)})
    )
    pids = await started(pids_file)

    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert await survivors(pids) == []
    assert is_empty(work_base)


@pytest.mark.usefixtures("permit_loopback")
async def test_a_second_cancellation_during_the_kill_still_finishes_the_cleanup(
    cli: ComposeCli,
    project: ComposeProject,
    scrubber: Scrubber,
    work_base: Path,
    tmp_path: Path,
    short_grace: None,
) -> None:
    pids_file = tmp_path / "pids"
    caller = asyncio.ensure_future(
        hung_up(
            cli, project, scrubber, {"FAKE_SCENARIO": "hang_ignore", "FAKE_PIDS": str(pids_file)}
        )
    )
    pids = await started(pids_file)

    caller.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert [pid for pid in pids if alive(pid)] == []
    assert is_empty(work_base)


async def test_output_over_the_cap_raises_and_the_message_names_no_content(
    fake_binary: ComposeBinary, tmp_path: Path
) -> None:
    with pytest.raises(ComposeOutputTooLarge) as too_large:
        await run(fake_binary, direct(fake_binary, tmp_path, "big"))

    assert str(too_large.value) == "Docker Compose produced more output than dockmin accepts."


async def test_event_and_stderr_capture_stay_within_their_bounds(
    fake_binary: ComposeBinary, tmp_path: Path
) -> None:
    completed = await run(fake_binary, direct(fake_binary, tmp_path, "noisy"))

    assert completed.exit_code == 0
    assert len(completed.events) == MAX_EVENTS
    assert completed.events[0].resource == "Container c100"
    assert completed.events[-1].resource == "Container c599"
    assert len(completed.stderr) == MAX_LINE_BYTES


async def test_a_descendant_holding_the_pipes_does_not_hang_the_run_and_is_killed(
    fake_binary: ComposeBinary, tmp_path: Path, short_grace: None
) -> None:
    pids_file = tmp_path / "pids"

    completed = await run(
        fake_binary, direct(fake_binary, tmp_path, "pipeheld", FAKE_PIDS=str(pids_file))
    )

    assert completed.exit_code == 0
    assert len(completed.events) == 1
    assert await survivors(await started(pids_file)) == []


async def test_uninterrupted_finishes_its_work_however_often_the_caller_is_cancelled() -> None:
    release = asyncio.Event()
    finished: list[str] = []

    async def cleanup() -> None:
        await release.wait()
        finished.append("done")

    caller = asyncio.ensure_future(uninterrupted(cleanup()))
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    still_waiting = not caller.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert still_waiting
    assert finished == ["done"]


async def test_uninterrupted_logs_a_failing_cleanup_instead_of_raising_it() -> None:
    async def failing() -> None:
        raise OSError

    await uninterrupted(failing())


async def test_uninterrupted_logs_a_failing_cleanup_under_the_event_it_is_given(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def failing() -> None:
        raise OSError

    await uninterrupted(failing(), event="buildx_cleanup_failed")

    assert [record.getMessage() for record in caplog.records] == ["buildx_cleanup_failed"]


async def test_pump_cuts_each_line_at_the_limit_it_is_given() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"abcdefgh\nxy\ntail")
    reader.feed_eof()
    lines: list[bytes] = []

    await process.pump(reader, lines.append, limit=5)

    assert lines == [b"abcde", b"xy", b"tail"]


def collector(secret: str | None = None, on_event: Any = None) -> _Collector:
    scrubber = make_scrubber([secret]) if secret is not None else None
    return _Collector(on_event, scrubber.scrub_text if scrubber else None)


def test_the_collector_reads_error_and_warning_json_lines_as_their_message() -> None:
    gathered = collector()

    gathered.stderr_line(b'{"error":true,"message":"boom"}')
    gathered.stderr_line(b'{"level":"warning","msg":"careful","time":"t"}')
    gathered.stderr_line(b"plain text")

    assert gathered.stderr_text() == "boom\ncareful\nplain text"
    assert list(gathered.events) == []


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(f"rejected value {DOCKER_SECRET} here".encode(), id="literal"),
        pytest.param(
            go_json({"error": True, "message": f'parsing "{DOCKER_SECRET}": invalid'}),
            id="go-json-error-line",
        ),
        pytest.param(
            go_json({"level": "warning", "msg": f"bad {DOCKER_SECRET}", "time": "t"}),
            id="go-json-warning-line",
        ),
        pytest.param(
            f"quoted {json.dumps(DOCKER_SECRET)[1:-1]} in text".encode(), id="json-escaped-form"
        ),
        pytest.param(b"escaped Pa&#34;ss&lt;w&gt;&amp;0rd\\42 in text", id="html-escaped-form"),
        pytest.param(b"escaped Pa&quot;ss&lt;w&gt;&amp;0rd\\42 in text", id="html-named-form"),
        pytest.param(f"doubled {DOCKER_SECRET.replace('$', '$$')}".encode(), id="dollar-doubled"),
    ],
)
def test_a_secret_is_redacted_from_stderr_in_every_spelling_compose_can_print(line: bytes) -> None:
    gathered = collector(DOCKER_SECRET)

    gathered.stderr_line(line)

    text = gathered.stderr_text()
    assert SECRET_TAIL not in text
    assert REDACTED in text


def test_a_secret_cut_by_the_line_limit_is_scrubbed_before_the_line_is_truncated() -> None:
    gathered = collector(DOCKER_SECRET)
    straddling = b"x" * (MAX_LINE_BYTES - 5) + DOCKER_SECRET.encode()

    gathered.stderr_line(straddling)

    text = gathered.stderr_text()
    assert 'Pa"' not in text
    assert SECRET_TAIL not in text


def test_event_text_is_scrubbed_before_it_reaches_the_callback_and_the_result() -> None:
    delivered: list[ProgressEvent] = []
    gathered = collector(DOCKER_SECRET, on_event=delivered.append)

    gathered.stdout_line(
        go_json({"id": "Image x", "status": "Error", "text": f"denied {DOCKER_SECRET}"})
    )

    (event,) = delivered
    assert SECRET_TAIL not in (event.text or "")
    assert REDACTED in (event.text or "")
    assert list(gathered.events) == [event]


@pytest.mark.parametrize("field", ["text", "id", "parent_id", "status"])
def test_an_event_field_is_scrubbed_before_it_is_cut_so_a_secret_at_the_cut_cannot_survive(
    field: str,
) -> None:
    limit = MAX_EVENT_TEXT_CHARS if field == "text" else MAX_EVENT_ID_CHARS
    document = {"id": "Image x", "status": "Error", "text": "denied"}
    document[field] = "x" * (limit - 5) + DOCKER_SECRET
    gathered = collector(DOCKER_SECRET)

    gathered.stdout_line(go_json(document))

    (event,) = gathered.events
    kept = getattr(event, {"id": "resource", "parent_id": "parent"}.get(field, field)) or ""
    assert SECRET_TAIL not in kept
    assert 'Pa"' not in kept
    assert kept[limit - 5 :] == REDACTED[:5]


def test_the_collector_ignores_stdout_lines_that_are_not_events() -> None:
    gathered = collector()

    gathered.stdout_line(b"plain output")
    gathered.stdout_line(EVENT_LINE)

    assert [e.resource for e in gathered.events] == ["Container c"]
    assert gathered.stderr_text() == ""


def test_a_failing_event_callback_is_dropped_after_its_first_failure_and_events_are_kept() -> None:
    calls: list[ProgressEvent] = []

    def explode(event: ProgressEvent) -> None:
        calls.append(event)
        raise RuntimeError

    gathered = collector(on_event=explode)

    gathered.stderr_line(EVENT_LINE)
    gathered.stderr_line(EVENT_LINE)

    assert len(calls) == 1
    assert len(gathered.events) == 2


def test_stderr_keeps_only_the_most_recent_lines() -> None:
    gathered = collector()
    total = MAX_STDERR_LINES + 50

    for number in range(total):
        gathered.stderr_line(f"line {number}".encode())

    lines = gathered.stderr_text().splitlines()
    assert len(lines) == MAX_STDERR_LINES
    assert lines[0] == "line 50"
    assert lines[-1] == f"line {total - 1}"


def test_stderr_keeps_only_the_most_recent_bytes() -> None:
    gathered = collector()
    kib = b"z" * 1024
    lines = [kib + str(number).encode() for number in range(100)]

    for line in lines:
        gathered.stderr_line(line)

    text = gathered.stderr_text()
    assert len(text.encode()) <= MAX_STDERR_BYTES
    assert text.endswith("99")
    assert not text.startswith("z" * 1024 + "0\n")


def test_the_data_directory_is_replaced_by_a_placeholder_whatever_its_spelling(
    cli: ComposeCli, work_base: Path, tmp_path: Path
) -> None:
    data_dir = work_base.parent
    alias = tmp_path / "alias"
    alias.symlink_to(data_dir)
    text = f"a {data_dir}/stacks/x b {data_dir} c {data_dir}extra d {data_dir}/y"

    stripped = cli.strip_data_dir(text)

    assert stripped == f"a <data>/stacks/x b <data> c {data_dir}extra d <data>/y"
