"""Update plan format: dataclasses, validation, and the file names inside an update directory."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from updater.files import read_json_object

SCHEMA_VERSION = 1
UPDATE_SUBDIRECTORY = "update"
PLAN_FILE = "plan.json"
RESULT_FILE = "result.json"
LOCK_FILE = ".lock"
DATABASE_BACKUP_DIRECTORY = "db-before"
PREVIOUS_SUFFIX = "-previous"

MAX_PLAN_BYTES = 4 * 1024 * 1024
MAX_STOP_TIMEOUT_SECONDS = 3600
MAX_HEALTH_TIMEOUT_SECONDS = 3600
MAX_DATABASE_FILES = 8

UPDATE_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")
API_VERSION_PATTERN = re.compile(r"\d{1,2}\.\d{1,3}")
FILE_NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")


class PlanError(ValueError):
    """The plan file is missing a field or holds a value outside its schema."""


@dataclass(frozen=True, slots=True)
class OldContainer:
    """The container being replaced."""

    id: str
    name: str
    stop_timeout: int
    image: str


@dataclass(frozen=True, slots=True)
class NewContainer:
    """The container that replaces it, created as `name` from `create_body`."""

    name: str
    image: str
    image_id: str
    create_body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Plan:
    """A validated update plan."""

    update_id: str
    created_at: str
    requested_by_user_id: int
    request_event_id: int
    engine_api_version: str
    old: OldContainer
    new: NewContainer
    health_timeout_seconds: int
    database_files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form of the plan."""
        return {
            "schema_version": SCHEMA_VERSION,
            "update_id": self.update_id,
            "created_at": self.created_at,
            "requested_by_user_id": self.requested_by_user_id,
            "request_event_id": self.request_event_id,
            "engine_api_version": self.engine_api_version,
            "old": {
                "id": self.old.id,
                "name": self.old.name,
                "stop_timeout": self.old.stop_timeout,
                "image": self.old.image,
            },
            "new": {
                "name": self.new.name,
                "image": self.new.image,
                "image_id": self.new.image_id,
                "create_body": self.new.create_body,
            },
            "health_timeout_seconds": self.health_timeout_seconds,
            "database_files": list(self.database_files),
        }


def read_plan(path: Path) -> Plan:
    """Read and validate the plan file at `path`."""
    return parse_plan(read_json_object(path, MAX_PLAN_BYTES))


def parse_plan(data: dict[str, Any]) -> Plan:
    """Validate a decoded plan; unknown keys are ignored."""
    if data.get("schema_version") != SCHEMA_VERSION:
        raise PlanError(f"schema_version must be {SCHEMA_VERSION}")
    old = _object(data, "old")
    new = _object(data, "new")
    old_name = _match(old, "name", CONTAINER_NAME_PATTERN)
    new_name = _match(new, "name", CONTAINER_NAME_PATTERN)
    if new_name != old_name:
        raise PlanError("new.name must equal old.name")
    new_image = _text(new, "image")
    create_body = _object(new, "create_body")
    if create_body.get("Image") != new_image:
        raise PlanError("new.create_body.Image must equal new.image")
    return Plan(
        update_id=_match(data, "update_id", UPDATE_ID_PATTERN),
        created_at=_match(data, "created_at", TIMESTAMP_PATTERN),
        requested_by_user_id=_integer(data, "requested_by_user_id", 1, 2**63 - 1),
        request_event_id=_integer(data, "request_event_id", 1, 2**63 - 1),
        engine_api_version=_match(data, "engine_api_version", API_VERSION_PATTERN),
        old=OldContainer(
            id=_match(old, "id", CONTAINER_ID_PATTERN),
            name=old_name,
            stop_timeout=_integer(old, "stop_timeout", 0, MAX_STOP_TIMEOUT_SECONDS),
            image=_text(old, "image"),
        ),
        new=NewContainer(
            name=new_name,
            image=new_image,
            image_id=_match(new, "image_id", IMAGE_ID_PATTERN),
            create_body=create_body,
        ),
        health_timeout_seconds=_integer(
            data, "health_timeout_seconds", 1, MAX_HEALTH_TIMEOUT_SECONDS
        ),
        database_files=_file_names(data),
    )


def _object(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise PlanError(f"{key} must be an object")
    return value


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise PlanError(f"{key} must be a non-empty string")
    return value


def _match(data: dict[str, Any], key: str, pattern: re.Pattern[str]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PlanError(f"{key} is malformed")
    return value


def _integer(data: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PlanError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def _file_names(data: dict[str, Any]) -> tuple[str, ...]:
    value = data.get("database_files")
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DATABASE_FILES:
        raise PlanError(f"database_files must list 1 to {MAX_DATABASE_FILES} file names")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or FILE_NAME_PATTERN.fullmatch(item) is None:
            raise PlanError("database_files holds a malformed file name")
        if item in names:
            raise PlanError("database_files holds a duplicate")
        names.append(item)
    return tuple(names)
