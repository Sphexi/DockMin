"""Tests for the terminal bridge: how a session runs and ends, against a scripted peer."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from app.services import terminals
from app.services.terminals import (
    INPUT_MAX_BYTES,
    OUTPUT_CHUNK_BYTES,
    TerminalBridge,
    TerminalLimits,
    open_guarded,
)
from tests.terminal_support import Resized, Rig, build_rig, settle, until

READY = {"t": "ready"}
EXIT_CODE = 3
WINDOW = 4 * OUTPUT_CHUNK_BYTES
ACK_STEP = OUTPUT_CHUNK_BYTES
BURST_BYTES = 3 * 1024 * 1024
NORMAL, POLICY, TOO_LARGE, ERROR, ACCESS_ENDED, IDLE, DURATION = (
    1000,
    1008,
    1009,
    1011,
    4403,
    4408,
    4409,
)
QUICK = 0.02
BURST_WIDTHS = (101, 102, 103, 104)


@pytest.fixture(autouse=True)
def quick_waits(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(terminals, "PROCESS_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(terminals, "PROCESS_POLL_SECONDS", 0.005)
    monkeypatch.setattr(terminals, "WRITE_FAILURE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(terminals, "FAREWELL_SECONDS", 0.2)
    yield


def resize_messages(widths: tuple[int, ...]) -> list[dict[str, Any]]:
    return [{"t": "resize", "cols": width, "rows": 30} for width in widths]


def ending(rig: Rig) -> dict[str, Any]:
    return rig.peer.last_text


async def test_a_process_that_exits_ends_the_session_with_its_exit_code() -> None:
    rig = build_rig()
    rig.client.exec_exit_code = EXIT_CODE
    rig.hijack.finish()

    end = await rig.run()

    assert (end.reason, end.exit_code, end.close_code) == ("process_exited", EXIT_CODE, NORMAL)
    assert rig.peer.texts == [
        READY,
        {"t": "end", "reason": "process_exited", "exit_code": EXIT_CODE},
    ]
    assert rig.peer.closed_with is not None and rig.peer.closed_with[0] == NORMAL
    assert rig.ends == [("process_exited", EXIT_CODE, False)]


async def test_a_session_releases_its_slot_and_closes_the_engine_stream_when_it_ends() -> None:
    rig = build_rig()
    rig.hijack.finish()

    await rig.run()

    assert rig.slots.open_count == 0
    assert rig.hijack.closed


async def test_an_exec_that_reports_running_briefly_after_the_stream_ends_is_polled() -> None:
    rig = build_rig()
    rig.client.exec_running_polls = 3
    rig.client.exec_exit_code = EXIT_CODE
    rig.hijack.finish()

    end = await rig.run()

    assert (end.reason, end.exit_code, end.process_running) == ("process_exited", EXIT_CODE, False)
    assert rig.client.exec_inspections == 4


async def test_a_stream_ending_while_the_exec_runs_is_a_detach_with_the_process_alive() -> None:
    rig = build_rig()
    rig.client.exec_running = True
    rig.hijack.finish()

    end = await rig.run()

    assert (end.reason, end.process_running) == ("detached", True)
    assert ending(rig) == {
        "t": "end",
        "reason": "detached",
        "exit_code": None,
        "process_running": True,
    }
    assert rig.ends == [("detached", None, True)]


async def test_a_stream_that_ends_after_the_container_stopped_is_a_container_stop() -> None:
    rig = build_rig()
    rig.client.container_running = False
    rig.hijack.finish()

    end = await rig.run()

    assert (end.reason, end.exit_code) == ("container_stopped", None)


async def test_a_stream_that_ends_after_the_container_was_removed_is_a_container_stop() -> None:
    rig = build_rig()
    rig.client.container_gone = True
    rig.hijack.finish()

    end = await rig.run()

    assert end.reason == "container_stopped"


async def test_an_attach_whose_stream_ends_with_the_container_running_is_a_detach() -> None:
    rig = build_rig(attach=True)
    rig.hijack.finish()

    end = await rig.run()

    assert (end.reason, end.process_running) == ("detached", False)
    assert rig.client.exec_inspections == 0


async def test_an_attach_asks_for_the_containers_size_as_soon_as_the_session_is_ready() -> None:
    rig = build_rig(attach=True)
    rig.hijack.finish()

    await rig.run()

    assert rig.client.resizes == [Resized("container", 24, 80)]


async def test_the_browser_leaving_ends_the_session_quietly_and_notes_a_surviving_process() -> None:
    rig = build_rig()
    rig.client.exec_running = True
    rig.peer.hang_up()

    end = await rig.run()

    assert (end.reason, end.close_code, end.process_running) == ("client_closed", NORMAL, True)
    assert rig.peer.texts == [READY]
    assert rig.ends == [("client_closed", None, True)]


@pytest.mark.parametrize(
    ("code", "reason", "close_code"),
    [
        (None, "client_closed", NORMAL),
        (1001, "client_closed", NORMAL),
        (1009, "protocol_error", TOO_LARGE),
        (1012, "environment_closed", NORMAL),
    ],
    ids=["abrupt", "going-away", "message-too-big", "server-restarting"],
)
async def test_the_close_code_the_browser_left_with_decides_how_the_session_is_recorded(
    code: int | None, reason: str, close_code: int
) -> None:
    rig = build_rig()
    rig.peer.hang_up(code)

    end = await rig.run()

    assert (end.reason, end.close_code) == (reason, close_code)


@pytest.mark.parametrize("failing", ["before-ready", "during-output"])
async def test_a_browser_that_is_gone_when_the_bridge_sends_ends_the_session_as_closed(
    failing: str,
) -> None:
    rig = build_rig()
    rig.peer.gone = failing == "before-ready"
    rig.peer.gone_on_output = failing == "during-output"
    rig.hijack.feed(b"output")

    end = await rig.run()

    assert end.reason == "client_closed"
    assert rig.slots.open_count == 0


async def test_keystrokes_reach_the_engine_byte_for_byte() -> None:
    rig = build_rig()
    keys = b"ls -l\r\xff\x00\x1b[A"
    rig.peer.say(keys)
    rig.peer.hang_up()

    await rig.run()

    assert bytes(rig.hijack.written) == keys


async def test_output_reaches_the_browser_in_chunks_no_larger_than_the_frame_limit() -> None:
    rig = build_rig()
    payload = bytes(range(256)) * 1024
    rig.peer.acknowledge_every = ACK_STEP
    rig.hijack.feed(payload)
    rig.hijack.finish()
    frames: list[int] = []
    original = rig.peer.send_bytes

    async def measuring(data: bytes) -> None:
        frames.append(len(data))
        await original(data)

    rig.peer.send_bytes = measuring  # type: ignore[method-assign]

    await rig.run()

    assert bytes(rig.peer.output) == payload
    assert max(frames) <= OUTPUT_CHUNK_BYTES


async def test_a_keystroke_message_over_the_input_limit_ends_the_session_as_too_large() -> None:
    rig = build_rig()
    rig.peer.say(b"x" * (INPUT_MAX_BYTES + 1))

    end = await rig.run()

    assert (end.reason, end.close_code) == ("protocol_error", TOO_LARGE)
    assert rig.hijack.written == b""


async def test_input_at_exactly_the_limit_is_written() -> None:
    rig = build_rig()
    rig.peer.say(b"x" * INPUT_MAX_BYTES)
    rig.peer.hang_up()

    await rig.run()

    assert len(rig.hijack.written) == INPUT_MAX_BYTES


@pytest.mark.parametrize(
    ("message", "close_code"),
    [
        ("not json", POLICY),
        ("", POLICY),
        ("[]", POLICY),
        ("null", POLICY),
        ('{"t":"zzz"}', POLICY),
        ('{"t":"ticket","v":"again"}', POLICY),
        ('{"t":"resize","cols":0,"rows":5}', POLICY),
        ('{"t":"resize","cols":1001,"rows":5}', POLICY),
        ('{"t":"resize","cols":10,"rows":501}', POLICY),
        ('{"t":"resize","cols":10.5,"rows":5}', POLICY),
        ('{"t":"resize","cols":true,"rows":5}', POLICY),
        ('{"t":"resize","cols":"10","rows":5}', POLICY),
        ('{"t":"resize","rows":5}', POLICY),
        ('{"t":"ack","n":0}', POLICY),
        ('{"t":"ack","n":-1}', POLICY),
        ('{"t":"ack","n":2147483649}', POLICY),
        ('{"t":"ack","n":"5"}', POLICY),
        ('{"t":"ack","n":true}', POLICY),
        ("x" * 513, TOO_LARGE),
        ("é" * 300, TOO_LARGE),
        ("[" * 100_000, TOO_LARGE),
    ],
    ids=[
        "text",
        "empty",
        "list",
        "null",
        "unknown-kind",
        "second-ticket",
        "zero-columns",
        "too-many-columns",
        "too-many-rows",
        "fractional-size",
        "boolean-size",
        "string-size",
        "missing-columns",
        "ack-zero",
        "ack-negative",
        "ack-huge",
        "ack-string",
        "ack-boolean",
        "over-512-characters",
        "over-512-bytes",
        "deep-nesting",
    ],
)
async def test_a_malformed_or_oversize_control_message_ends_the_session_as_a_protocol_error(
    message: str, close_code: int
) -> None:
    rig = build_rig()
    rig.peer.say(message)

    end = await rig.run()

    assert (end.reason, end.close_code) == ("protocol_error", close_code)
    assert ending(rig) == {"t": "end", "reason": "protocol_error", "exit_code": None}


@pytest.mark.parametrize(
    "message",
    [
        {"t": "activity"},
        {"t": "ack", "n": 1},
        {"t": "resize", "cols": 1, "rows": 1},
        {"t": "resize", "cols": 1000, "rows": 500},
    ],
    ids=["activity", "ack", "smallest-size", "largest-size"],
)
async def test_the_accepted_control_messages_do_not_end_the_session(
    message: dict[str, Any],
) -> None:
    rig = build_rig()
    rig.peer.say(message)
    rig.peer.hang_up()

    end = await rig.run()

    assert end.reason == "client_closed"


async def test_a_resize_message_is_forwarded_to_the_exec() -> None:
    rig = build_rig()
    rig.peer.say({"t": "resize", "cols": 120, "rows": 40})
    rig.peer.hang_up()
    run = asyncio.create_task(rig.run())
    await until(lambda: bool(rig.client.resizes))

    await run

    assert rig.client.resizes == [Resized("exec", 40, 120)]


async def test_resizes_in_a_burst_are_coalesced_to_the_latest_size() -> None:
    limits = TerminalLimits(resize_interval=QUICK)
    rig = build_rig(limits=limits)
    rig.peer.say({"t": "resize", "cols": 100, "rows": 30})
    run = asyncio.create_task(rig.run())
    await until(lambda: len(rig.client.resizes) == 1)
    rig.peer.say_all(*resize_messages(BURST_WIDTHS))

    await until(lambda: len(rig.client.resizes) == 2)
    rig.peer.hang_up()
    await run

    assert rig.client.resizes == [Resized("exec", 30, 100), Resized("exec", 30, 104)]


async def test_output_stops_at_the_window_until_the_browser_acknowledges_it() -> None:
    rig = build_rig(limits=TerminalLimits(window=WINDOW))
    rig.hijack.feed(b"o" * (4 * WINDOW))
    run = asyncio.create_task(rig.run())
    await until(lambda: len(rig.peer.output) >= WINDOW - OUTPUT_CHUNK_BYTES)
    await settle()
    held_at = len(rig.peer.output)

    rig.peer.say({"t": "ack", "n": held_at})
    await until(lambda: len(rig.peer.output) > held_at)
    rig.peer.hang_up()
    await run

    assert held_at <= WINDOW
    assert rig.bridge.peak_unacknowledged <= WINDOW


async def test_a_large_burst_with_acknowledgements_arrives_whole_within_the_window() -> None:
    rig = build_rig(limits=TerminalLimits(window=WINDOW))
    rig.peer.acknowledge_every = ACK_STEP
    rig.hijack.feed(b"x" * BURST_BYTES)
    rig.hijack.finish()

    end = await rig.run()

    assert end.reason == "process_exited"
    assert len(rig.peer.output) == BURST_BYTES
    assert rig.bridge.peak_unacknowledged <= WINDOW


async def test_a_terminal_with_no_input_is_warned_and_then_closed_as_idle() -> None:
    rig = build_rig(limits=TerminalLimits(idle=0.2, idle_warning=0.1, recheck=60))

    end = await rig.run()

    assert (end.reason, end.close_code) == ("idle_timeout", IDLE)
    assert rig.peer.texts == [
        READY,
        {"t": "notice", "code": "idle_warning"},
        {"t": "end", "reason": "idle_timeout", "exit_code": None},
    ]
    assert rig.ends == [("idle_timeout", None, False)]


async def feed_every(rig: Rig, message: bytes | dict[str, Any], count: int, spacing: float) -> None:
    for _ in range(count):
        await asyncio.sleep(spacing)
        rig.peer.say(message)


async def test_activity_messages_keep_an_otherwise_silent_terminal_open() -> None:
    rig = build_rig(limits=TerminalLimits(idle=0.3, idle_warning=0.2, recheck=60))
    feeder = asyncio.create_task(feed_every(rig, {"t": "activity"}, count=5, spacing=0.1))

    end = await rig.run()

    assert end.reason == "idle_timeout"
    assert feeder.done()


@pytest.mark.parametrize(
    "traffic",
    [b"keystroke", {"t": "ack", "n": 1}, {"t": "resize", "cols": 90, "rows": 30}],
    ids=["binary-input", "acknowledgements", "resizes"],
)
async def test_binary_input_acknowledgements_and_resizes_do_not_count_as_activity(
    traffic: bytes | dict[str, Any],
) -> None:
    rig = build_rig(limits=TerminalLimits(idle=0.3, idle_warning=0.2, recheck=60))
    feeder = asyncio.create_task(feed_every(rig, traffic, count=8, spacing=0.1))

    end = await rig.run()
    still_feeding = not feeder.done()
    feeder.cancel()

    assert end.reason == "idle_timeout"
    assert still_feeding


async def test_a_session_is_closed_at_its_maximum_duration() -> None:
    rig = build_rig(limits=TerminalLimits(max_duration=QUICK, recheck=60))

    end = await rig.run()

    assert (end.reason, end.close_code) == ("max_duration", DURATION)
    assert ending(rig) == {"t": "end", "reason": "max_duration", "exit_code": None}


async def test_the_session_is_touched_at_a_recheck_only_after_the_browser_showed_activity() -> None:
    limits = TerminalLimits(recheck=QUICK)
    rig = build_rig(limits=limits, verdicts=["permitted", "permitted", "session_expired"])
    rig.peer.say({"t": "activity"})

    end = await rig.run()

    assert (end.reason, end.close_code) == ("session_expired", ACCESS_ENDED)
    assert rig.rechecks == [True, False, False]


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [("access_revoked", "access_revoked"), ("session_expired", "session_expired")],
)
async def test_a_failed_recheck_ends_the_session_and_says_why(verdict: str, reason: str) -> None:
    rig = build_rig(limits=TerminalLimits(recheck=QUICK), verdicts=[verdict])

    end = await rig.run()

    assert (end.reason, end.close_code) == (reason, ACCESS_ENDED)
    assert ending(rig) == {"t": "end", "reason": reason, "exit_code": None}


async def test_a_recheck_that_keeps_failing_to_answer_closes_the_session_as_unverified() -> None:
    limits = TerminalLimits(recheck=QUICK, recheck_failures=3)
    rig = build_rig(limits=limits, verdicts=[OSError("db busy")])

    end = await rig.run()

    assert (end.reason, end.close_code) == ("access_unverified", ACCESS_ENDED)
    assert ending(rig) == {"t": "end", "reason": "access_unverified", "exit_code": None}
    assert len(rig.rechecks) == 3


async def test_a_recheck_that_answers_again_resets_the_count_of_failures() -> None:
    limits = TerminalLimits(recheck=QUICK, recheck_failures=3)
    failing = OSError("db busy")
    rig = build_rig(
        limits=limits,
        verdicts=[failing, failing, "permitted", failing, failing, "session_expired"],
    )

    end = await rig.run()

    assert end.reason == "session_expired"
    assert len(rig.rechecks) == 6


async def test_activity_seen_before_a_failed_recheck_still_extends_the_session_at_the_next() -> (
    None
):
    limits = TerminalLimits(recheck=QUICK, recheck_failures=3)
    rig = build_rig(limits=limits, verdicts=[OSError("db busy"), "session_expired"])
    rig.peer.say({"t": "activity"})

    await rig.run()

    assert rig.rechecks == [True, True]


async def test_an_engine_stream_that_resets_ends_the_session_as_an_engine_error() -> None:
    rig = build_rig()
    rig.hijack.fail(ConnectionResetError())

    end = await rig.run()

    assert (end.reason, end.close_code) == ("engine_error", ERROR)


async def test_an_engine_stream_that_resets_after_the_client_closed_is_an_environment_close() -> (
    None
):
    rig = build_rig()
    rig.client.closed = True
    rig.hijack.fail(ConnectionResetError())

    end = await rig.run()

    assert (end.reason, end.close_code) == ("environment_closed", NORMAL)


async def test_a_failed_write_to_the_engine_ends_the_session_as_an_engine_error() -> None:
    rig = build_rig()
    rig.hijack.write_error = BrokenPipeError()
    rig.peer.say(b"x")

    end = await rig.run()

    assert (end.reason, end.close_code) == ("engine_error", ERROR)


async def test_a_browser_that_never_takes_the_farewell_cannot_hold_the_session_open() -> None:
    rig = build_rig(limits=TerminalLimits(max_duration=QUICK, recheck=60))
    rig.peer.hang_on_end = True

    end = await rig.run()

    assert end.reason == "max_duration"
    assert rig.slots.open_count == 0
    assert rig.ends == [("max_duration", None, False)]


async def test_cancelling_a_running_session_twice_still_releases_and_records_once() -> None:
    rig = build_rig()
    run = asyncio.create_task(rig.run())
    await settle()

    run.cancel()
    await settle(3)
    run.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run
    assert rig.slots.open_count == 0
    assert rig.hijack.closed
    assert len(rig.ends) == 1


async def test_a_bridge_that_will_never_run_is_abandoned_once_and_recorded_as_interrupted() -> None:
    rig = build_rig()

    await rig.bridge.abandon()
    await rig.bridge.abandon()

    assert rig.ends == [("interrupted", None, False)]
    assert rig.slots.open_count == 0
    assert rig.hijack.closed


async def test_a_bridge_that_was_abandoned_or_has_run_refuses_to_run_again() -> None:
    abandoned = build_rig()
    await abandoned.bridge.abandon()
    ran = build_rig()
    ran.peer.hang_up()
    await ran.run()

    with pytest.raises(RuntimeError):
        await abandoned.run()
    with pytest.raises(RuntimeError):
        await ran.run()


async def test_an_open_that_completes_hands_back_its_bridge() -> None:
    rig = build_rig()

    async def opening() -> TerminalBridge:
        await settle(3)
        return rig.bridge

    assert await open_guarded(opening()) is rig.bridge


async def test_an_open_that_fails_raises_its_own_error() -> None:
    async def opening() -> TerminalBridge:
        raise LookupError

    with pytest.raises(LookupError):
        await open_guarded(opening())


async def test_an_open_cancelled_while_it_runs_finishes_and_abandons_what_it_opened() -> None:
    rig = build_rig()
    opened = asyncio.Event()

    async def opening() -> TerminalBridge:
        await opened.wait()
        return rig.bridge

    waiting = asyncio.create_task(open_guarded(opening()))
    await settle()
    waiting.cancel()
    await settle(3)
    opened.set()

    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert rig.ends == [("interrupted", None, False)]
    assert rig.slots.open_count == 0
