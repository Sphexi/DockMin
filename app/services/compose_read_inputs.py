"""Where a Compose project's files are, from its containers' labels, and its environment file."""

import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.services.host_files import (
    InvalidWorkingDirectory,
    check_working_directory,
    is_relative_path,
)
from app.services.stack_document import VARIABLE_NAME, is_reserved_variable
from app.services.stack_read_result import NeedsFile, ReadVariable, is_secret_entry

__all__ = [
    "CONFIG_FILES_LABEL",
    "ENVIRONMENT_FILE_LABEL",
    "MAX_CONFIG_FILES",
    "WORKING_DIR_LABEL",
    "ProjectLocation",
    "locate_project",
    "parse_environment_file",
    "variables_of",
]

WORKING_DIR_LABEL: Final = "com.docker.compose.project.working_dir"
CONFIG_FILES_LABEL: Final = "com.docker.compose.project.config_files"
ENVIRONMENT_FILE_LABEL: Final = "com.docker.compose.project.environment_file"
MAX_CONFIG_FILES: Final = 8

_LABELS: Final = (WORKING_DIR_LABEL, CONFIG_FILES_LABEL, ENVIRONMENT_FILE_LABEL)
_ASSIGNMENT: Final = re.compile(r"(?:export[ \t]+)?([^=\s]+)=(.*)")
_INLINE_COMMENT: Final = re.compile(r"\s#")
_UNQUOTED_REFUSED: Final = frozenset("$\"'\\`")
_QUOTES: Final = "'\""


@dataclass(frozen=True, slots=True)
class ProjectLocation:
    """Where a project's files lie; every path is relative to `working_dir`."""

    working_dir: str
    config_files: tuple[str, ...]
    environment_files: tuple[str, ...]


def locate_project(
    labels: Sequence[Mapping[str, str]], protects: Callable[[str], bool]
) -> ProjectLocation | NeedsFile:
    """Return the project's working directory and files, or why they cannot be read.

    `labels` holds the labels of every container of the project, which must agree.
    `protects` reports whether a working directory holds or lies inside a mount of dockmin's own.
    """
    for label in _LABELS:
        if len({item.get(label) for item in labels}) > 1:
            return NeedsFile("labels_disagree", label)
    first = labels[0] if labels else {}
    working_dir = first.get(WORKING_DIR_LABEL, "")
    config_value = first.get(CONFIG_FILES_LABEL, "")
    if not working_dir:
        return NeedsFile("labels_missing", WORKING_DIR_LABEL)
    if not config_value:
        return NeedsFile("labels_missing", CONFIG_FILES_LABEL)
    try:
        check_working_directory(working_dir)
    except InvalidWorkingDirectory as problem:
        return NeedsFile(f"working_directory_{problem.problem.value}", WORKING_DIR_LABEL)
    if protects(working_dir):
        return NeedsFile("own_mount", WORKING_DIR_LABEL)
    config_files = _relative_paths(working_dir, config_value)
    if config_files is None:
        return NeedsFile("config_file_outside", CONFIG_FILES_LABEL)
    if len(config_files) > MAX_CONFIG_FILES:
        return NeedsFile("too_many_config_files", CONFIG_FILES_LABEL)
    environment_files = _relative_paths(working_dir, first.get(ENVIRONMENT_FILE_LABEL, ""))
    if environment_files is None:
        return NeedsFile("environment_file_outside", ENVIRONMENT_FILE_LABEL)
    return ProjectLocation(working_dir, config_files, environment_files)


def _relative_paths(working_dir: str, value: str) -> tuple[str, ...] | None:
    found: dict[str, None] = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        relative = _relative(working_dir, item)
        if relative is None:
            return None
        found[relative] = None
    return tuple(found)


def _relative(working_dir: str, path: str) -> str | None:
    if not path.startswith("/"):
        path = f"{working_dir}/{path}"
    if any(part in (".", "..") for part in path.split("/")):
        return None
    prefix = f"{working_dir}/"
    if posixpath.normpath(path) != path or not path.startswith(prefix):
        return None
    relative = path.removeprefix(prefix)
    return relative if is_relative_path(relative) else None


def parse_environment_file(text: str, path: str) -> dict[str, str] | NeedsFile:
    """Parse the strict subset of the dotenv format; a later line sets a name again.

    Anything outside the subset is refused with `path:line`, never the line's content.
    """
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parsed = _assignment(stripped)
        if parsed is None:
            return NeedsFile("environment_file_unsupported", f"{path}:{number}")
        values[parsed[0]] = parsed[1]
    return values


def _assignment(line: str) -> tuple[str, str] | None:
    found = _ASSIGNMENT.fullmatch(line)
    if found is None:
        return None
    name, raw = found.group(1), found.group(2).strip()
    if VARIABLE_NAME.fullmatch(name) is None or is_reserved_variable(name):
        return None
    value = _value(raw)
    return None if value is None else (name, value)


def _value(raw: str) -> str | None:
    if not raw:
        return ""
    if raw[0] in _QUOTES:
        inner = raw[1:-1]
        closed = len(raw) >= 2 and raw[-1] == raw[0]
        return inner if closed and not (set(inner) & {raw[0], "\\", "$"}) else None
    if raw.startswith("#") or _INLINE_COMMENT.search(raw) or _UNQUOTED_REFUSED & set(raw):
        return None
    return raw


def variables_of(values: Mapping[str, str]) -> tuple[ReadVariable, ...]:
    """Return the values as stack variables, a secret where the name says so."""
    return tuple(
        ReadVariable(name, value, is_secret_entry(name, value)) for name, value in values.items()
    )
