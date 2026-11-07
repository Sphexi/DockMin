"""Who can reach an environment or a group: the access sections and the effective-access view."""

from dataclasses import dataclass
from typing import Literal

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.repositories.environment import EnvironmentRepository
from app.db.repositories.environment_access import (
    MAX_GRANT_LISTING,
    EnvironmentAccessRepository,
    GrantScope,
)
from app.db.repositories.environment_group import EnvironmentGroupRepository
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.services.guards import require_administrator

GranteeKind = Literal["user", "team"]


@dataclass(frozen=True, slots=True)
class Grantee:
    """A user or team that holds, or is to hold, a grant."""

    kind: GranteeKind
    id: int
    name: str

    @property
    def user_id(self) -> int | None:
        """Return the user's id, or None for a team."""
        return self.id if self.kind == "user" else None

    @property
    def team_id(self) -> int | None:
        """Return the team's id, or None for a user."""
        return self.id if self.kind == "team" else None


@dataclass(frozen=True, slots=True)
class GrantList:
    """Grantees of one target, with the full counts when the listing was cut short."""

    users: tuple[Grantee, ...]
    teams: tuple[Grantee, ...]
    users_total: int
    teams_total: int


@dataclass(frozen=True, slots=True)
class GroupReference:
    """The group an environment sits in."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class EnvironmentAccessView:
    """The access section of an environment: its own grants and those inherited from its group."""

    environment_id: int
    direct: GrantList
    group: GroupReference | None
    inherited: GrantList | None


@dataclass(frozen=True, slots=True)
class GroupAccessView:
    """The access section of a group."""

    group_id: int
    grants: GrantList


async def resolve_grantee(
    users: UserRepository, teams: TeamRepository, *, user_id: int | None, team_id: int | None
) -> Grantee:
    """Find the one user or team a grant names; ValidationFailed unless exactly one exists."""
    if (user_id is None) == (team_id is None):
        raise ValidationFailed({"grantee": "Choose one user or one team."})
    if user_id is not None:
        user = await users.get(user_id)
        if user is None:
            raise ValidationFailed({"grantee": "That user no longer exists."})
        return Grantee("user", user.id, user.username)
    assert team_id is not None
    team = await teams.get(team_id)
    if team is None:
        raise ValidationFailed({"grantee": "That team no longer exists."})
    return Grantee("team", team.id, team.name)


async def list_grants(
    grants: EnvironmentAccessRepository, scope: GrantScope, target_id: int
) -> GrantList:
    """List a target's direct grantees, by name, up to the listing cap."""
    users = await grants.list_users(scope, target_id, limit=MAX_GRANT_LISTING)
    teams = await grants.list_teams(scope, target_id, limit=MAX_GRANT_LISTING)
    user_total, team_total = (
        (len(users), len(teams))
        if len(users) < MAX_GRANT_LISTING and len(teams) < MAX_GRANT_LISTING
        else await grants.count_grants(scope, target_id)
    )
    return GrantList(
        users=tuple(Grantee("user", u.user_id, u.username) for u in users),
        teams=tuple(Grantee("team", t.team_id, t.name) for t in teams),
        users_total=user_total,
        teams_total=team_total,
    )


class EnvironmentAccessService:
    """Reads who holds access to environments and groups; administrators only."""

    def __init__(
        self,
        *,
        grants: EnvironmentAccessRepository,
        environments: EnvironmentRepository,
        groups: EnvironmentGroupRepository,
        users: UserRepository,
        teams: TeamRepository,
    ) -> None:
        self._grants = grants
        self._environments = environments
        self._groups = groups
        self._users = users
        self._teams = teams

    async def resolve_grantee(
        self, actor: Principal, *, username: str | None = None, team_name: str | None = None
    ) -> Grantee:
        """Find the one enabled standard user or team a grant form names, by exact name.

        Administrators need no grant, so naming one is refused.
        """
        require_administrator(actor)
        if (username is None) == (team_name is None):
            raise ValidationFailed({"grantee": "Choose one user or one team."})
        if username is not None:
            user = await self._users.get_by_username(username.strip())
            if user is None:
                raise ValidationFailed({"grantee": "No user has that username."})
            if user.role != "standard":
                raise ValidationFailed(
                    {"grantee": "Administrators reach every environment and need no grant."}
                )
            if not user.is_enabled:
                raise ValidationFailed({"grantee": "That user is disabled."})
            return Grantee("user", user.id, user.username)
        assert team_name is not None
        team = await self._teams.get_by_name(team_name.strip())
        if team is None:
            raise ValidationFailed({"grantee": "No team has that name."})
        return Grantee("team", team.id, team.name)

    async def for_environment(self, actor: Principal, environment_id: int) -> EnvironmentAccessView:
        """Return the grants on an environment and those it inherits from its group."""
        require_administrator(actor)
        environment = await self._environments.get(environment_id)
        if environment is None:
            raise ResourceNotFound("not found")
        direct = await list_grants(self._grants, "environment", environment_id)
        group_id = environment.environment_group_id
        if group_id is None:
            return EnvironmentAccessView(environment_id, direct, None, None)
        group = await self._groups.get(group_id)
        if group is None:
            return EnvironmentAccessView(environment_id, direct, None, None)
        return EnvironmentAccessView(
            environment_id,
            direct,
            GroupReference(group.id, group.name),
            await list_grants(self._grants, "group", group_id),
        )

    async def for_group(self, actor: Principal, group_id: int) -> GroupAccessView:
        """Return the grants on a group."""
        require_administrator(actor)
        if await self._groups.get(group_id) is None:
            raise ResourceNotFound("not found")
        return GroupAccessView(group_id, await list_grants(self._grants, "group", group_id))
