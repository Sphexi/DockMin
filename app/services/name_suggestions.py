"""Names of existing objects offered while a field is typed, as each list page shows them."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final, Literal

from app.auth.errors import ProductError
from app.auth.principal import Principal
from app.auth.users import UserService
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.db.repositories.resource_control import canonical_stack_key
from app.db.repositories.stack import StackKind
from app.services.containers import ContainerService
from app.services.image_queries import ImageQueryService
from app.services.networks import NetworkQueryService
from app.services.stack_queries import StackQueryService
from app.services.swarm_configs import SwarmConfigQueries, SwarmSecretQueries
from app.services.swarm_service_queries import SwarmServiceQueries
from app.services.teams import TeamService
from app.services.volumes import VolumeQueryService

MAX_SUGGESTIONS: Final = 20
MAX_PREFIX_CHARS: Final = 512
READ_ONLY_LABEL: Final = "read-only"
USER_LABEL: Final = "user"
TEAM_LABEL: Final = "team"
EVERY_STACK_KIND: Final[frozenset[StackKind]] = frozenset({"compose", "swarm"})

type EnvironmentKind = Literal[
    "containers", "images", "volumes", "networks", "stacks", "services", "configs", "secrets"
]
type GlobalKind = Literal["users", "grantees"]


@dataclass(frozen=True, slots=True)
class NameOption:
    """One suggested name; `label` is shown beside it when set."""

    value: str
    label: str | None = None


@dataclass(frozen=True, slots=True)
class SuggestionSources:
    """The list services suggestions read from, each built only when its kind is asked for."""

    containers: Callable[[], ContainerService]
    images: Callable[[], ImageQueryService]
    volumes: Callable[[], VolumeQueryService]
    networks: Callable[[], NetworkQueryService]
    stacks: Callable[[], StackQueryService]
    services: Callable[[], SwarmServiceQueries]
    configs: Callable[[], SwarmConfigQueries]
    secrets: Callable[[], SwarmSecretQueries]
    users: Callable[[], UserService]
    teams: Callable[[], TeamService]


def _prefix_of(typed: str) -> str | None:
    text = typed.strip()
    return text.casefold() if text and len(text) <= MAX_PREFIX_CHARS else None


def _chosen(options: Iterable[NameOption], prefix: str) -> list[NameOption]:
    unique: dict[str, NameOption] = {}
    for option in options:
        if option.value.casefold().startswith(prefix):
            unique.setdefault(option.value, option)
    return sorted(unique.values(), key=lambda o: (o.value.casefold(), o.value))[:MAX_SUGGESTIONS]


class NameSuggestions:
    """Names from what the caller's list page shows, cut to a prefix.

    Hidden, missing, unreachable and refused all answer empty, so no answer tells them apart.
    """

    def __init__(self, sources: SuggestionSources) -> None:
        self._sources = sources

    async def in_environment(
        self,
        actor: Principal,
        environment_id: int,
        kind: EnvironmentKind,
        typed: str,
        *,
        stack_kinds: frozenset[StackKind] = EVERY_STACK_KIND,
    ) -> tuple[NameOption, ...]:
        """Return at most 20 names of `kind` in the environment starting with `typed`."""
        prefix = _prefix_of(typed)
        if prefix is None:
            return ()
        try:
            found = await self._read(actor, environment_id, kind, prefix, stack_kinds)
        except ProductError:
            return ()
        return tuple(_chosen(_one_per_stack(found) if kind == "stacks" else found, prefix))

    async def global_names(
        self, actor: Principal, kind: GlobalKind, typed: str
    ) -> tuple[NameOption, ...]:
        """Return at most 20 users, or users then teams, starting with `typed`; administrators."""
        prefix = _prefix_of(typed)
        if prefix is None or not actor.is_administrator:
            return ()
        try:
            users = await self._sources.users().list_page(
                actor, limit=MAX_SUGGESTIONS, cursor=None, name_prefix=prefix
            )
            teams = (
                await self._sources.teams().list_page(
                    actor, limit=MAX_SUGGESTIONS, cursor=None, name_prefix=prefix
                )
                if kind == "grantees"
                else None
            )
        except ProductError:
            return ()
        if teams is None:
            return tuple(_chosen((NameOption(u.username) for u in users.items), prefix))
        named_users = _chosen((NameOption(u.username, USER_LABEL) for u in users.items), prefix)
        named_teams = _chosen((NameOption(t.name, TEAM_LABEL) for t in teams.items), prefix)
        return tuple([*named_users, *named_teams][:MAX_SUGGESTIONS])

    async def _read(
        self,
        actor: Principal,
        environment_id: int,
        kind: EnvironmentKind,
        prefix: str,
        stack_kinds: frozenset[StackKind],
    ) -> list[NameOption]:
        sources = self._sources
        match kind:
            case "containers":
                names = await sources.containers().names(
                    actor, environment_id, prefix, limit=MAX_SUGGESTIONS
                )
            case "images":
                names = await sources.images().tag_names(
                    actor, environment_id, prefix, limit=MAX_SUGGESTIONS
                )
            case "volumes":
                volumes = await sources.volumes().names(
                    actor, environment_id, prefix, limit=MAX_SUGGESTIONS
                )
                return [
                    NameOption(name, None if writable else READ_ONLY_LABEL)
                    for name, writable in volumes
                ]
            case "networks":
                names = await sources.networks().names(
                    actor, environment_id, prefix, limit=MAX_SUGGESTIONS
                )
            case "stacks":
                if not stack_kinds:
                    return []
                kind_filter = next(iter(stack_kinds)) if len(stack_kinds) == 1 else None
                stacks = await sources.stacks().managed_names(
                    actor, environment_id, kind=kind_filter, prefix=prefix, limit=MAX_PAGE_SIZE
                )
                return [NameOption(stack.name) for stack in stacks if stack.kind in stack_kinds]
            case "services":
                names = await sources.services().names(
                    actor, environment_id, prefix, limit=MAX_SUGGESTIONS
                )
            case "configs" | "secrets":
                queries = sources.configs() if kind == "configs" else sources.secrets()
                names = await queries.names(actor, environment_id, prefix, limit=MAX_SUGGESTIONS)
        return [NameOption(name) for name in names]


def _one_per_stack(options: Iterable[NameOption]) -> list[NameOption]:
    """Keep one spelling of each stack: a Compose and a Swarm stack may differ only by case."""
    seen: set[str] = set()
    kept: list[NameOption] = []
    for option in options:
        key = canonical_stack_key(option.value)
        if key not in seen:
            seen.add(key)
            kept.append(option)
    return kept
