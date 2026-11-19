"""Container display names and Engine timestamps, shared across service modules."""

import re
from collections.abc import Sequence
from datetime import UTC, datetime

_ENGINE_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})")


def short_id(container_id: str) -> str:
    """Return the 12-character form shown in the interface."""
    return container_id[:12]


def display_name(names: Sequence[str], container_id: str) -> str:
    """Return the container's own name: the first without a link path, slash stripped."""
    stripped = [name.removeprefix("/") for name in names]
    for name in stripped:
        if name and "/" not in name:
            return name
    return next((name for name in stripped if name), short_id(container_id))


def parse_engine_time(text: str) -> datetime | None:
    """Parse an Engine timestamp; the zero time and anything unreadable are None."""
    match = _ENGINE_TIME.fullmatch(text.strip())
    if match is None:
        return None
    base, fraction, offset = match.groups()
    micros = (fraction or "0")[:6].ljust(6, "0")
    zone = "+00:00" if offset == "Z" else offset
    try:
        moment = datetime.fromisoformat(f"{base}.{micros}{zone}")
    except ValueError:
        return None
    return None if moment.year <= 1 else moment.astimezone(UTC)
