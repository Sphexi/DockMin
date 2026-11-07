"""Schedule parsing and next-fire evaluation for interval, five-field cron and on-call schedules."""

from __future__ import annotations

import functools
import re
import zoneinfo
from bisect import bisect_left
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import islice
from typing import Literal, Protocol, overload
from zoneinfo import ZoneInfo

from app.auth.errors import ValidationFailed
from app.db.repositories.scheduled_job import ScheduleKind

MIN_RULE_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 30 * 86400
MAX_CRON_CHARS = 128

_MAX_TIME_ZONE_CHARS = 64
_PSEUDO_ZONES = frozenset({"localtime", "Factory", "posixrules", "posix", "right"})
_PSEUDO_ZONE_PREFIXES = ("posix/", "right/", "SystemV/")
_SEARCH_YEARS = 40
_SATISFIABLE_FROM = date(2024, 1, 1)
_SATISFIABLE_DAYS = 5 * 366
_OFFSET_PROBE = timedelta(days=1)
_MAX_ERROR_TOKEN = 32
_ONE_SECOND = timedelta(seconds=1)
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_MONTH_NAMES = {
    name: number
    for number, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
    )
}
_WEEKDAY_NAMES = {
    name: number for number, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}
_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_UNSUPPORTED_BARE = re.compile(r"[LW]|\d+[LW]")


class ScheduleInvalid(ValidationFailed):
    """Raised for an unacceptable schedule or time zone, keyed schedule or time_zone."""


class Schedule(Protocol):
    """A parsed schedule."""

    @property
    def kind(self) -> ScheduleKind: ...

    @property
    def expression(self) -> str: ...

    @property
    def time_zone(self) -> str: ...

    def next_after(self, moment: datetime) -> datetime:
        """Return the earliest fire strictly after moment, as aware UTC in whole seconds."""
        ...

    def describe(self) -> str:
        """Return a short human description."""
        ...


class _Invalid(Exception):
    pass


