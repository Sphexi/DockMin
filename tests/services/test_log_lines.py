"""Tests for app/services/log_lines.py: turning log frames into bounded, display-safe lines."""

import asyncio
import calendar
from typing import Any

import pytest

from app.docker_engine.frames import LogFrame, LogFrameDecoder
from app.services import log_lines
from app.services.log_lines import (
    INLINE_DECODE_BYTES,
    MAX_LINE_BYTES,
    REPLACEMENT,
    TRUNCATION_MARKER,
    LogLineSplitter,
    frames_size,
    parse_timestamp_ns,
    sanitise,
)
from tests.docker_engine.helpers import STDERR, STDOUT, log_frame

SECONDS = calendar.timegm((2026, 10, 19, 12, 0, 1))
NANOS_PER_SECOND = 1_000_000_000
STAMP = "2026-10-19T12:00:01.000000001Z"
STAMP_NS = SECONDS * NANOS_PER_SECOND + 1
CSI_RESET = "\x1b[0m"


def texts(lines: list[log_lines.LogLine]) -> list[tuple[str, str]]:
    return [(line.stream, line.text) for line in lines]


def in_chunks(data: bytes, size: int) -> list[bytes]:
    return [data[start : start + size] for start in range(0, len(data), size)]


def test_complete_lines_split_on_newline_and_drop_a_trailing_carriage_return() -> None:
    splitter = LogLineSplitter()

    lines = splitter.feed("stdout", b"one\r\ntwo\n\nthree\n")

    assert texts(lines) == [
        ("stdout", "one"),
        ("stdout", "two"),
        ("stdout", ""),
        ("stdout", "three"),
    ]


def test_a_partial_line_is_held_until_its_newline_arrives() -> None:
    splitter = LogLineSplitter()

    first = splitter.feed("stdout", b"par")
    second = splitter.feed("stdout", b"tial\n")

    assert first == []
    assert texts(second) == [("stdout", "partial")]


def test_each_stream_keeps_its_own_partial_line_so_interleaved_frames_never_splice() -> None:
    splitter = LogLineSplitter()

    lines = [
        *splitter.feed("stdout", b"out-"),
        *splitter.feed("stderr", b"err-"),
        *splitter.feed("stdout", b"1\n"),
        *splitter.feed("stderr", b"2\n"),
    ]

    assert texts(lines) == [("stdout", "out-1"), ("stderr", "err-2")]


def test_flush_returns_the_unterminated_last_line_of_each_stream() -> None:
    splitter = LogLineSplitter()
    splitter.feed("stdout", b"tail-out")
    splitter.feed("stderr", b"tail-err")

    lines = splitter.flush()

    assert texts(lines) == [("stdout", "tail-out"), ("stderr", "tail-err")]
    assert splitter.flush() == []


def test_stdin_frames_are_ignored() -> None:
    splitter = LogLineSplitter()
    frames = [LogFrame("stdin", b"typed\n"), LogFrame("stdout", b"shown\n")]

    lines = splitter.feed_frames(frames)

    assert texts(lines) == [("stdout", "shown")]


@pytest.mark.parametrize("chunk_size", [1, 3, 8, 13, 64, 10_000])
def test_multiplexed_frames_decode_to_the_same_lines_whatever_the_chunk_boundaries(
    chunk_size: int,
) -> None:
    wire = (
        log_frame(STDOUT, b"alpha\nbe")
        + log_frame(STDERR, b"warn\n")
        + log_frame(STDOUT, b"ta\ngamma\n")
        + log_frame(STDERR, b"oops")
    )
    decoder = LogFrameDecoder()
    splitter = LogLineSplitter()

    lines = [
        *(
            line
            for chunk in in_chunks(wire, chunk_size)
            for line in splitter.feed_frames(decoder.feed(chunk))
        ),
        *splitter.flush(),
    ]

    assert texts(lines) == [
        ("stdout", "alpha"),
        ("stderr", "warn"),
        ("stdout", "beta"),
        ("stdout", "gamma"),
        ("stderr", "oops"),
    ]


def test_a_timestamp_is_split_from_the_text_and_parsed_to_exact_nanoseconds() -> None:
    splitter = LogLineSplitter()

    (line,) = splitter.feed("stdout", f"{STAMP} hello world\n".encode())

    assert (line.timestamp, line.timestamp_ns, line.text) == (STAMP, STAMP_NS, "hello world")


@pytest.mark.parametrize(
    ("token", "expected_ns"),
    [
        ("2026-10-19T12:00:01Z ", SECONDS * NANOS_PER_SECOND),
        ("2026-10-19T12:00:01.5Z ", SECONDS * NANOS_PER_SECOND + 500_000_000),
        ("2026-10-19T12:00:01.123456789Z ", SECONDS * NANOS_PER_SECOND + 123_456_789),
        ("2026-10-19T12:00:01.000000001Z ", SECONDS * NANOS_PER_SECOND + 1),
        ("2026-10-19T14:00:01+02:00 ", SECONDS * NANOS_PER_SECOND),
        ("2026-10-19T09:30:01-02:30 ", SECONDS * NANOS_PER_SECOND),
        ("2026-10-19T12:00:01.000000001Z", None),
        ("2026-13-19T12:00:01Z ", None),
        ("2026-02-30T12:00:01Z ", None),
        ("2026-10-19T12:00:01.0000000001Z ", None),
        ("2026-10-19T12:00:01z ", None),
        ("not a timestamp ", None),
        ("", None),
    ],
)
def test_timestamp_parsing_is_exact_and_refuses_anything_malformed(
    token: str, expected_ns: int | None
) -> None:
    assert parse_timestamp_ns(token) == expected_ns


