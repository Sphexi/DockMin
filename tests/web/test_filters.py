"""Tests for app/web/filters.py."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from markupsafe import Markup

from app.web.filters import (
    PLACEHOLDER,
    duration,
    filesize,
    relative_age,
    timestamp,
    timestamp_iso,
    visible_text,
)

EPOCH_MOMENT = "1970-01-01 00:00 UTC"
PLUS_TWO_HOURS = timezone(timedelta(hours=2))
BEYOND_DATETIME_RANGE = 10**20


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, PLACEHOLDER),
        ("", PLACEHOLDER),
        ("not a date", PLACEHOLDER),
        (BEYOND_DATETIME_RANGE, PLACEHOLDER),
        (float("nan"), PLACEHOLDER),
        (0, EPOCH_MOMENT),
        (0.9, EPOCH_MOMENT),
        ("1970-01-01T00:00:00", EPOCH_MOMENT),
        (datetime(1970, 1, 1), EPOCH_MOMENT),
        (datetime(1970, 1, 1, 2, 0, tzinfo=PLUS_TWO_HOURS), EPOCH_MOMENT),
        ("1970-01-01T02:00:00+02:00", EPOCH_MOMENT),
    ],
    ids=[
        "none",
        "empty-string",
        "malformed-string",
        "out-of-range-epoch",
        "nan",
        "epoch-int",
        "epoch-float-truncates-seconds",
        "naive-string-is-utc",
        "naive-datetime-is-utc",
        "aware-datetime-converted",
        "aware-string-converted",
    ],
)
def test_timestamp_formats_input_as_utc_or_a_dash(value: Any, expected: str) -> None:
    assert timestamp(value) == expected


def test_timestamp_with_seconds_includes_them() -> None:
    assert timestamp(59, seconds=True) == "1970-01-01 00:00:59 UTC"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("not a date", ""),
        (0, "1970-01-01T00:00:00+00:00"),
        (datetime(1970, 1, 1, 2, 0, 0, 999, tzinfo=PLUS_TWO_HOURS), "1970-01-01T00:00:00+00:00"),
    ],
    ids=["none", "empty-string", "malformed-string", "epoch", "microseconds-dropped"],
)
def test_timestamp_iso_returns_machine_form_or_empty(value: Any, expected: str) -> None:
    assert timestamp_iso(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, PLACEHOLDER),
        (-1, PLACEHOLDER),
        (True, PLACEHOLDER),
        (0, "0 B"),
        (1023, "1023 B"),
        (1023.9, "1023 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024**2, "1.0 MiB"),
        (1024**3, "1.0 GiB"),
        (1024**4, "1.0 TiB"),
        (1024**5, "1.0 PiB"),
        (1024**6, "1024.0 PiB"),
        (1024**2 - 1, "1.0 MiB"),
        (1024**3 - 1, "1.0 GiB"),
    ],
    ids=[
        "none",
        "negative",
        "bool",
        "zero",
        "just-under-a-kibibyte",
        "fractional-bytes",
        "one-kibibyte",
        "one-and-a-half-kibibytes",
        "mebibyte",
        "gibibyte",
        "tebibyte",
        "pebibyte",
        "promotes-rather-than-showing-1024-kibibytes",
        "promotes-rather-than-showing-1024-mebibytes",
        "capped-at-pebibyte",
    ],
)
def test_filesize_formats_bytes_in_binary_units(value: Any, expected: str) -> None:
    assert filesize(value) == expected


def test_filesize_honours_precision() -> None:
    assert filesize(1536, precision=2) == "1.50 KiB"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, PLACEHOLDER),
        (-1, PLACEHOLDER),
        (True, PLACEHOLDER),
        (0, "0s"),
        (59, "59s"),
        (59.9, "59s"),
        (60, "1m"),
        (61, "1m 1s"),
        (3600, "1h"),
        (3661, "1h 1m"),
        (86_400, "1d"),
        (86_405, "1d 5s"),
        (90_061, "1d 1h"),
        (timedelta(hours=2, minutes=5), "2h 5m"),
    ],
    ids=[
        "none",
        "negative",
        "bool",
        "zero",
        "seconds",
        "fractional-seconds-truncate",
        "one-minute",
        "minute-and-second",
        "one-hour",
        "parts-limit-drops-seconds",
        "one-day",
        "zero-units-skipped",
        "parts-limit-drops-minutes",
        "timedelta",
    ],
)
def test_duration_shows_the_largest_nonzero_units(value: Any, expected: str) -> None:
    assert duration(value) == expected


@pytest.mark.parametrize(("parts", "expected"), [(1, "1d"), (3, "1d 1h 1m")])
def test_duration_honours_parts(parts: int, expected: str) -> None:
    assert duration(90_061, parts=parts) == expected


AGE_NOW = datetime(2030, 1, 10, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, PLACEHOLDER),
        ("", PLACEHOLDER),
        ("not a date", PLACEHOLDER),
        ("2030-01-10T12:00:00Z", "just now"),
        ("2030-01-10T11:59:01Z", "just now"),
        ("2030-01-10T11:59:00Z", "1 min ago"),
        ("2030-01-10T11:58:00Z", "2 min ago"),
        ("2030-01-10T11:00:01Z", "59 min ago"),
        ("2030-01-10T11:00:00Z", "1 h ago"),
        ("2030-01-09T12:00:00Z", "1 d ago"),
        ("2029-12-01T12:00:00Z", "40 d ago"),
        ("2030-01-10T12:00:30Z", "just now"),
    ],
    ids=[
        "none",
        "empty",
        "malformed",
        "now",
        "under a minute",
        "one minute",
        "two minutes",
        "just under an hour",
        "one hour",
        "one day",
        "forty days",
        "in the future",
    ],
)
def test_relative_age_reports_the_largest_whole_unit_since_the_moment(
    value: str | None, expected: str
) -> None:
    assert relative_age(value, now=AGE_NOW) == expected


def hidden(code_point: str) -> str:
    return f'<span class="visible-char">U+{ord(code_point):04X}</span>'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, "", id="none"),
        pytest.param("plain", "plain", id="plain"),
        pytest.param("a<b>&\"'", "a&lt;b&gt;&amp;&#34;&#39;", id="markup-escaped"),
        pytest.param("a\u202eb", f"a{hidden(chr(0x202E))}b", id="bidirectional-override"),
        pytest.param("a\nb", f"a{hidden(chr(0x0A))}b", id="line-feed"),
        pytest.param("a\tb", f"a{hidden(chr(0x09))}b", id="tab"),
        pytest.param("a\x00b", f"a{hidden(chr(0x00))}b", id="nul"),
        pytest.param("a\u200bb", f"a{hidden(chr(0x200B))}b", id="zero-width-space"),
        pytest.param("a\u2028b", f"a{hidden(chr(0x2028))}b", id="line-separator"),
        pytest.param("a\u2029b", f"a{hidden(chr(0x2029))}b", id="paragraph-separator"),
        pytest.param("a\ud800b", f"a{hidden(chr(0xD800))}b", id="lone-surrogate"),
        pytest.param("\u202e<b>", f"{hidden(chr(0x202E))}&lt;b&gt;", id="hidden-then-escaped"),
        pytest.param("café ✓", "café ✓", id="printable-non-ascii"),
        pytest.param(42, "42", id="non-text"),
    ],
)
def test_visible_text_escapes_markup_and_shows_hidden_characters_as_their_code_points(
    value: Any, expected: str
) -> None:
    shown = visible_text(value)

    assert isinstance(shown, Markup)
    assert shown == expected
