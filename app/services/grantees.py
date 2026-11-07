"""Resolve the users and teams an access form names, and name the ids a control holds."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from app.auth.errors import ValidationFailed
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository

MAX_NAMES_PER_FIELD: Final = 50
MAX_NAME_CHARS: Final = 128
MAX_REPORTED_NAMES: Final = 5

USER_FIELD: Final = "add_users"
TEAM_FIELD: Final = "add_teams"
OWNER_FIELD: Final = "owner_name"
OWNER_GONE: Final = "That account no longer exists."


@dataclass(frozen=True, slots=True)
class GranteeNames:
    """The names of the users and teams a control grants; an id with no entry no longer exists."""

    users: Mapping[int, str]
    teams: Mapping[int, str]


def _lookup_key(name: str) -> bytes:
    return name.encode("utf-8").lower()


def split_names(text: str, *, field: str = "names", commas: bool = True) -> tuple[str, ...]:
    """Split typed names on new lines, and on commas unless `commas` is off.

    Raises ValidationFailed over MAX_NAMES_PER_FIELD. Names are stripped, empty ones dropped, and
    repeats that the account lookup treats as one name (ASCII case ignored) kept once.
    """
    seen: set[bytes] = set()
    names: list[str] = []
    normalised = text.replace("\r", "\n")
    if commas:
        normalised = normalised.replace(",", "\n")
    for part in normalised.split("\n"):
        name = part.strip()
        if not name or _lookup_key(name) in seen:
            continue
        seen.add(_lookup_key(name))
        names.append(name)
    if len(names) > MAX_NAMES_PER_FIELD:
        raise ValidationFailed({field: f"Name at most {MAX_NAMES_PER_FIELD} in one field."})
    return tuple(names)


def _shown(name: str) -> str:
    printable = "".join(char for char in name if char.isprintable())
    return printable[:MAX_NAME_CHARS]


def _unknown(kind: str, names: Sequence[str]) -> str:
    listed = ", ".join(f"“{_shown(name)}”" for name in names[:MAX_REPORTED_NAMES])
    more = len(names) - MAX_REPORTED_NAMES
    suffix = f" and {more} more" if more > 0 else ""
    noun = kind if len(names) == 1 else f"{kind}s"
    verb = "is" if len(names) == 1 else "are"
    return f"No {noun} {verb} named {listed}{suffix}. Check the spelling."


class GranteeDirectory:
    """Looks accounts up by name or id; reads only."""

    def __init__(self, users: UserRepository, teams: TeamRepository) -> None:
        self._users = users
        self._teams = teams

    async def resolve(
        self, *, users: Sequence[str], teams: Sequence[str], owner: str, owner_id: int | None = None
    ) -> tuple[tuple[int, ...], tuple[int, ...], int | None]:
        """Return the ids of the named users and teams and of the owner.

        A given `owner_id` is the owner and `owner` is ignored; it must still name an account.
        ValidationFailed names every unknown name, each under the field that carried it.
        """
        problems: dict[str, str] = {}
        user_ids = await self._resolve_users(users, problems)
        team_ids = await self._resolve_teams(teams, problems)
        if owner_id is None:
            resolved_owner = await self._resolve_owner(owner, problems)
        else:
            resolved_owner = await self._confirm_owner(owner_id, problems)
        if problems:
            raise ValidationFailed(problems)
        return user_ids, team_ids, resolved_owner

    async def names(
        self, *, user_ids: Sequence[int], team_ids: Sequence[int], owner_id: int | None
    ) -> GranteeNames:
        """Return the current names of the given users (the owner included) and teams."""
        wanted = set(user_ids)
        if owner_id is not None:
            wanted.add(owner_id)
        users = await self._users.usernames(wanted)
        teams = await self._teams.team_names(team_ids)
        return GranteeNames(users=users, teams=teams)

    async def _resolve_users(
        self, names: Sequence[str], problems: dict[str, str]
    ) -> tuple[int, ...]:
        found: list[int] = []
        missing: list[str] = []
        for name in names:
            user = None if len(name) > MAX_NAME_CHARS else await self._users.get_by_username(name)
            if user is None:
                missing.append(name)
            else:
                found.append(user.id)
        if missing:
            problems[USER_FIELD] = _unknown("user", missing)
        return tuple(dict.fromkeys(found))

    async def _resolve_teams(
        self, names: Sequence[str], problems: dict[str, str]
    ) -> tuple[int, ...]:
        found: list[int] = []
        missing: list[str] = []
        for name in names:
            team = None if len(name) > MAX_NAME_CHARS else await self._teams.get_by_name(name)
            if team is None:
                missing.append(name)
            else:
                found.append(team.id)
        if missing:
            problems[TEAM_FIELD] = _unknown("team", missing)
        return tuple(dict.fromkeys(found))

    async def _resolve_owner(self, owner: str, problems: dict[str, str]) -> int | None:
        name = owner.strip()
        if not name:
            return None
        user = None if len(name) > MAX_NAME_CHARS else await self._users.get_by_username(name)
        if user is None:
            problems[OWNER_FIELD] = _unknown("user", [name])
            return None
        return user.id

    async def _confirm_owner(self, owner_id: int, problems: dict[str, str]) -> int | None:
        if owner_id not in await self._users.usernames([owner_id]):
            problems[OWNER_FIELD] = OWNER_GONE
            return None
        return owner_id


class HasGrantees(Protocol):
    """A query service that carries a grantee directory."""

    grantees: GranteeDirectory


async def resolve_grantees(
    services: HasGrantees,
    *,
    users: Sequence[str],
    teams: Sequence[str],
    owner: str,
    owner_id: int | None = None,
) -> tuple[tuple[int, ...], tuple[int, ...], int | None]:
    """Resolve typed names through the `grantees` directory of a query service."""
    return await services.grantees.resolve(users=users, teams=teams, owner=owner, owner_id=owner_id)


async def names_for(
    services: HasGrantees, *, user_ids: Sequence[int], team_ids: Sequence[int], owner_id: int | None
) -> GranteeNames:
    """Look up the names of the given ids through the `grantees` directory of a query service."""
    return await services.grantees.names(user_ids=user_ids, team_ids=team_ids, owner_id=owner_id)
