"""Template filters that format timestamps, ages, byte sizes and durations for display."""

import unicodedata
from datetime import UTC, datetime, timedelta

from markupsafe import Markup, escape

PLACEHOLDER = "—"
_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_SECONDS_PER = (("d", 86_400), ("h", 3_600), ("m", 60), ("s", 1))

_AGE_UNITS = (("d", 86_400), ("h", 3_600), ("min", 60))
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

TimestampInput = datetime | int | float | str | None


def _as_datetime(value: datetime | int | float | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        parsed = datetime.fromtimestamp(value, UTC)
    # A naive value is taken as UTC because the datastore stores UTC without an offset.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def timestamp(value: TimestampInput, *, seconds: bool = False) -> str:
    """Format a datetime, epoch seconds or ISO 8601 string as UTC; None renders a dash."""
    if value is None or value == "":
        return PLACEHOLDER
    try:
        moment = _as_datetime(value)
    except (ValueError, OverflowError, OSError):
        # Display must not fail a whole page over one malformed value.
        return PLACEHOLDER
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC" if seconds else "%Y-%m-%d %H:%M UTC")


def timestamp_iso(value: TimestampInput) -> str:
    """Return the machine-readable form for a <time datetime> attribute, or an empty string."""
    if value is None or value == "":
        return ""
    try:
        return _as_datetime(value).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return ""


def relative_age(value: TimestampInput, *, now: datetime | None = None) -> str:
    """Format how long ago a moment was, e.g. "2 min ago"; None renders a dash."""
    if value is None or value == "":
        return PLACEHOLDER
    try:
        moment = _as_datetime(value)
    except (ValueError, OverflowError, OSError):
        return PLACEHOLDER
    elapsed = int(((now or datetime.now(UTC)) - moment).total_seconds())
    for label, size in _AGE_UNITS:
        if elapsed >= size:
            return f"{elapsed // size} {label} ago"
    return "just now"


def filesize(value: int | float | None, *, precision: int = 1) -> str:
    """Format a byte count in binary units; None renders a dash."""
    if value is None or isinstance(value, bool) or value < 0:
        return PLACEHOLDER
    size = float(value)
    unit = 0
    last = len(_BYTE_UNITS) - 1
    # Compare the rounded value, so a size that rounds up to the next unit is shown in that unit.
    while unit < last and round(size, precision) >= 1024:
        size /= 1024
        unit += 1
    if unit == 0:
        return f"{int(size)} B"
    return f"{size:.{precision}f} {_BYTE_UNITS[unit]}"


def duration(value: timedelta | int | float | None, *, parts: int = 2) -> str:
    """Format a duration as its largest `parts` non-zero units, e.g. "2h 5m"."""
    if value is None or isinstance(value, bool):
        return PLACEHOLDER
    total = int(value.total_seconds() if isinstance(value, timedelta) else value)
    if total < 0:
        return PLACEHOLDER
    if total == 0:
        return "0s"
    pieces: list[str] = []
    for label, size in _SECONDS_PER:
        count, total = divmod(total, size)
        if count:
            pieces.append(f"{count}{label}")
    return " ".join(pieces[:parts])


def visible_text(value: object) -> Markup:
    """Escape text, showing each control, format, surrogate and line-separator character as U+XXXX.

    A bidirectional override or a line break in stored text must not reorder or hide what
    follows it. The returned markup holds only escaped text and fixed spans.
    """
    text = "" if value is None else str(value)
    parts: list[Markup] = []
    run: list[str] = []
    for character in text:
        if unicodedata.category(character) in _HIDDEN_CATEGORIES:
            if run:
                parts.append(escape("".join(run)))
                run = []
            parts.append(Markup(f'<span class="visible-char">U+{ord(character):04X}</span>'))
        else:
            run.append(character)
    if run:
        parts.append(escape("".join(run)))
    return Markup("").join(parts)
