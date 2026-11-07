"""Tests for the log query parser, the resume point and the download file name."""

import calendar
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.auth.errors import ValidationFailed
from app.services.container_logs import (
    DEFAULT_PAGE_TAIL,
    DEFAULT_TAIL,
    MAX_FILTER_CHARS,
    MAX_PAGE_TAIL,
    MAX_TAIL,
    LogQuery,
    download_filename,
    resume_point,
)

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
NOW_SECONDS = calendar.timegm(NOW.timetuple())
NANOS = 1_000_000_000
CONTAINER_ID = "0123456789abcdef" * 4


def parse(**params: str) -> LogQuery:
    return LogQuery.from_params(params, now=NOW)


def failure_keys(**params: str) -> set[str]:
    with pytest.raises(ValidationFailed) as refused:
        LogQuery.from_params(params, now=NOW)
    return set(refused.value.fields)


def test_an_empty_query_selects_both_streams_follows_and_asks_for_the_default_tail() -> None:
    query = parse()

    assert query == LogQuery(
        tail=DEFAULT_TAIL,
        since_ns=None,
        until_ns=None,
        stdout=True,
        stderr=True,
        follow=True,
        timestamps=True,
        filter="",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", 0), ("1", 1), (str(MAX_TAIL), MAX_TAIL), (" 25 ", 25)],
)
def test_a_tail_within_range_is_accepted(value: str, expected: int) -> None:
    assert parse(tail=value).tail == expected


@pytest.mark.parametrize(
    "value",
    [str(MAX_TAIL + 1), "-1", "1.5", "ten", "٣", "99999999999", "all"],
    ids=["over", "negative", "fraction", "word", "non-ascii-digit", "huge", "all-not-allowed"],
)
def test_a_tail_out_of_range_is_refused_by_field(value: str) -> None:
    assert failure_keys(tail=value) == {"tail"}


def test_the_page_limits_bound_the_tail_and_all_is_allowed_only_when_asked() -> None:
    page = LogQuery.from_params({}, default_tail=DEFAULT_PAGE_TAIL, max_tail=MAX_PAGE_TAIL, now=NOW)
    everything = LogQuery.from_params({"tail": "all"}, allow_all=True, now=NOW)

    assert (page.tail, everything.tail) == (DEFAULT_PAGE_TAIL, None)
    with pytest.raises(ValidationFailed):
        LogQuery.from_params({"tail": str(MAX_PAGE_TAIL + 1)}, max_tail=MAX_PAGE_TAIL, now=NOW)


@pytest.mark.parametrize(
    ("value", "expected_ns"),
    [
        ("15m", (NOW_SECONDS - 15 * 60) * NANOS),
        ("6h", (NOW_SECONDS - 6 * 3600) * NANOS),
        ("7d", (NOW_SECONDS - 7 * 86400) * NANOS),
        ("90s", (NOW_SECONDS - 90) * NANOS),
        ("1792411201", 1_792_411_201 * NANOS),
        ("1792411201.000000001", 1_792_411_201 * NANOS + 1),
        ("1792411201.5", 1_792_411_201 * NANOS + 500_000_000),
        ("2026-10-19T11:00:00Z", (NOW_SECONDS - 3600) * NANOS),
        ("2026-10-19T11:00:00", (NOW_SECONDS - 3600) * NANOS),
        ("2026-10-19T13:00:00+02:00", (NOW_SECONDS - 3600) * NANOS),
        ("2026-10-19 11:00", (NOW_SECONDS - 3600) * NANOS),
        ("2026-10-19", (NOW_SECONDS - 12 * 3600) * NANOS),
    ],
    ids=[
        "minutes",
        "hours",
        "days",
        "seconds",
        "epoch",
        "epoch-exact-nanos",
        "epoch-fraction",
        "iso-utc",
        "iso-naive-read-as-utc",
        "iso-offset",
        "iso-space",
        "iso-date",
    ],
)
def test_since_and_until_read_every_documented_form_to_exact_nanoseconds(
    value: str, expected_ns: int
) -> None:
    assert parse(since=value).since_ns == expected_ns
    assert parse(until=value).until_ns == expected_ns


@pytest.mark.parametrize(
    "value",
    [
        "garbage",
        "5x",
        "99999d",
        "12345678901234",
        "9999-12-31T00:00:00Z",
        "1969-12-31T23:00:00Z",
        "2026-13-01",
        "m5",
        "1" * 41,
    ],
)
def test_an_unreadable_time_is_refused_by_field(value: str) -> None:
    assert failure_keys(since=value, until=value) == {"since", "until"}