def floor_seconds(moment: datetime) -> datetime:
    """Convert an aware datetime to UTC and drop sub-second precision."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("A timezone-aware datetime is required.")
    return moment.astimezone(UTC).replace(microsecond=0)


def next_grid_point(moment: datetime, seconds: int) -> datetime:
    """Return the first whole multiple of seconds in Unix time strictly after moment."""
    elapsed = int((floor_seconds(moment) - UNIX_EPOCH).total_seconds())
    return UNIX_EPOCH + timedelta(seconds=(elapsed // seconds + 1) * seconds)


@dataclass(frozen=True, slots=True)
class IntervalSchedule:
    """Fixed-interval schedule; the zone is stored but not used."""

    expression: str
    time_zone: str
    seconds: int
    kind: ScheduleKind = "interval"

    def next_after(self, moment: datetime) -> datetime:
        """Return moment plus one interval."""
        return floor_seconds(moment) + timedelta(seconds=self.seconds)

    def next_after_anchored(self, anchor: datetime, moment: datetime) -> datetime:
        """Return the earliest time after moment that is the anchor plus whole intervals.

        The anchor may lie after moment, in which case an earlier slot of the same phase can win.
        """
        anchor = floor_seconds(anchor)
        elapsed = int((floor_seconds(moment) - anchor).total_seconds())
        return anchor + timedelta(seconds=(elapsed // self.seconds + 1) * self.seconds)

    def at_or_after_anchored(self, anchor: datetime, moment: datetime) -> datetime:
        """Return the earliest time at or after moment that is the anchor plus whole intervals."""
        return self.next_after_anchored(anchor, floor_seconds(moment) - _ONE_SECOND)

    def describe(self) -> str:
        """Describe the interval in the largest unit that divides it evenly."""
        for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
            if self.seconds % size == 0:
                count = self.seconds // size
                return f"Every {unit}" if count == 1 else f"Every {count} {unit}s"
        return "Every second" if self.seconds == 1 else f"Every {self.seconds} seconds"


@dataclass(frozen=True, slots=True)
class CronSchedule:
    """Five-field cron schedule evaluated on local wall-clock minutes."""

    expression: str
    time_zone: str
    minutes_of_day: tuple[int, ...]
    months: frozenset[int]
    days_of_month: frozenset[int]
    days_of_week: frozenset[int]
    day_of_month_restricted: bool
    day_of_week_restricted: bool
    kind: ScheduleKind = "cron"

    def describe(self) -> str:
        """Return the expression with its zone."""
        return f"{self.expression} ({self.time_zone})"

    def day_matches(self, day: date) -> bool:
        """Report whether the date satisfies month, day of month and day of week."""
        if day.month not in self.months:
            return False
        of_month = day.day in self.days_of_month
        of_week = (day.weekday() + 1) % 7 in self.days_of_week
        if self.day_of_month_restricted and self.day_of_week_restricted:
            return of_month or of_week
        return of_month and of_week

    def next_after(self, moment: datetime) -> datetime:
        """Return the next fire after moment; a nonexistent local time fires when the gap ends."""
        after = floor_seconds(moment)
        zone = ZoneInfo(self.time_zone)
        local = after.astimezone(zone).replace(tzinfo=None)
        quiet = (
            _offset(after - _OFFSET_PROBE, zone)
            == _offset(after, zone)
            == _offset(after + _OFFSET_PROBE, zone)
        )
        earliest = local if quiet else local - _OFFSET_PROBE
        start_day = earliest.date()
        start_minute = earliest.hour * 60 + earliest.minute
        for day in self._matching_days(start_day):
            first = bisect_left(self.minutes_of_day, start_minute) if day == start_day else 0
            for minute_of_day in islice(self.minutes_of_day, first, None):
                wall = datetime.combine(day, time(minute_of_day // 60, minute_of_day % 60))
                instant = _wall_to_instant(wall, zone)
                if instant > after:
                    return instant
        raise ScheduleInvalid({"schedule": "This schedule has no further fire times."})

    def _matching_days(self, start: date) -> Iterator[date]:
        last_year = min(start.year + _SEARCH_YEARS, 9998)
        day = start
        while day.year <= last_year:
            if day.month not in self.months:
                day = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
                continue
            if self.day_matches(day):
                yield day
            day += timedelta(days=1)


@dataclass(frozen=True, slots=True)
class WebhookSchedule:
    """No fire times of its own: the job runs when it is called."""

    expression: str = ""
    time_zone: str = "UTC"
    kind: ScheduleKind = "webhook"

    def next_after(self, moment: datetime) -> datetime:
        """Refuse: a call arms the job, so no fire is ever computed."""
        raise ScheduleInvalid({"schedule": "A job that runs when called has no fire times."})

    def describe(self) -> str:
        """Return the fixed description."""
        return "When called"


def _offset(instant: datetime, zone: ZoneInfo) -> timedelta | None:
    return instant.astimezone(zone).utcoffset()


def _wall_to_instant(wall: datetime, zone: ZoneInfo) -> datetime:
    """Map a local wall-clock time to UTC: first occurrence when repeated, gap end when absent."""
    first = wall.replace(tzinfo=zone, fold=0)
    second = wall.replace(tzinfo=zone, fold=1)
    before, after = first.utcoffset(), second.utcoffset()
    first_utc = first.astimezone(UTC)
    if before == after:
        return first_utc
    if first_utc.astimezone(zone).replace(tzinfo=None) == wall:
        return first_utc
    low = int(second.astimezone(UTC).timestamp())
    high = int(first_utc.timestamp())
    while high - low > 1:
        middle = (low + high) // 2
        if datetime.fromtimestamp(middle, zone).utcoffset() == after:
            high = middle
        else:
            low = middle
    return datetime.fromtimestamp(high, UTC)


@functools.cache
def _known_zones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def _check_time_zone(time_zone: str) -> None:
    if (
        len(time_zone) > _MAX_TIME_ZONE_CHARS
        or time_zone in _PSEUDO_ZONES
        or time_zone.startswith(_PSEUDO_ZONE_PREFIXES)
        or time_zone not in _known_zones()
    ):
        raise _Invalid("Unknown time zone.")


def _parse_interval(expression: str, min_interval: int) -> int:
    if not re.fullmatch(r"[0-9]{1,10}", expression):
        raise _Invalid("An interval is a whole number of seconds.")
    seconds = int(expression)
    if not min_interval <= seconds <= MAX_INTERVAL_SECONDS:
        raise _Invalid(
            f"An interval must be between {min_interval} and {MAX_INTERVAL_SECONDS} seconds."
        )
    return seconds


@dataclass(frozen=True, slots=True)
class _FieldSpec:
    label: str
    low: int
    high: int
    names: Mapping[str, int]


_FIELDS = (
    _FieldSpec("minute", 0, 59, {}),
    _FieldSpec("hour", 0, 23, {}),
    _FieldSpec("day of month", 1, 31, {}),
    _FieldSpec("month", 1, 12, _MONTH_NAMES),
    _FieldSpec("day of week", 0, 7, _WEEKDAY_NAMES),
)


def _shorten(token: str) -> str:
    return token if len(token) <= _MAX_ERROR_TOKEN else token[:_MAX_ERROR_TOKEN] + "..."


def _field_value(token: str, spec: _FieldSpec) -> int:
    if not token:
        raise _Invalid(f"A value is missing in the {spec.label} field.")
    if token.isascii() and token.isdigit():
        value = int(token)
    elif token.upper() in spec.names:
        value = spec.names[token.upper()]
    elif _UNSUPPORTED_BARE.fullmatch(token) or "?" in token or "#" in token:
        raise _Invalid(f"'{_shorten(token)}' is not supported in the {spec.label} field.")
    else:
        raise _Invalid(f"'{_shorten(token)}' is not a valid value in the {spec.label} field.")
    if not spec.low <= value <= spec.high:
        raise _Invalid(f"{value} is outside {spec.low}-{spec.high} in the {spec.label} field.")
    return value


def _parse_field(text: str, spec: _FieldSpec) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(","):
        if not item:
            raise _Invalid(f"The {spec.label} field has an empty list item.")
        base, slash, step_text = item.partition("/")
        step = 1
        if slash:
            if not (step_text.isascii() and step_text.isdigit()) or int(step_text) < 1:
                raise _Invalid(
                    f"The step must be a positive whole number in the {spec.label} field."
                )
            step = int(step_text)
            if step > spec.high:
                raise _Invalid(
                    f"The step {step} is larger than {spec.high} in the {spec.label} field."
                )
        if base == "*":
            low, high = spec.low, spec.high
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            low, high = _field_value(start_text, spec), _field_value(end_text, spec)
            if low > high:
                raise _Invalid(f"The range {low}-{high} runs backwards in the {spec.label} field.")
        else:
            low = _field_value(base, spec)
            high = spec.high if slash else low
        values.update(range(low, high + 1, step))
    return frozenset(values)


def _satisfiable(
    months: frozenset[int],
    days_of_month: frozenset[int],
    days_of_week: frozenset[int],
    both_restricted: bool,
) -> bool:
    day = _SATISFIABLE_FROM
    for _ in range(_SATISFIABLE_DAYS):
        if day.month in months:
            of_month = day.day in days_of_month
            of_week = (day.weekday() + 1) % 7 in days_of_week
            if (of_month or of_week) if both_restricted else (of_month and of_week):
                return True
        day += timedelta(days=1)
    return False


def _parse_cron(expression: str, time_zone: str) -> CronSchedule:
    if len(expression) > MAX_CRON_CHARS:
        raise _Invalid(f"A cron expression is at most {MAX_CRON_CHARS} characters.")
    text = " ".join(expression.split())
    if not text:
        raise _Invalid("A cron expression is required.")
    if text.startswith("@"):
        macro = text.lower()
        if macro not in _MACROS:
            raise _Invalid(f"The macro '{_shorten(text)}' is not supported.")
        parts = _MACROS[macro].split()
        text = macro
    else:
        parts = text.split()
        if len(parts) != 5:
            raise _Invalid(
                "A cron expression has five fields: minute, hour, day of month, month, day of week."
            )
    minutes, hours, days_of_month, months, raw_weekdays = (
        _parse_field(part, spec) for part, spec in zip(parts, _FIELDS, strict=True)
    )
    days_of_week = frozenset(day % 7 for day in raw_weekdays)
    day_of_month_restricted = not parts[2].startswith("*")
    day_of_week_restricted = not parts[4].startswith("*")
    if not _satisfiable(
        months, days_of_month, days_of_week, day_of_month_restricted and day_of_week_restricted
    ):
        raise _Invalid("This expression never fires.")
    return CronSchedule(
        expression=text,
        time_zone=time_zone,
        minutes_of_day=tuple(sorted(h * 60 + m for h in hours for m in minutes)),
        months=months,
        days_of_month=days_of_month,
        days_of_week=days_of_week,
        day_of_month_restricted=day_of_month_restricted,
        day_of_week_restricted=day_of_week_restricted,
    )


@overload
def parse_schedule(
    kind: Literal["interval"],
    expression: str,
    time_zone: str = "UTC",
    *,
    min_interval: int = MIN_RULE_INTERVAL_SECONDS,
) -> IntervalSchedule: ...


@overload
def parse_schedule(
    kind: Literal["cron"],
    expression: str,
    time_zone: str = "UTC",
    *,
    min_interval: int = MIN_RULE_INTERVAL_SECONDS,
) -> CronSchedule: ...


@overload
def parse_schedule(
    kind: Literal["webhook"],
    expression: str,
    time_zone: str = "UTC",
    *,
    min_interval: int = MIN_RULE_INTERVAL_SECONDS,
) -> WebhookSchedule: ...


@overload
def parse_schedule(
    kind: str,
    expression: str,
    time_zone: str = "UTC",
    *,
    min_interval: int = MIN_RULE_INTERVAL_SECONDS,
) -> Schedule: ...


def parse_schedule(
    kind: str,
    expression: str,
    time_zone: str = "UTC",
    *,
    min_interval: int = MIN_RULE_INTERVAL_SECONDS,
) -> Schedule:
    """Validate and parse a schedule; raises ScheduleInvalid keyed schedule and time_zone."""
    problems: dict[str, str] = {}
    schedule: Schedule | None = None
    try:
        _check_time_zone(time_zone)
    except _Invalid as error:
        problems["time_zone"] = str(error)
    try:
        match kind:
            case "interval":
                seconds = _parse_interval(expression, min_interval)
                schedule = IntervalSchedule(expression, time_zone, seconds)
            case "cron":
                schedule = _parse_cron(expression, time_zone)
            case "webhook":
                if expression:
                    raise _Invalid("A job that runs when called takes no schedule expression.")
                schedule = WebhookSchedule(expression, time_zone)
            case _:
                raise _Invalid("The schedule type must be interval or cron.")
    except _Invalid as error:
        problems["schedule"] = str(error)
    if problems or schedule is None:
        raise ScheduleInvalid(problems)
    return schedule
