"""Update result format: dataclass, validation, and the atomic write."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast, get_args

from updater.files import read_json_object, write_json_atomic
from updater.plan import (
    CONTAINER_ID_PATTERN,
    SCHEMA_VERSION,
    TIMESTAMP_PATTERN,
    UPDATE_ID_PATTERN,
)

Outcome = Literal["succeeded", "rolled_back", "rollback_failed", "not_started"]
FailureReason = Literal[
    "unhealthy",
    "health_timeout",
    "exited",
    "create_failed",
    "start_failed",
    "copy_failed",
    "image_changed",
    "engine_error",
]
RefusalReason = Literal[
    "previous_exists", "image_changed", "plan_invalid", "locked", "engine_error"
]
Reason = FailureReason | RefusalReason

MAX_RESULT_BYTES = 1024 * 1024
MAX_EXCERPT_LINES = 200
MAX_EXCERPT_LINE_CHARS = 1024

_OUTCOMES = frozenset(get_args(Outcome))
_FAILURE_REASONS = frozenset(get_args(FailureReason))
_REFUSAL_REASONS = frozenset(get_args(RefusalReason))


class ResultError(ValueError):
    """The result file is missing a field or holds a value outside its schema."""


@dataclass(frozen=True, slots=True)
class Result:
    """The outcome of an update attempt."""

    update_id: str
    outcome: Outcome
    reason: Reason | None
    finished_at: str
    new_container_id: str | None
    log_excerpt: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form of the result."""
        return {
            "schema_version": SCHEMA_VERSION,
            "update_id": self.update_id,
            "outcome": self.outcome,
            "reason": self.reason,
            "finished_at": self.finished_at,
            "new_container_id": self.new_container_id,
            "log_excerpt": list(self.log_excerpt),
        }


def utc_timestamp() -> str:
    """Return the current time as `YYYY-MM-DDTHH:MM:SSZ`."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_result(path: Path, result: Result) -> None:
    """Write the result file atomically with mode 0600."""
    write_json_atomic(path, result.to_dict())


def read_result(path: Path) -> Result:
    """Read and validate the result file at `path`."""
    return parse_result(read_json_object(path, MAX_RESULT_BYTES))


def parse_result(data: dict[str, Any]) -> Result:
    """Validate a decoded result; unknown keys are ignored."""
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ResultError(f"schema_version must be {SCHEMA_VERSION}")
    update_id = data.get("update_id")
    if not isinstance(update_id, str) or UPDATE_ID_PATTERN.fullmatch(update_id) is None:
        raise ResultError("update_id is malformed")
    outcome = data.get("outcome")
    if not isinstance(outcome, str) or outcome not in _OUTCOMES:
        raise ResultError("outcome is not recognised")
    reason = data.get("reason")
    allowed = {
        "succeeded": frozenset[str](),
        "not_started": _REFUSAL_REASONS,
        "rolled_back": _FAILURE_REASONS,
        "rollback_failed": _FAILURE_REASONS,
    }[outcome]
    if outcome == "succeeded":
        if reason is not None:
            raise ResultError("reason must be null for a succeeded update")
    elif not isinstance(reason, str) or reason not in allowed:
        raise ResultError("reason is not valid for the outcome")
    finished_at = data.get("finished_at")
    if not isinstance(finished_at, str) or TIMESTAMP_PATTERN.fullmatch(finished_at) is None:
        raise ResultError("finished_at is malformed")
    container_id = data.get("new_container_id")
    if container_id is not None and (
        not isinstance(container_id, str) or CONTAINER_ID_PATTERN.fullmatch(container_id) is None
    ):
        raise ResultError("new_container_id is malformed")
    excerpt = data.get("log_excerpt")
    if (
        not isinstance(excerpt, list)
        or len(excerpt) > MAX_EXCERPT_LINES
        or not all(
            isinstance(line, str) and len(line) <= MAX_EXCERPT_LINE_CHARS for line in excerpt
        )
    ):
        raise ResultError("log_excerpt is malformed")
    return Result(
        update_id=update_id,
        outcome=cast(Outcome, outcome),
        reason=cast(Reason | None, reason),
        finished_at=finished_at,
        new_container_id=container_id,
        log_excerpt=tuple(excerpt),
    )