def test_all_is_a_valid_since_only() -> None:
    assert parse(since="all").since_ns is None
    assert failure_keys(until="all") == {"until"}


def test_until_must_be_after_since() -> None:
    assert failure_keys(since="2026-10-19T11:00:00Z", until="2026-10-19T10:00:00Z") == {"until"}
    assert failure_keys(since="2026-10-19T11:00:00Z", until="2026-10-19T11:00:00Z") == {"until"}


def test_setting_until_turns_follow_off_even_when_follow_is_asked_for() -> None:
    assert parse(until="2026-10-19T11:00:00Z", follow="1").follow is False


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, (True, True)),
        ({"stdout": "1"}, (True, False)),
        ({"stderr": "on"}, (False, True)),
        ({"stdout": "1", "stderr": "1"}, (True, True)),
        ({"stdout": "off", "stderr": "true"}, (False, True)),
        ({"stdout": "yes", "stderr": "no"}, (True, False)),
        ({"stdout": "1", "stderr": "0"}, (True, False)),
    ],
)
def test_streams_default_to_both_and_then_to_only_those_given_as_on(
    params: dict[str, str], expected: tuple[bool, bool]
) -> None:
    query = LogQuery.from_params(params, now=NOW)

    assert (query.stdout, query.stderr) == expected


@pytest.mark.parametrize(
    "params",
    [{"stdout": "0", "stderr": "0"}, {"stdout": "off"}, {"stderr": "no"}, {"stdout": "maybe"}],
)
def test_selecting_no_stream_is_refused(params: dict[str, str]) -> None:
    with pytest.raises(ValidationFailed) as refused:
        LogQuery.from_params(params, now=NOW)

    assert set(refused.value.fields) == {"streams"}


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("follow", "0", False),
        ("follow", "off", False),
        ("follow", "1", True),
        ("follow", "unrecognised", True),
        ("timestamps", "0", False),
        ("timestamps", "false", False),
        ("timestamps", "1", True),
    ],
)
def test_flags_read_the_common_spellings_and_default_when_unrecognised(
    name: str, value: str, expected: bool
) -> None:
    assert getattr(parse(**{name: value}), name) is expected


def test_the_filter_is_kept_as_given_up_to_its_limit() -> None:
    assert parse(q="x" * MAX_FILTER_CHARS).filter == "x" * MAX_FILTER_CHARS


@pytest.mark.parametrize(
    "text", ["x" * (MAX_FILTER_CHARS + 1), "bad\nline", "nul\x00", "tab\there"]
)
def test_a_filter_that_is_too_long_or_holds_a_control_character_is_refused(text: str) -> None:
    assert failure_keys(q=text) == {"q"}


def test_every_bad_field_is_reported_together() -> None:
    assert failure_keys(tail="x", since="y", stdout="0", stderr="0", q="z" * 200) == {
        "tail",
        "since",
        "streams",
        "q",
    }


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (("100",), 100),
        (("100", "250"), 250),
        (("250", None), 250),
        (("12abc", "7"), 7),
        (("", "-5", "1.5", " 9"), None),
        (("1" * 21, "5"), 5),
        (("1" * 20,), int("1" * 20)),
        ((None, None), None),
    ],
)
def test_the_resume_point_is_the_latest_usable_nanosecond_value(
    reported: tuple[str | None, ...], expected: int | None
) -> None:
    assert resume_point(*reported) == expected


@pytest.mark.parametrize(
    ("name", "expected_prefix"),
    [
        ("web", "web-0123456789ab-"),
        ("my_app.v2", "my_app.v2-0123456789ab-"),
        ("../../etc/passwd", ".._.._etc_passwd-0123456789ab-"),
        ('quo"te\r\nInjected: header', "quo_te__Injected__header-0123456789ab-"),
        ("naïve name", "na_ve_name-0123456789ab-"),
        ("", "container-0123456789ab-"),
        ("n" * 200, "n" * 64 + "-0123456789ab-"),
    ],
    ids=["plain", "safe-punctuation", "path", "header-injection", "non-ascii", "empty", "long"],
)
def test_the_download_name_reduces_the_container_name_to_safe_characters(
    name: str, expected_prefix: str
) -> None:
    filename = download_filename(name, CONTAINER_ID, NOW)

    assert filename == f"{expected_prefix}20261019T120000Z.log"


def test_the_download_name_carries_the_utc_time_of_a_zoned_moment() -> None:
    moment = datetime(2026, 10, 19, 14, 30, 5, tzinfo=timezone(timedelta(hours=2)))

    assert download_filename("web", CONTAINER_ID, moment).endswith("-20261019T123005Z.log")
