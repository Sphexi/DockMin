"""CSV encoding of activity events, shared by the on-demand download and the scheduled export."""

import csv
import io
import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.db.engine import Session
from app.db.repositories.activity_event import ActivityEvent, DetailValue
from app.db.repositories.environment import EnvironmentRepository
from app.db.repositories.user import UserRepository

CSV_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "occurred_at",
    "category",
    "actor_type",
    "actor_user_id",
    "actor_username",
    "actor_subsystem",
    "rule_id",
    "rule_name",
    "job_run_id",
    "request_id",
    "environment_id",
    "environment_name",
    "target_type",
    "target_id",
    "action",
    "result",
    "reason",
    "message",
    "detail",
)

type ExportFormat = Literal["csv", "ndjson"]
EXPORT_FORMATS: Final[tuple[ExportFormat, ...]] = ("csv", "ndjson")

FORMULA_PREFIXES: Final = ("=", "+", "-", "@", "\uff1d", "\uff0b", "\uff0d", "\uff20")
CONTROL_PREFIXES: Final = ("\t", "\r", "\n")


@dataclass(frozen=True, slots=True)
class ExportRow:
    """One event with the current names of its actor and environment."""

    event: ActivityEvent
    actor_username: str | None
    environment_name: str | None


async def resolve_names(
    db: Session, events: Sequence[ActivityEvent]
) -> tuple[dict[int, str], dict[int, str]]:
    """Return the current usernames and environment names the events refer to."""
    user_ids = {e.actor_user_id for e in events if e.actor_user_id is not None}
    environment_ids = {e.environment_id for e in events if e.environment_id is not None}
    usernames = await UserRepository(db).usernames(user_ids) if user_ids else {}
    names = await EnvironmentRepository(db).names(environment_ids) if environment_ids else {}
    return usernames, names


async def export_rows(db: Session, events: Sequence[ActivityEvent]) -> list[ExportRow]:
    """Pair each event with its actor's and environment's current name; a missing one is None."""
    usernames, names = await resolve_names(db, events)
    return [
        ExportRow(
            event,
            None if event.actor_user_id is None else usernames.get(event.actor_user_id),
            None if event.environment_id is None else names.get(event.environment_id),
        )
        for event in events
    ]


def neutralise(text: str) -> str:
    """Prefix a cell a spreadsheet would read as a formula, even after leading whitespace."""
    formula = text.startswith(CONTROL_PREFIXES) or text.lstrip().startswith(FORMULA_PREFIXES)
    return f"'{text}" if formula else text


def csv_header() -> bytes:
    """Return the header row."""
    return _encode([list(CSV_COLUMNS)])


def csv_rows(rows: Sequence[ExportRow]) -> bytes:
    """Return the rows in column order; blocking work for large batches, so run it off the loop."""
    return _encode([_cells(row) for row in rows])


def _encode(lines: Collection[Sequence[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n").writerows(lines)
    # A lone surrogate cannot be encoded, and one bad cell must not end the stream.
    return buffer.getvalue().encode("utf-8", errors="replace")


def _cells(row: ExportRow) -> list[str]:
    event = row.event
    detail = event.detail or {}
    return [
        str(event.id),
        _text(event.occurred_at),
        _text(event.category),
        _text(event.actor_type),
        _number(event.actor_user_id),
        _text(row.actor_username),
        _text(event.actor_subsystem),
        _number(event.rule_id),
        _text(detail.get("rule_name")),
        _number(event.job_run_id),
        _text(event.request_id),
        _number(event.environment_id),
        _text(row.environment_name),
        _text(event.target_type),
        _text(event.target_id),
        _text(event.action),
        _text(event.result),
        _text(detail.get("reason")),
        _text(detail.get("message")),
        _text(_canonical(event.detail)),
    ]


def _canonical(detail: dict[str, DetailValue] | None) -> str | None:
    if not detail:
        return None
    return json.dumps(detail, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _number(value: int | None) -> str:
    return "" if value is None else str(value)


def _text(value: DetailValue) -> str:
    match value:
        case None:
            return ""
        case bool():
            return "true" if value else "false"
        case int():
            return str(value)
        case str():
            return neutralise(value)
