"""NDJSON records for log lines, shared by the log downloads and the log export."""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from app.services.access import COMPOSE_PROJECT_LABEL, SWARM_NAMESPACE_LABEL
from app.services.activity_ndjson import encode_object
from app.services.log_lines import TRUNCATION_MARKER, LogLine

LOG_SCHEMA_VERSION: Final = 2
MAX_PAYLOAD_DEPTH: Final = 32
MAX_LEVEL_CHARS: Final = 32

COMPOSE_SERVICE_LABEL: Final = "com.docker.compose.service"

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NANOS_PER_SECOND: Final = 1_000_000_000
_LEVEL_KEYS: Final = ("level", "severity", "lvl")
_UNSAFE_CHARACTER: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

type SourceKeys = Mapping[str, str | int | None]


@dataclass(frozen=True, slots=True)
class LogContext:
    """The instance, environment and host every record of a download or a run names."""

    instance_name: str
    environment_id: int
    environment_name: str
    host_name: str | None


@dataclass(frozen=True, slots=True)
class Classified:
    """How a line is written: `payload` when it is a JSON object, else `message`."""

    format: Literal["json", "text"]
    message: str | None = None
    payload: dict[str, object] | None = None
    level: str | None = None


def line_time(line: LogLine) -> str | None:
    """Return the line's time as UTC with nine fractional digits, None for a line without one."""
    if line.timestamp_ns is None:
        return None
    seconds, nanos = divmod(line.timestamp_ns, _NANOS_PER_SECOND)
    moment = _EPOCH + timedelta(seconds=seconds)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{nanos:09d}Z"


def _reject_constant(name: str) -> object:
    raise ValueError(name)


def _finite_float(text: str) -> float:
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise ValueError(text)
    return value


def _is_clean(document: dict[str, object]) -> bool:
    pending: list[tuple[object, int]] = [(document, 1)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, str):
            if _UNSAFE_CHARACTER.search(value):
                return False
            continue
        if isinstance(value, dict):
            if any(_UNSAFE_CHARACTER.search(key) for key in value):
                return False
            children: Iterable[object] = value.values()
        elif isinstance(value, list):
            children = value
        else:
            continue
        if depth > MAX_PAYLOAD_DEPTH:
            return False
        pending.extend((child, depth + 1) for child in children)
    return True


def _level_of(payload: Mapping[str, object]) -> str | None:
    for key in _LEVEL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and 1 <= len(value) <= MAX_LEVEL_CHARS:
            return value.lower()
    return None


def classify(text: str) -> Classified:
    """Return a line's `format` with its `message`, or its `payload` and `level`.

    A line is `json` only when its sanitised text parses to a top-level object nested at most
    MAX_PAYLOAD_DEPTH levels deep, with finite numbers, whose decoded keys and strings hold no
    control character other than newline, carriage return and tab, and was not cut at the line
    limit. A key repeated within an object keeps its last value.
    """
    text_form = Classified("text", message=text)
    if not text.lstrip().startswith("{") or text.endswith(TRUNCATION_MARKER):
        return text_form
    try:
        parsed = json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        return text_form
    if not isinstance(parsed, dict) or not _is_clean(parsed):
        return text_form
    return Classified("json", payload=parsed, level=_level_of(parsed))


def container_source(
    *,
    container_id: str,
    container_name: str,
    image: str,
    labels: Mapping[str, str],
) -> dict[str, str | int | None]:
    """Return the container key set: id, name, image, then the Compose project and service."""
    return {
        "container_id": container_id,
        "container_name": container_name,
        "image": image,
        "compose_project": labels.get(COMPOSE_PROJECT_LABEL),
        "compose_service": labels.get(COMPOSE_SERVICE_LABEL),
    }


def service_image(task_template: Mapping[str, object]) -> str | None:
    """Return the image the service's task template names, None when it names none."""
    container = task_template.get("ContainerSpec")
    image = container.get("Image") if isinstance(container, dict) else None
    return image if isinstance(image, str) and image else None


def service_source(
    *,
    service_id: str,
    service_name: str,
    image: str | None,
    labels: Mapping[str, str],
) -> dict[str, str | int | None]:
    """Return the service key set with `task_id` and `node_id` left null for each line to fill."""
    return {
        "service_id": service_id,
        "service_name": service_name,
        "task_id": None,
        "node_id": None,
        "image": image,
        "stack_namespace": labels.get(SWARM_NAMESPACE_LABEL),
    }


def for_task(source: SourceKeys, task_id: str | None, node_id: str | None) -> SourceKeys:
    """Return a service key set with the task and node a line names, keys in the same places."""
    return {**source, "task_id": task_id, "node_id": node_id}


def log_record(line: LogLine, context: LogContext, source: SourceKeys) -> dict[str, object]:
    """Return the record for a line: common keys, then the context, then the source's keys."""
    classified = classify(line.text)
    record: dict[str, object] = {
        "schema_version": LOG_SCHEMA_VERSION,
        "time": line_time(line),
        "stream": line.stream,
        "format": classified.format,
    }
    if classified.payload is not None:
        record["payload"] = classified.payload
        if classified.level is not None:
            record["level"] = classified.level
    else:
        record["message"] = classified.message
    record["instance_name"] = context.instance_name
    record["environment_id"] = context.environment_id
    record["environment_name"] = context.environment_name
    record["host_name"] = context.host_name
    record.update(source)
    return record


def encode_log_line(line: LogLine, context: LogContext, source: SourceKeys) -> bytes:
    """Return the line as one NDJSON record ending in a newline."""
    return encode_object(log_record(line, context, source))


def encode_notice(text: str) -> bytes:
    """Return the record that stands in for a line of text a download appends when it stops."""
    return encode_object({"schema_version": LOG_SCHEMA_VERSION, "notice": text})
