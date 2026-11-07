"""Pure parsers for Compose's machine-readable output: version, config, ps and progress events."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

from app.compose_cli.errors import ComposeOutputInvalid

MAX_EVENT_LINE_BYTES: Final = 64 * 1024
MAX_EVENT_ID_CHARS: Final = 512
MAX_EVENT_TEXT_CHARS: Final = 4096
_MAX_VERSION_BYTES: Final = 1024

_VERSION = re.compile(
    r"v?(?P<major>\d{1,6})\.(?P<minor>\d{1,6})\.(?P<patch>\d{1,6})(?:[-+][0-9A-Za-z.+\-]{0,64})?"
)


@dataclass(frozen=True)
class ComposeVersion:
    """A Compose release; any pre-release or build suffix stays in `text` only."""

    text: str
    major: int
    minor: int
    patch: int

    def at_least(self, other: "ComposeVersion") -> bool:
        """Report whether this release is the same as or newer than other."""
        return (self.major, self.minor, self.patch) >= (other.major, other.minor, other.patch)


FLOOR: Final = ComposeVersion("v2.32.0", 2, 32, 0)


@dataclass(frozen=True)
class ResolvedProject:
    """The normalised model printed by `config --format json`."""

    name: str
    services: Mapping[str, Mapping[str, Any]] = field(repr=False)
    networks: Mapping[str, Mapping[str, Any]] = field(repr=False)
    volumes: Mapping[str, Mapping[str, Any]] = field(repr=False)
    configs: Mapping[str, Mapping[str, Any]] = field(repr=False)
    secrets: Mapping[str, Mapping[str, Any]] = field(repr=False)
    document: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class PsEntry:
    """One container line of `ps --format json`; labels are deliberately not read."""

    name: str
    service: str
    state: str
    health: str | None
    exit_code: int | None


@dataclass(frozen=True)
class ProgressEvent:
    """One `--progress json` event."""

    resource: str | None
    parent: str | None
    status: str | None
    text: str | None
    is_error: bool
    current: int | None
    total: int | None

    def bounded(self) -> "ProgressEvent":
        """Return the event with each text field cut to its limit; call after scrubbing."""
        return replace(
            self,
            resource=_cut(self.resource, MAX_EVENT_ID_CHARS),
            parent=_cut(self.parent, MAX_EVENT_ID_CHARS),
            status=_cut(self.status, MAX_EVENT_ID_CHARS),
            text=_cut(self.text, MAX_EVENT_TEXT_CHARS),
        )


def parse_version(stdout: bytes) -> ComposeVersion:
    """Parse `version --format json`."""
    if len(stdout) > _MAX_VERSION_BYTES:
        raise ComposeOutputInvalid
    document = _decode_object(stdout)
    raw = document.get("version")
    if not isinstance(raw, str):
        raise ComposeOutputInvalid
    version = parse_version_text(raw)
    if version is None:
        raise ComposeOutputInvalid
    return version


def parse_version_text(text: str) -> ComposeVersion | None:
    """Parse a version as `ComposeVersion.text` stores it; None when it is not one."""
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        return None
    stripped = text.strip()
    return ComposeVersion(
        stripped if stripped.startswith("v") else f"v{stripped}",
        int(match["major"]),
        int(match["minor"]),
        int(match["patch"]),
    )


def parse_config(stdout: bytes) -> ResolvedProject:
    """Parse `config --format json`; call through asyncio.to_thread."""
    document = _decode_object(stdout)
    name = document.get("name")
    if not isinstance(name, str) or not name:
        raise ComposeOutputInvalid
    return ResolvedProject(
        name=name,
        services=_section(document, "services"),
        networks=_section(document, "networks"),
        volumes=_section(document, "volumes"),
        configs=_section(document, "configs"),
        secrets=_section(document, "secrets"),
        document=document,
    )


def parse_ps(stdout: bytes) -> tuple[PsEntry, ...]:
    """Parse `ps --all --format json` (JSON Lines); empty output is no containers."""
    entries: list[PsEntry] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        entry = _decode_object(line)
        name, service, state = entry.get("Name"), entry.get("Service"), entry.get("State")
        if not (isinstance(name, str) and isinstance(service, str) and isinstance(state, str)):
            raise ComposeOutputInvalid
        health = entry.get("Health")
        exit_code = entry.get("ExitCode")
        entries.append(
            PsEntry(
                name=name,
                service=service,
                state=state,
                health=health if isinstance(health, str) and health else None,
                exit_code=exit_code if _is_int(exit_code) else None,
            )
        )
    return tuple(entries)


def parse_event(line: bytes) -> ProgressEvent | None:
    """Return the event a line carries, or None when it is any other output.

    Fields are returned whole so a scrubber sees every secret intact; `bounded` cuts them.
    """
    if len(line) > MAX_EVENT_LINE_BYTES:
        return None
    stripped = line.strip()
    if not stripped.startswith(b"{"):
        return None
    try:
        document = json.loads(stripped, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None
    resource, status = document.get("id"), document.get("status")
    if not (isinstance(resource, str) and isinstance(status, str)):
        return None
    parent, text = document.get("parent_id"), document.get("text")
    current, total = document.get("current"), document.get("total")
    return ProgressEvent(
        resource=resource,
        parent=parent if isinstance(parent, str) and parent else None,
        status=status,
        text=text if isinstance(text, str) else None,
        is_error=status.casefold() == "error",
        current=_count(current),
        total=_count(total),
    )


def _decode_object(raw: bytes) -> dict[str, Any]:
    try:
        document = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ComposeOutputInvalid from None
    if not isinstance(document, dict):
        raise ComposeOutputInvalid
    return document


def _section(document: Mapping[str, Any], key: str) -> Mapping[str, Mapping[str, Any]]:
    value = document.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ComposeOutputInvalid
    section: dict[str, Mapping[str, Any]] = {}
    for item_name, item in value.items():
        if item is None:
            section[item_name] = {}
        elif isinstance(item, dict):
            section[item_name] = item
        else:
            raise ComposeOutputInvalid
    return section


def _cut(value: str | None, limit: int) -> str | None:
    return None if value is None else value[:limit]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _reject_constant(constant: str) -> Any:
    raise ValueError(constant)