@pytest.mark.parametrize(
    "line",
    [
        b"plain text without a stamp\n",
        b"2026-13-19T12:00:01Z bad month stays in the text\n",
        b"2026-10-19T12:00:01Z\n",
    ],
    ids=["no-stamp", "unparseable-stamp", "stamp-without-text"],
)
def test_a_line_without_a_usable_timestamp_keeps_all_of_its_text(line: bytes) -> None:
    splitter = LogLineSplitter()

    (parsed,) = splitter.feed("stdout", line)

    assert (parsed.timestamp, parsed.timestamp_ns) == (None, None)
    assert parsed.text == line.decode().rstrip("\n")


def test_with_timestamps_off_the_leading_token_stays_in_the_text() -> None:
    splitter = LogLineSplitter(timestamps=False)

    (line,) = splitter.feed("stdout", f"{STAMP} hello\n".encode())

    assert (line.timestamp, line.text) == (None, f"{STAMP} hello")


def test_a_line_over_the_cap_is_cut_marked_and_the_rest_of_it_discarded() -> None:
    splitter = LogLineSplitter()
    over = b"x" * (MAX_LINE_BYTES + 100)

    first = splitter.feed("stdout", over)
    rest = splitter.feed("stdout", b"discarded tail\nnext\n")

    assert first == []
    assert [(line.text, line.truncated) for line in rest] == [
        ("x" * MAX_LINE_BYTES + TRUNCATION_MARKER, True),
        ("next", False),
    ]


def test_a_line_exactly_at_the_cap_is_not_truncated() -> None:
    splitter = LogLineSplitter()

    (line,) = splitter.feed("stdout", b"y" * MAX_LINE_BYTES + b"\n")

    assert (len(line.text), line.truncated) == (MAX_LINE_BYTES, False)


def test_a_multibyte_character_cut_by_the_cap_is_dropped_rather_than_garbled() -> None:
    splitter = LogLineSplitter()
    data = b"a" * (MAX_LINE_BYTES - 1) + "é".encode() + b"\n"

    (line,) = splitter.feed("stdout", data)

    assert line.text == "a" * (MAX_LINE_BYTES - 1) + TRUNCATION_MARKER


def test_a_stream_without_newlines_yields_one_truncated_line_however_much_arrives() -> None:
    splitter = LogLineSplitter()
    chunk = b"z" * 65_536

    during = [splitter.feed("stdout", chunk) for _ in range(64)]
    (final,) = splitter.flush()

    assert during == [[] for _ in range(64)]
    assert (final.truncated, final.text.endswith(TRUNCATION_MARKER)) == (True, True)


def test_invalid_utf8_is_replaced_not_raised() -> None:
    splitter = LogLineSplitter()

    (line,) = splitter.feed("stdout", b"ok\xff\xfe\n")

    assert line.text == f"ok{REPLACEMENT}{REPLACEMENT}"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\x1b[31mred" + CSI_RESET, "red"),
        ("\x1b[1;38;2;1;2;3mtruecolour" + CSI_RESET, "truecolour"),
        ("\x1b[2K\x1b[1Gcleared", "cleared"),
        ("\x9b31mc1 csi\x9b0m", "c1 csi"),
        ("\x1b]0;window title\x07text", "text"),
        ("\x1b]8;;http://example.test\x1b\\link\x1b]8;;\x1b\\", "link"),
        ("\x9d0;c1 osc\x07text", "text"),
        ("kept\x1b[31", "kept"),
        ("kept\x1b]0;never terminated", "kept"),
        ("tab\there", "tab\there"),
    ],
    ids=[
        "sgr",
        "truecolour",
        "erase",
        "c1-csi",
        "osc-bel",
        "osc-hyperlink",
        "c1-osc",
        "unterminated-csi",
        "unterminated-osc",
        "tab-kept",
    ],
)
def test_ansi_sequences_are_removed_from_display_text(raw: str, expected: str) -> None:
    assert sanitise(raw) == expected


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x1b", "\x7f"])
def test_other_control_characters_are_replaced(control: str) -> None:
    assert sanitise(f"a{control}b") == f"a{REPLACEMENT}b"


@pytest.mark.parametrize(
    ("payload_size", "uses_worker"),
    [(INLINE_DECODE_BYTES, False), (INLINE_DECODE_BYTES + 1, True)],
    ids=["at-threshold-inline", "over-threshold-worker"],
)
async def test_a_batch_is_decoded_in_a_worker_thread_only_above_the_inline_size(
    monkeypatch: pytest.MonkeyPatch, payload_size: int, uses_worker: bool
) -> None:
    offloaded: list[Any] = []
    real = asyncio.to_thread

    async def recording(function: Any, *args: Any) -> Any:
        offloaded.append(function)
        return await real(function, *args)

    monkeypatch.setattr(asyncio, "to_thread", recording)
    splitter = LogLineSplitter()
    frames = [LogFrame("stdout", b"x" * (payload_size - 1) + b"\n")]

    lines = await splitter.split(frames)

    assert (len(offloaded) == 1, len(lines)) == (uses_worker, 1)
    assert frames_size(frames) == payload_size
