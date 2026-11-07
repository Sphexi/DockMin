"""Tests for the incremental decoders in app/docker_engine/frames.py."""

import json
from typing import Any

import pytest

from app.docker_engine import frames
from app.docker_engine.frames import (
    FrameDecodeError,
    JsonLinesDecoder,
    LogFrame,
    LogFrameDecoder,
    TruncatedStream,
)
from tests.docker_engine.helpers import (
    FRAME_HEADER_BYTES,
    STDERR,
    STDOUT,
    feed_each_byte,
    log_frame,
)

PAYLOAD = b"hello\n"
FRAME = log_frame(STDOUT, PAYLOAD)
OBJECT_LINE = json.dumps({"read": "t", "n": 1}).encode() + b"\n"
SMALL_LIMIT = 16


@pytest.mark.parametrize(
    "cut",
    [1, FRAME_HEADER_BYTES - 1, FRAME_HEADER_BYTES, FRAME_HEADER_BYTES + 2, len(FRAME) - 1],
    ids=["one-byte", "inside-header", "after-header", "inside-payload", "last-byte-missing"],
)
def test_log_decoder_yields_nothing_for_a_partial_frame_and_reports_truncation(cut: int) -> None:
    decoder = LogFrameDecoder()

    produced = decoder.feed(FRAME[:cut])

    assert produced == []
    with pytest.raises(TruncatedStream):
        decoder.finish()


def test_log_decoder_yields_a_frame_only_when_its_last_byte_arrives() -> None:
    decoder = LogFrameDecoder()

    per_byte: list[list[LogFrame]] = feed_each_byte(decoder, FRAME)

    assert per_byte[:-1] == [[]] * (len(FRAME) - 1)
    assert per_byte[-1] == [LogFrame("stdout", PAYLOAD)]
    decoder.finish()


def test_log_decoder_yields_the_complete_frames_before_a_partial_one() -> None:
    decoder = LogFrameDecoder()

    produced = decoder.feed(FRAME + FRAME[:-1])

    assert produced == [LogFrame("stdout", PAYLOAD)]
    with pytest.raises(TruncatedStream):
        decoder.finish()


def test_log_decoder_separates_streams_and_skips_empty_frames() -> None:
    decoder = LogFrameDecoder()
    chunk = log_frame(STDOUT, b"out") + log_frame(STDOUT, b"") + log_frame(STDERR, b"err")

    produced = decoder.feed(chunk)

    assert produced == [LogFrame("stdout", b"out"), LogFrame("stderr", b"err")]


@pytest.mark.parametrize("kind", [3, 255])
def test_log_decoder_rejects_an_unknown_stream_type(kind: int) -> None:
    decoder = LogFrameDecoder()

    with pytest.raises(FrameDecodeError, match="not multiplexed"):
        decoder.feed(log_frame(kind, PAYLOAD))


def test_log_decoder_rejects_a_declared_size_over_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frames, "MAX_FRAME_BYTES", SMALL_LIMIT)
    decoder = LogFrameDecoder()

    with pytest.raises(FrameDecodeError):
        decoder.feed(log_frame(STDOUT, b"x" * (SMALL_LIMIT + 1)))


def test_log_decoder_accepts_a_frame_exactly_at_the_limit_it_was_given() -> None:
    decoder = LogFrameDecoder(max_frame_bytes=SMALL_LIMIT)

    produced = decoder.feed(log_frame(STDOUT, b"x" * SMALL_LIMIT))

    assert produced == [LogFrame("stdout", b"x" * SMALL_LIMIT)]


def test_log_decoder_refuses_an_oversize_header_before_its_body_arrives() -> None:
    decoder = LogFrameDecoder(max_frame_bytes=SMALL_LIMIT)
    header_only = log_frame(STDOUT, b"x" * (SMALL_LIMIT + 1))[:FRAME_HEADER_BYTES]

    with pytest.raises(FrameDecodeError, match="size limit"):
        decoder.feed(header_only)


@pytest.mark.parametrize(
    "cut",
    [1, len(OBJECT_LINE) // 2, len(OBJECT_LINE) - 1],
    ids=["one-byte", "mid-object", "valid-json-without-terminator"],
)
def test_json_decoder_yields_nothing_for_an_unterminated_object_and_reports_truncation(
    cut: int,
) -> None:
    decoder = JsonLinesDecoder()

    produced = decoder.feed(OBJECT_LINE[:cut])

    assert produced == []
    with pytest.raises(TruncatedStream):
        decoder.finish()


def test_json_decoder_yields_an_object_only_when_its_newline_arrives() -> None:
    decoder = JsonLinesDecoder()

    per_byte: list[list[dict[str, Any]]] = feed_each_byte(decoder, OBJECT_LINE)

    assert per_byte[:-1] == [[]] * (len(OBJECT_LINE) - 1)
    assert per_byte[-1] == [{"read": "t", "n": 1}]
    decoder.finish()


def test_json_decoder_returns_several_objects_from_one_chunk_and_skips_blank_lines() -> None:
    decoder = JsonLinesDecoder()

    produced = decoder.feed(b'{"a": 1}\n\n  \n{"a": 2}\n')

    assert produced == [{"a": 1}, {"a": 2}]
    decoder.finish()


@pytest.mark.parametrize("line", [b"not json\n", b"[1, 2]\n", b'"text"\n'])
def test_json_decoder_rejects_a_line_that_is_not_a_json_object(line: bytes) -> None:
    decoder = JsonLinesDecoder()

    with pytest.raises(FrameDecodeError):
        decoder.feed(line)


def test_json_decoder_rejects_an_unterminated_line_over_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frames, "MAX_FRAME_BYTES", SMALL_LIMIT)
    decoder = JsonLinesDecoder()

    with pytest.raises(FrameDecodeError, match="size limit"):
        decoder.feed(b"x" * (SMALL_LIMIT + 1))
