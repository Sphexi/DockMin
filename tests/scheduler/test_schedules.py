"""Schedule parsing and next-fire evaluation: grammar, refusals, month ends, DST and intervals."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.scheduler.schedules import (
    MAX_CRON_CHARS,
    MAX_INTERVAL_SECONDS,
    MIN_RULE_INTERVAL_SECONDS,
    CronSchedule,
    IntervalSchedule,
    ScheduleInvalid,
    WebhookSchedule,
    floor_seconds,
    next_grid_point,
    parse_schedule,
)

LONDON = "Europe/London"
NEW_YORK = "America/New_York"
LORD_HOWE = "Australia/Lord_Howe"
SNAPSHOT_FLOOR = 15


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def next_fire(expression: str, zone: str, moment: datetime) -> datetime:
    return parse_schedule("cron", expression, zone).next_after(moment)


def fires(expression: str, zone: str, start: datetime, count: int) -> list[datetime]:
    schedule = parse_schedule("cron", expression, zone)
    found: list[datetime] = []
    moment = start
    for _ in range(count):
        moment = schedule.next_after(moment)
        found.append(moment)
    return found


def fields_of(kind: str, expression: str, zone: str = "UTC", **options: int) -> set[str]:
    with pytest.raises(ScheduleInvalid) as raised:
        parse_schedule(kind, expression, zone, **options)
    return set(raised.value.fields)


ACCEPTED_CRON = [
    "* * * * *",
    "*/15 * * * *",
    "0 3 * * *",
    "5,10,15 1-5 * * mon-fri",
    "0 0 1 JAN *",
    "0 0 * * 7",
    "0 0 * * 0-7",
    "10/20 * * * *",
    "0-30/10 * * * *",
    "0 0 29 2 *",
    "@Daily",
    "@hourly",
    "@weekly",
    "@monthly",
    "@yearly",
    "@annually",
    "@midnight",
    "  0   3  *  *  *  ",
    "0 0 13 * fri",
    "0 0 * */3 sun",
]
REFUSED_CRON = [
    pytest.param("", id="empty"),
    pytest.param("* * * *", id="four-fields"),
    pytest.param("* * * * * *", id="six-fields"),
    pytest.param("60 * * * *", id="minute-60"),
    pytest.param("* 24 * * *", id="hour-24"),
    pytest.param("* * 0 * *", id="day-of-month-0"),
    pytest.param("* * 32 * *", id="day-of-month-32"),
    pytest.param("* * * 13 *", id="month-13"),
    pytest.param("* * * * 8", id="day-of-week-8"),
    pytest.param("5-1 * * * *", id="backwards-range"),
    pytest.param("*/0 * * * *", id="zero-step"),
    pytest.param("*/60 * * * *", id="step-over-field"),
    pytest.param("*/x * * * *", id="non-numeric-step"),
    pytest.param("1,,2 * * * *", id="empty-list-item"),
    pytest.param("? * * * *", id="question-mark"),
    pytest.param("* * L * *", id="last-day"),
    pytest.param("* * 15W * *", id="nearest-weekday"),
    pytest.param("* * * * 5#2", id="nth-weekday"),
    pytest.param("* * * * 5L", id="last-weekday"),
    pytest.param("@reboot", id="reboot"),
    pytest.param("@nonsense", id="unknown-macro"),
    pytest.param("0 0 31 2 *", id="february-31st-never-fires"),
    pytest.param("0 0 30 2 *", id="february-30th-never-fires"),
    pytest.param("0 0 31 4 *", id="april-31st-never-fires"),
    pytest.param("FOO * * * *", id="unknown-minute-name"),
    pytest.param("* * * FOO *", id="unknown-month-name"),
    pytest.param("* * * * FUN", id="unknown-weekday-name"),
    pytest.param("0 0 0 0 0", id="zeroes"),
    pytest.param("1-2-3 * * * *", id="double-range"),
    pytest.param("-1 * * * *", id="negative"),
    pytest.param("1.5 * * * *", id="fraction"),
    pytest.param("x" * (MAX_CRON_CHARS + 1), id="too-long"),
]


@pytest.mark.parametrize("expression", ACCEPTED_CRON)
def test_a_valid_cron_expression_parses(expression: str) -> None:
    schedule = parse_schedule("cron", expression, LONDON)

    assert schedule.kind == "cron"
    assert schedule.time_zone == LONDON


@pytest.mark.parametrize("expression", REFUSED_CRON)
def test_an_invalid_cron_expression_is_refused_under_the_schedule_key(expression: str) -> None:
    assert fields_of("cron", expression) == {"schedule"}


def test_a_cron_expression_with_extra_whitespace_is_stored_collapsed() -> None:
    assert parse_schedule("cron", "  0   3  *  *  * ").expression == "0 3 * * *"


@pytest.mark.parametrize(
    ("expression", "attribute", "wanted"),
    [
        pytest.param("0 0 * * 7", "days_of_week", {0}, id="sunday-as-seven"),
        pytest.param("0 0 * * 5-7", "days_of_week", {0, 5, 6}, id="weekday-range-through-seven"),
        pytest.param("0 0 * * */2", "days_of_week", {0, 2, 4, 6}, id="weekday-step"),
        pytest.param("0 0 * */3 sun", "months", {1, 4, 7, 10}, id="month-step"),
        pytest.param("5 1 * * mon-fri", "days_of_week", {1, 2, 3, 4, 5}, id="weekday-names"),
    ],
)
def test_cron_fields_expand_to_the_values_they_name(
    expression: str, attribute: str, wanted: set[int]
) -> None:
    assert set(getattr(parse_schedule("cron", expression), attribute)) == wanted


def cron_schedule(expression: str) -> CronSchedule:
    return parse_schedule("cron", expression)


def test_a_start_with_a_step_runs_to_the_end_of_the_field() -> None:
    schedule = cron_schedule("10/20 * * * *")

    assert sorted({minute % 60 for minute in schedule.minutes_of_day}) == [10, 30, 50]


def test_a_macro_is_the_expression_it_stands_for() -> None:
    assert cron_schedule("@daily").minutes_of_day == cron_schedule("0 0 * * *").minutes_of_day


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        pytest.param(utc(2026, 1, 1, 0, 0, 0), utc(2026, 1, 1, 0, 1), id="next-minute"),
        pytest.param(utc(2026, 1, 1, 0, 0, 59), utc(2026, 1, 1, 0, 1), id="within-the-minute"),
    ],
)
def test_an_every_minute_cron_fires_at_the_next_whole_minute(
    moment: datetime, expected: datetime
) -> None:
    assert next_fire("* * * * *", "UTC", moment) == expected


@pytest.mark.parametrize(
    ("expression", "moment", "expected"),
    [
        pytest.param(
            "0 3 * * *", utc(2026, 1, 1, 3, 0, 0), utc(2026, 1, 2, 3, 0), id="strictly-after"
        ),
        pytest.param(
            "0 0 31 * *", utc(2026, 1, 31, 0, 0), utc(2026, 3, 31, 0, 0), id="skips-short-months"
        ),
        pytest.param("0 0 1 1 *", utc(2026, 6, 1), utc(2027, 1, 1), id="year-rollover"),
        pytest.param("0 0 29 2 *", utc(2025, 3, 1), utc(2028, 2, 29), id="leap-day"),
        pytest.param(
            "0 0 29 2 *", utc(2096, 3, 1), utc(2104, 2, 29), id="leap-day-over-a-skipped-century"
        ),
        pytest.param("0 0 29 2 *", utc(2024, 2, 28, 12), utc(2024, 2, 29), id="leap-day-same-year"),
        pytest.param(
            "59 23 31 12 *",
            utc(2026, 12, 31, 23, 59),
            utc(2027, 12, 31, 23, 59),
            id="new-years-eve",
        ),
        pytest.param(
            "0 0 28-31 2 *", utc(2026, 2, 28, 0, 0), utc(2027, 2, 28, 0, 0), id="february-range"
        ),
        pytest.param(
            "0 0 13 * fri", utc(2026, 2, 2), utc(2026, 2, 6), id="day-or-weekday-friday-first"
        ),
        pytest.param(
            "0 0 13 * fri", utc(2026, 2, 12), utc(2026, 2, 13), id="day-or-weekday-the-13th-first"
        ),
        pytest.param("0 9 * * 1", utc(2026, 10, 22), utc(2026, 10, 26, 9), id="monday"),
        pytest.param(
            "0 0 * * */2", utc(2026, 10, 22), utc(2026, 10, 24), id="unrestricted-day-ands-weekday"
        ),
        pytest.param(
            "0 0 */2 * fri", utc(2026, 10, 1), utc(2026, 10, 9), id="star-prefixed-day-ands-weekday"
        ),
        pytest.param("@weekly", utc(2026, 10, 22), utc(2026, 10, 25), id="weekly-is-sunday"),
        pytest.param("@monthly", utc(2026, 10, 22), utc(2026, 11, 1), id="monthly"),
        pytest.param("@yearly", utc(2026, 10, 22), utc(2027, 1, 1), id="yearly"),
        pytest.param("@hourly", utc(2026, 10, 22, 10, 30), utc(2026, 10, 22, 11, 0), id="hourly"),
        pytest.param(
            "0,30 * * * *", utc(2026, 10, 22, 10, 30), utc(2026, 10, 22, 11, 0), id="list"
        ),
        pytest.param("0 0 1 jun *", utc(2026, 7, 1), utc(2027, 6, 1), id="named-month"),
    ],
)
def test_next_after_finds_the_earliest_matching_fire(
    expression: str, moment: datetime, expected: datetime
) -> None:
    assert next_fire(expression, "UTC", moment) == expected


def test_next_after_drops_sub_second_precision() -> None:
    moment = utc(2026, 1, 1, 2, 59, 59).replace(microsecond=999_999)

    assert next_fire("0 3 * * *", "UTC", moment) == utc(2026, 1, 1, 3, 0)


def test_next_after_converts_an_aware_moment_in_another_zone() -> None:
    moment = datetime(2026, 1, 1, 5, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    assert next_fire("0 3 * * *", "UTC", moment) == utc(2026, 1, 1, 3, 0)


def test_a_naive_moment_is_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        parse_schedule("cron", "* * * * *").next_after(datetime(2026, 1, 1))


def test_floor_seconds_refuses_a_naive_moment() -> None:
    with pytest.raises(ValueError, match="aware"):
        floor_seconds(datetime(2026, 1, 1))


@pytest.mark.parametrize(
    ("expression", "zone", "moment", "expected"),
    [
        pytest.param(
            "30 1 * * *",
            LONDON,
            utc(2026, 3, 28, 12),
            utc(2026, 3, 29, 1, 0),
            id="london-gap-fires-at-the-gap-end",
        ),
        pytest.param(
            "30 1 * * *",
            LONDON,
            utc(2026, 3, 29, 1, 0),
            utc(2026, 3, 30, 0, 30),
            id="london-day-after-the-gap",
        ),
        pytest.param(
            "0 2 * * *",
            LONDON,
            utc(2026, 3, 28, 12),
            utc(2026, 3, 29, 1, 0),
            id="london-second-wall-hour-after-the-gap",
        ),
        pytest.param(
            "30 1 * * *",
            LONDON,
            utc(2026, 3, 27, 12),
            utc(2026, 3, 28, 1, 30),
            id="london-day-before-the-gap",
        ),
        pytest.param(
            "30 1 * * *",
            LONDON,
            utc(2026, 10, 24, 12),
            utc(2026, 10, 25, 0, 30),
            id="london-fold-first-occurrence",
        ),
        pytest.param(
            "30 1 * * *",
            LONDON,
            utc(2026, 10, 25, 0, 30),
            utc(2026, 10, 26, 1, 30),
            id="london-fold-not-again",
        ),
        pytest.param(
            "30 1 * * *",
            LONDON,
            utc(2026, 10, 25, 1, 30),
            utc(2026, 10, 26, 1, 30),
            id="london-fold-from-inside-the-repeat",
        ),
        pytest.param(
            "30 0 * * *",
            LONDON,
            utc(2026, 10, 24, 12),
            utc(2026, 10, 24, 23, 30),
            id="london-before-the-fold",
        ),
        pytest.param(
            "30 2 * * *", NEW_YORK, utc(2026, 3, 7, 12), utc(2026, 3, 8, 7, 0), id="new-york-gap"
        ),
        pytest.param(
            "30 2 * * *",
            NEW_YORK,
            utc(2026, 3, 8, 7, 0),
            utc(2026, 3, 9, 6, 30),
            id="new-york-day-after-the-gap",
        ),
        pytest.param(
            "0 3 * * *",
            NEW_YORK,
            utc(2026, 3, 7, 12),
            utc(2026, 3, 8, 7, 0),
            id="new-york-gap-end-is-three",
        ),
        pytest.param(
            "30 1 * * *",
            NEW_YORK,
            utc(2026, 10, 31, 12),
            utc(2026, 11, 1, 5, 30),
            id="new-york-fold-first-occurrence",
        ),
        pytest.param(
            "30 1 * * *",
            NEW_YORK,
            utc(2026, 11, 1, 5, 30),
            utc(2026, 11, 2, 6, 30),
            id="new-york-fold-not-again",
        ),
        pytest.param(
            "30 2 * * *",
            NEW_YORK,
            utc(2026, 10, 31, 12),
            utc(2026, 11, 1, 7, 30),
            id="new-york-after-the-fold",
        ),
        pytest.param(
            "0 3 * * *", NEW_YORK, utc(2026, 1, 10), utc(2026, 1, 10, 8, 0), id="new-york-winter"
        ),
        pytest.param(
            "0 3 * * *", NEW_YORK, utc(2026, 7, 10), utc(2026, 7, 10, 7, 0), id="new-york-summer"
        ),
        pytest.param(
            "10 2 * * *",
            LORD_HOWE,
            utc(2026, 10, 3, 0),
            utc(2026, 10, 3, 15, 30),
            id="lord-howe-half-hour-gap",
        ),
        pytest.param(
            "30 2 * * *",
            LORD_HOWE,
            utc(2026, 10, 3, 12),
            utc(2026, 10, 3, 15, 30),
            id="lord-howe-gap-end",
        ),
        pytest.param(
            "59 1 * * *",
            LORD_HOWE,
            utc(2026, 10, 3, 12),
            utc(2026, 10, 3, 15, 29),
            id="lord-howe-before-the-gap",
        ),
        pytest.param(
            "45 1 * * *",
            LORD_HOWE,
            utc(2026, 4, 4, 0),
            utc(2026, 4, 4, 14, 45),
            id="lord-howe-fold-first-occurrence",
        ),
        pytest.param(
            "45 1 * * *",
            LORD_HOWE,
            utc(2026, 4, 4, 14, 45),
            utc(2026, 4, 5, 15, 15),
            id="lord-howe-fold-not-again",
        ),
        pytest.param(
            "15 1 * * *",
            LORD_HOWE,
            utc(2026, 4, 4, 0),
            utc(2026, 4, 4, 14, 15),
            id="lord-howe-not-ambiguous",
        ),
        pytest.param(
            "0 2 * * *",
            LORD_HOWE,
            utc(2026, 4, 4, 0),
            utc(2026, 4, 4, 15, 30),
            id="lord-howe-after-the-fold",
        ),
        pytest.param(
            "0 3 * * *", LORD_HOWE, utc(2026, 6, 1), utc(2026, 6, 1, 16, 30), id="lord-howe-winter"
        ),
    ],
)
def test_a_cron_fire_follows_the_zones_wall_clock_through_daylight_saving(
    expression: str, zone: str, moment: datetime, expected: datetime
) -> None:
    assert next_fire(expression, zone, moment) == expected


def test_two_wall_times_inside_a_gap_fire_once_at_its_end() -> None:
    fired = fires("0,30 1 * * *", LONDON, utc(2026, 3, 29, 0, 0), 2)

    assert fired == [utc(2026, 3, 29, 1, 0), utc(2026, 3, 30, 0, 0)]


def test_two_wall_times_inside_a_new_york_gap_fire_once_at_its_end() -> None:
    fired = fires("5,55 2 * * *", NEW_YORK, utc(2026, 3, 8, 6, 0), 2)

    assert fired == [utc(2026, 3, 8, 7, 0), utc(2026, 3, 9, 6, 5)]


def test_two_wall_times_inside_a_lord_howe_gap_fire_once_at_its_end() -> None:
    fired = fires("0,29 2 * * *", LORD_HOWE, utc(2026, 10, 3, 12), 2)

    assert fired == [utc(2026, 10, 3, 15, 30), utc(2026, 10, 4, 15, 0)]


def test_an_hourly_cron_skips_the_repeated_hour_in_london() -> None:
    assert fires("0 * * * *", LONDON, utc(2026, 10, 24, 22), 3) == [
        utc(2026, 10, 24, 23),
        utc(2026, 10, 25, 0),
        utc(2026, 10, 25, 2),
    ]


def test_an_hourly_cron_skips_the_repeated_hour_in_new_york() -> None:
    assert fires("0 * * * *", NEW_YORK, utc(2026, 11, 1, 3), 4) == [
        utc(2026, 11, 1, 4),
        utc(2026, 11, 1, 5),
        utc(2026, 11, 1, 7),
        utc(2026, 11, 1, 8),
    ]


def test_a_half_hourly_cron_crosses_the_london_gap_without_a_missed_slot() -> None:
    assert fires("*/30 * * * *", LONDON, utc(2026, 3, 29, 0, 0), 4) == [
        utc(2026, 3, 29, 0, 30),
        utc(2026, 3, 29, 1, 0),
        utc(2026, 3, 29, 1, 30),
        utc(2026, 3, 29, 2, 0),
    ]


def test_a_quarter_hourly_cron_crosses_the_lord_howe_half_hour_gap() -> None:
    assert fires("*/15 * * * *", LORD_HOWE, utc(2026, 10, 3, 15, 0), 3) == [
        utc(2026, 10, 3, 15, 15),
        utc(2026, 10, 3, 15, 30),
        utc(2026, 10, 3, 15, 45),
    ]


def fires_on_local_day(zone: str, year: int, month: int, day: int) -> list[datetime]:
    """Return every instant a ten-minute cron fires between two local midnights."""
    zone_info = ZoneInfo(zone)
    start = datetime(year, month, day, tzinfo=zone_info).astimezone(UTC)
    following = datetime(year, month, day, tzinfo=zone_info) + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=zone_info).astimezone(UTC)
    schedule = parse_schedule("cron", "*/10 * * * *", zone)
    found: list[datetime] = []
    moment = schedule.next_after(start - timedelta(seconds=1))
    while moment < end:
        found.append(moment)
        moment = schedule.next_after(moment)
    return found


SLOTS_IN_A_SKIPPED_HOUR_DAY = 138
SLOTS_IN_A_SKIPPED_HALF_HOUR_DAY = 141
SLOTS_IN_A_NORMAL_DAY = 144


@pytest.mark.parametrize(
    ("zone", "local_day", "expected_fires"),
    [
        pytest.param(LONDON, (2026, 3, 29), SLOTS_IN_A_SKIPPED_HOUR_DAY, id="london-spring"),
        pytest.param(LONDON, (2026, 10, 25), SLOTS_IN_A_NORMAL_DAY, id="london-autumn"),
        pytest.param(NEW_YORK, (2026, 3, 8), SLOTS_IN_A_SKIPPED_HOUR_DAY, id="new-york-spring"),
        pytest.param(NEW_YORK, (2026, 11, 1), SLOTS_IN_A_NORMAL_DAY, id="new-york-autumn"),
        pytest.param(
            LORD_HOWE, (2026, 10, 4), SLOTS_IN_A_SKIPPED_HALF_HOUR_DAY, id="lord-howe-spring"
        ),
        pytest.param(LORD_HOWE, (2026, 4, 5), SLOTS_IN_A_NORMAL_DAY, id="lord-howe-autumn"),
    ],
)
def test_a_ten_minute_cron_fires_once_per_slot_that_exists_on_a_transition_day(
    zone: str, local_day: tuple[int, int, int], expected_fires: int
) -> None:
    fired = fires_on_local_day(zone, *local_day)

    assert len(fired) == expected_fires
    assert fired == sorted(set(fired))


@pytest.mark.parametrize(
    "zone",
    [
        pytest.param("UTC", id="utc"),
        pytest.param(LONDON, id="london"),
        pytest.param(NEW_YORK, id="new-york"),
        pytest.param(LORD_HOWE, id="lord-howe"),
        pytest.param("Asia/Kolkata", id="kolkata"),
        pytest.param("US/Eastern", id="alias"),
    ],
)
def test_a_known_time_zone_is_accepted(zone: str) -> None:
    assert parse_schedule("interval", "60", zone).time_zone == zone


@pytest.mark.parametrize(
    "zone",
    [
        pytest.param("Mars/Base", id="unknown"),
        pytest.param("europe/london", id="wrong-case"),
        pytest.param("../etc/passwd", id="traversal"),
        pytest.param("/etc/localtime", id="absolute-path"),
        pytest.param("", id="empty"),
        pytest.param("X" * 65, id="too-long"),
        pytest.param("localtime", id="pseudo-localtime"),
        pytest.param("Factory", id="pseudo-factory"),
        pytest.param("posixrules", id="pseudo-posixrules"),
        pytest.param("posix/Europe/London", id="posix-prefix"),
        pytest.param("right/Europe/London", id="right-prefix"),
        pytest.param("SystemV/AST4", id="systemv-prefix"),
    ],
)
def test_an_unknown_or_pseudo_time_zone_is_refused_under_the_time_zone_key(zone: str) -> None:
    assert fields_of("interval", "60", zone) == {"time_zone"}


def test_a_bad_expression_and_a_bad_zone_are_both_reported() -> None:
    assert fields_of("cron", "bad", "Nope/Zone") == {"schedule", "time_zone"}


def test_an_unknown_schedule_kind_is_refused() -> None:
    assert fields_of("weekly", "1") == {"schedule"}


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param(str(MIN_RULE_INTERVAL_SECONDS), id="floor"),
        pytest.param(str(MAX_INTERVAL_SECONDS), id="ceiling"),
        pytest.param("300", id="five-minutes"),
    ],
)
def test_an_interval_inside_the_bounds_is_accepted(expression: str) -> None:
    assert parse_schedule("interval", expression).expression == expression


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param(str(MIN_RULE_INTERVAL_SECONDS - 1), id="below-floor"),
        pytest.param(str(MAX_INTERVAL_SECONDS + 1), id="above-ceiling"),
        pytest.param("abc", id="letters"),
        pytest.param("-60", id="negative"),
        pytest.param("60.5", id="fraction"),
        pytest.param("", id="empty"),
        pytest.param(" 60", id="leading-space"),
        pytest.param("٦٠", id="non-ascii-digits"),
        pytest.param("99999999999", id="eleven-digits"),
    ],
)
def test_an_interval_outside_the_bounds_or_not_a_whole_number_is_refused(
    expression: str,
) -> None:
    assert fields_of("interval", expression) == {"schedule"}


def test_a_subsystem_can_lower_the_interval_floor() -> None:
    assert parse_schedule("interval", str(SNAPSHOT_FLOOR), min_interval=SNAPSHOT_FLOOR).kind == (
        "interval"
    )
    assert fields_of("interval", str(SNAPSHOT_FLOOR - 1), min_interval=SNAPSHOT_FLOOR) == {
        "schedule"
    }


@pytest.mark.parametrize(
    ("expression", "words"),
    [
        pytest.param("60", "Every minute", id="minute"),
        pytest.param("300", "Every 5 minutes", id="minutes"),
        pytest.param("3600", "Every hour", id="hour"),
        pytest.param("5400", "Every 90 minutes", id="ninety-minutes"),
        pytest.param("90", "Every 90 seconds", id="seconds"),
        pytest.param("86400", "Every day", id="day"),
        pytest.param("172800", "Every 2 days", id="days"),
    ],
)
def test_an_interval_is_described_in_its_largest_even_unit(expression: str, words: str) -> None:
    assert parse_schedule("interval", expression).describe() == words


def test_a_cron_schedule_is_described_as_its_expression_and_zone() -> None:
    assert parse_schedule("cron", "0 3 * * *", LONDON).describe() == "0 3 * * * (Europe/London)"


def test_an_interval_fires_one_interval_after_the_moment() -> None:
    schedule = parse_schedule("interval", "300")

    assert schedule.next_after(utc(2026, 1, 1, 0, 0, 7)) == utc(2026, 1, 1, 0, 5, 7)


def test_an_interval_ignores_its_time_zone() -> None:
    schedule = parse_schedule("interval", "300", NEW_YORK)

    assert schedule.next_after(utc(2026, 3, 8, 6, 59)) == utc(2026, 3, 8, 7, 4)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        pytest.param(utc(2026, 1, 1, 0, 7, 0), utc(2026, 1, 1, 0, 10), id="keeps-the-phase"),
        pytest.param(utc(2026, 1, 1, 0, 0, 0), utc(2026, 1, 1, 0, 5), id="moment-is-the-anchor"),
        pytest.param(utc(2026, 1, 1, 0, 10, 0), utc(2026, 1, 1, 0, 15), id="strictly-after-a-slot"),
        pytest.param(utc(2025, 12, 31), utc(2025, 12, 31, 0, 5), id="before-the-anchor"),
        pytest.param(utc(2026, 1, 31, 0, 2, 30), utc(2026, 1, 31, 0, 5), id="after-long-downtime"),
    ],
)
def test_an_anchored_interval_lands_on_the_anchors_phase(
    moment: datetime, expected: datetime
) -> None:
    schedule = parse_schedule("interval", "300")
    assert isinstance(schedule, IntervalSchedule)

    assert schedule.next_after_anchored(utc(2026, 1, 1, 0, 0, 0), moment) == expected


@pytest.mark.parametrize(
    ("seconds", "moment", "expected"),
    [
        pytest.param(60, utc(2026, 1, 1, 12, 0, 0), utc(2026, 1, 1, 12, 1, 0), id="after-a-point"),
        pytest.param(60, utc(2026, 1, 1, 12, 0, 59), utc(2026, 1, 1, 12, 1, 0), id="in-a-minute"),
        pytest.param(3600, utc(2026, 1, 1, 12, 30, 0), utc(2026, 1, 1, 13, 0, 0), id="hourly"),
        pytest.param(90, utc(2026, 1, 1, 12, 0, 20), utc(2026, 1, 1, 12, 1, 30), id="ninety-first"),
        pytest.param(90, utc(2026, 1, 1, 12, 1, 30), utc(2026, 1, 1, 12, 3, 0), id="ninety-next"),
        pytest.param(7, utc(1970, 1, 1, 0, 0, 6), utc(1970, 1, 1, 0, 0, 7), id="from-the-epoch"),
        pytest.param(7, utc(1970, 1, 1, 0, 0, 7), utc(1970, 1, 1, 0, 0, 14), id="epoch-next"),
    ],
)
def test_the_next_grid_point_is_the_next_whole_multiple_of_the_interval_in_unix_time(
    seconds: int, moment: datetime, expected: datetime
) -> None:
    assert next_grid_point(moment, seconds) == expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        pytest.param(utc(2026, 1, 1, 0, 7, 0), utc(2026, 1, 1, 0, 10), id="rounds-up"),
        pytest.param(utc(2026, 1, 1, 0, 10, 0), utc(2026, 1, 1, 0, 10), id="includes-a-slot"),
        pytest.param(utc(2026, 1, 1, 0, 0, 0), utc(2026, 1, 1, 0, 0), id="includes-the-anchor"),
        pytest.param(utc(2025, 12, 31, 23, 59), utc(2026, 1, 1, 0, 0), id="before-the-anchor"),
    ],
)
def test_an_anchored_interval_can_land_on_the_moment_itself_when_it_is_a_slot(
    moment: datetime, expected: datetime
) -> None:
    schedule = parse_schedule("interval", "300")
    assert isinstance(schedule, IntervalSchedule)

    assert schedule.at_or_after_anchored(utc(2026, 1, 1, 0, 0, 0), moment) == expected


def test_a_webhook_schedule_takes_no_expression_and_reads_when_called() -> None:
    schedule = parse_schedule("webhook", "")

    assert isinstance(schedule, WebhookSchedule)
    assert (schedule.kind, schedule.expression, schedule.describe()) == (
        "webhook",
        "",
        "When called",
    )


def test_a_webhook_schedule_refuses_an_expression_and_never_computes_a_fire() -> None:
    assert fields_of("webhook", "60") == {"schedule"}
    with pytest.raises(ScheduleInvalid):
        parse_schedule("webhook", "").next_after(utc(2026, 1, 1))
