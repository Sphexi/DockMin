"""NDJSON encoding of activity events, and the choice between it and CSV for exports."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.db.repositories.activity_event import DetailValue
from app.services.activity_csv import ExportFormat, ExportRow, csv_header, csv_rows

ACTIVITY_SCHEMA_VERSION: Final = 2
NDJSON_MEDIA_TYPE: Final = "application/x-ndjson"

# Python's str.splitlines treats these as line breaks, which would split a record in two.
_LINE_BREAKS: Final = str.maketrans({0x85: "\\u0085", 0x2028: "\\u2028", 0x2029: "\\u2029"})


@dataclass(frozen=True, slots=True)
class ExportEncoding:
    """How one export format writes its header, its rows, its file name and its media type."""

    header: Callable[[], bytes]
    rows: Callable[[Sequence[ExportRow]], bytes]
    suffix: str
    media_type: str


def encode_object(record: Mapping[str, object]) -> bytes:
    """Return one JSON object as a compact UTF-8 line ending in a newline."""
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":")).translate(_LINE_BREAKS)
    # A lone surrogate cannot be encoded, and one bad value must not end the stream.
    return text.encode("utf-8", errors="replace") + b"\n"


def ndjson_rows(rows: Sequence[ExportRow]) -> bytes:
    """Return one line per row; blocking work for large batches, so run it off the loop."""
    return b"".join(encode_object(_record(row)) for row in rows)


def export_encoding(export_format: ExportFormat) -> ExportEncoding:
    """Return the encoding for a format."""
    match export_format:
        case "ndjson":
            return ExportEncoding(_no_header, ndjson_rows, ".ndjson", NDJSON_MEDIA_TYPE)
        case "csv":
            return ExportEncoding(csv_header, csv_rows, ".csv", "text/csv; charset=utf-8")


def _no_header() -> bytes:
    return b""


def _record(row: ExportRow) -> dict[str, object]:
    event = row.event
    detail = event.detail or {}
    return {
        "schema_version": ACTIVITY_SCHEMA_VERSION,
        "id": event.id,
        "occurred_at": event.occurred_at,
        "category": event.category,
        "actor_type": event.actor_type,
        "actor_user_id": event.actor_user_id,
        "actor_username": row.actor_username,
        "actor_subsystem": event.actor_subsystem,
        "rule_id": event.rule_id,
        "rule_name": _string(detail.get("rule_name")),
        "job_run_id": event.job_run_id,
        "request_id": event.request_id,
        "environment_id": event.environment_id,
        "environment_name": row.environment_name,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "action": event.action,
        "result": event.result,
        "reason": _string(detail.get("reason")),
        "message": _string(detail.get("message")),
        "detail": dict(sorted(detail.items())) or None,
    }


def _string(value: DetailValue) -> str | None:
    match value:
        case None:
            return None
        case bool():
            return "true" if value else "false"
        case _:
            return str(value)
