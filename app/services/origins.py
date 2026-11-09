"""The page a user came from: a typed reference, resolved under the viewer's own access."""

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final

from app.auth.errors import ProductError, ResourceNotFound
from app.auth.principal import Principal
from app.db.repositories.pagination import MAX_ROW_ID
from app.docker_engine.errors import EngineError

REF_MAX: Final = 320
HOST_BUDGET_SECONDS: Final = 2.0
REACHABLE: Final = "reachable"
NO_ENVIRONMENT: Final = "-"
IMAGE_PREFIX: Final = "sha256:"

HOST_KINDS: Final = frozenset(
    {"container", "service", "network", "volume", "image", "config", "secret"}
)
ROW_KINDS: Final = frozenset({"automation", "activity", "environment", "team"})
LIST_KIND: Final = "list"


@dataclass(frozen=True, slots=True)
class ListOrigin:
    """A list page that can be an origin; `environment` lists are addressed by environment id."""

    label: str
    environment: bool
    swarm: bool = False
    administrators: bool = False


LIST_ORIGINS: Final[Mapping[str, ListOrigin]] = {
    "containers": ListOrigin("containers", environment=True),
    "images": ListOrigin("images", environment=True),
    "volumes": ListOrigin("volumes", environment=True),
    "networks": ListOrigin("networks", environment=True),
    "services": ListOrigin("services", environment=True, swarm=True),
    "configs": ListOrigin("configs", environment=True, swarm=True),
    "secrets": ListOrigin("secrets", environment=True, swarm=True),
    "automation": ListOrigin("automation", environment=False),
    "templates": ListOrigin("templates", environment=False),
    "jobs": ListOrigin("jobs", environment=False, administrators=True),
    "activity": ListOrigin("the activity log", environment=False, administrators=True),
    "backups": ListOrigin("backups", environment=False, administrators=True),
}

_SWARM_ID: Final = re.compile(r"[0-9a-z]{25}")
_KEY_SHAPES: Final[Mapping[str, re.Pattern[str]]] = {
    "stack": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}"),
    "container": re.compile(r"[0-9a-f]{64}"),
    "service": _SWARM_ID,
    "config": _SWARM_ID,
    "secret": _SWARM_ID,
    "network": re.compile(r"[0-9a-z]{25}|[0-9a-f]{64}"),
    "volume": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}"),
    "image": re.compile(r"sha256:([0-9a-f]{64})"),
}
_DIGITS: Final = re.compile(r"[0-9]{1,19}")


@dataclass(frozen=True, slots=True)
class OriginRef:
    """A parsed reference; `environment_id` is None for the kinds that belong to none."""

    kind: str
    environment_id: int | None
    key: str

    @property
    def text(self) -> str:
        """Return the reference as it is written into a link."""
        shown = NO_ENVIRONMENT if self.environment_id is None else str(self.environment_id)
        prefix = IMAGE_PREFIX if self.kind == "image" else ""
        return f"{self.kind}:{shown}:{prefix}{self.key}"


@dataclass(frozen=True, slots=True)
class OriginTarget:
    """What a reader found: the object's name and the spelling its page is addressed by."""

    name: str
    key: str
    variant: str = ""


@dataclass(frozen=True, slots=True)
class Origin:
    """A resolved origin; the href and label are built from the object read, never the input."""

    label: str
    href: str
    ref: str


type Reader = Callable[[Principal, int | None, str, bool], Awaitable[OriginTarget]]
type StatusOf = Callable[[int], Awaitable[str]]
type HrefFor = Callable[[str, int | None, str, str], str]


def _row_id(text: str) -> int | None:
    if _DIGITS.fullmatch(text) is None or text != str(int(text)):
        return None
    value = int(text)
    return value if 1 <= value <= MAX_ROW_ID else None


