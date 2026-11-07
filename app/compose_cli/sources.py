"""Config and secret file sources delivered to Compose as environment variables."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from app.compose_cli.environment import SOURCE_PREFIX

SOURCES_FILE: Final = "sources.yaml"

_KEY: Final = re.compile(r"[A-Za-z0-9._-]{1,256}")
_SERVICE: Final = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,127}")
_IMAGE: Final = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")

type BuiltImages = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ComposeSource:
    """The content of the file one config or secret is read from."""

    section: Literal["configs", "secrets"]
    key: str
    content: str = field(repr=False)
    file: str = ""
    secret: bool = False


def number_sources(sources: Sequence[ComposeSource]) -> tuple[tuple[str, ComposeSource], ...]:
    """Name each source `DOCKMIN_SOURCE_<n>`, n counting from 1 in sorted (section, key) order.

    ValueError for a key outside Compose's grammar or a section and key given twice.
    """
    ordered = sorted(sources, key=lambda item: (item.section, item.key))
    slots = [(item.section, item.key) for item in ordered]
    if len(set(slots)) != len(slots):
        raise ValueError("A config or secret source is given twice.")
    if any(_KEY.fullmatch(item.key) is None for item in ordered):
        raise ValueError("A config or secret name is outside the permitted grammar.")
    return tuple((f"{SOURCE_PREFIX}{number}", item) for number, item in enumerate(ordered, 1))


def check_built_images(built: BuiltImages) -> BuiltImages:
    """Return the (service, image) pairs unchanged; ValueError for a name outside the grammar."""
    seen: set[str] = set()
    for service, image in built:
        if _SERVICE.fullmatch(service) is None or _IMAGE.fullmatch(image) is None:
            raise ValueError("A built service or image name is outside the permitted grammar.")
        if service in seen:
            raise ValueError("A built service is given twice.")
        seen.add(service)
    return built


def override_document(
    numbered: Sequence[tuple[str, ComposeSource]], built: BuiltImages = ()
) -> str:
    """Return the Compose file that swaps each `file` source for the matching environment one.

    Each (service, image) of `built` replaces the service's `build` with that image.
    """
    lines: list[str] = []
    if built:
        lines.append("services:")
        for service, image in check_built_images(built):
            lines.extend(
                (
                    f"  {json.dumps(service)}:",
                    "    build: !reset null",
                    f"    image: {json.dumps(image)}",
                )
            )
    for section in ("configs", "secrets"):
        entries = [(name, item) for name, item in numbered if item.section == section]
        if not entries:
            continue
        lines.append(f"{section}:")
        for name, item in entries:
            lines.extend(
                (
                    f"  {json.dumps(item.key)}:",
                    "    file: !reset null",
                    f"    environment: {name}",
                )
            )
    return "\n".join(lines) + "\n"