def parse_origin(text: str) -> OriginRef | None:
    """Return the reference `text` spells, or None for anything that is not exactly one."""
    if not text or len(text) > REF_MAX or not text.isascii() or not text.isprintable():
        return None
    kind, separator, rest = text.partition(":")
    environment, separator_two, key = rest.partition(":")
    if not separator or not separator_two:
        return None
    if kind in ROW_KINDS:
        row = _row_id(key)
        return OriginRef(kind, None, key) if environment == NO_ENVIRONMENT and row else None
    if kind == LIST_KIND:
        return _list_ref(environment, key)
    shape = _KEY_SHAPES.get(kind)
    environment_id = _row_id(environment)
    if shape is None or environment_id is None:
        return None
    found = shape.fullmatch(key)
    if found is None:
        return None
    return OriginRef(kind, environment_id, found.group(1) if kind == "image" else key)


def _list_ref(environment: str, key: str) -> OriginRef | None:
    listed = LIST_ORIGINS.get(key)
    if listed is None:
        return None
    if not listed.environment:
        return OriginRef(LIST_KIND, None, key) if environment == NO_ENVIRONMENT else None
    environment_id = _row_id(environment)
    return None if environment_id is None else OriginRef(LIST_KIND, environment_id, key)


def list_ref(name: str, environment_id: int | None = None) -> str:
    """Return the reference of a list page; raises KeyError or ValueError for one that is not."""
    listed = LIST_ORIGINS[name]
    if listed.environment != (environment_id is not None):
        raise ValueError(f"list {name!r} takes {'an' if listed.environment else 'no'} environment")
    return OriginRef(LIST_KIND, environment_id, name).text


async def list_target(
    principal: Principal,
    environment_id: int | None,
    name: str,
    *,
    require_environment: Callable[[Principal, int], Awaitable[None]],
    environment_type: Callable[[int], Awaitable[str | None]],
) -> OriginTarget:
    """Return a list origin the viewer may open; ResourceNotFound otherwise."""
    listed = LIST_ORIGINS[name]
    if listed.administrators and not principal.is_administrator:
        raise ResourceNotFound("not found")
    if environment_id is not None:
        await require_environment(principal, environment_id)
        if listed.swarm and await environment_type(environment_id) != "swarm":
            raise ResourceNotFound("not found")
    return OriginTarget(listed.label, name)


class OriginResolver:
    """Resolves an origin reference to a labelled link, or to nothing."""

    def __init__(
        self,
        readers: Mapping[str, Reader],
        status_of: StatusOf,
        href_for: HrefFor,
        *,
        budget: float | None = None,
    ) -> None:
        self._readers = readers
        self._status_of = status_of
        self._href_for = href_for
        self._budget = HOST_BUDGET_SECONDS if budget is None else budget

    async def resolve(self, principal: Principal, text: str) -> Origin | None:
        """Return the origin the viewer may read, or None; the text is never echoed."""
        parsed = parse_origin(text)
        if parsed is None:
            return None
        reader = self._readers.get(parsed.kind)
        if reader is None:
            return None
        try:
            async with asyncio.timeout(self._budget):
                reachable = await self._reachable(parsed)
                if parsed.kind in HOST_KINDS and not reachable:
                    return None
                target = await reader(principal, parsed.environment_id, parsed.key, reachable)
        except (ProductError, EngineError, TimeoutError):
            return None
        resolved = OriginRef(parsed.kind, parsed.environment_id, target.key)
        return Origin(
            label=_label(parsed.kind, target.name),
            href=self._href_for(parsed.kind, parsed.environment_id, target.key, target.variant),
            ref=resolved.text,
        )

    async def _reachable(self, parsed: OriginRef) -> bool:
        if parsed.environment_id is None:
            return False
        return await self._status_of(parsed.environment_id) == REACHABLE


def _label(kind: str, name: str) -> str:
    if kind == LIST_KIND:
        return name
    return "activity event" if kind == "activity" else f"{kind} {name}"
