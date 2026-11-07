"""Team management: administrators run teams, a leader runs the membership of their own."""

from dataclasses import dataclass

from app.auth.errors import PermissionDenied, TeamNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.errors import DuplicateError
from app.db.repositories.pagination import Page
from app.db.repositories.team import Team, TeamRepository
from app.db.repositories.team_membership import TeamMembership, TeamMembershipRepository
from app.db.repositories.team_roster import RosterEntry, TeamRosterRepository
from app.db.repositories.user import UserRepository
from app.services.activity import ActivityRecorder, audited, current_audit

TEAM_NAME_MAX = 64
CHANGED_SINCE_REVIEW = "The team changed since you reviewed it; nothing was deleted."


@dataclass(frozen=True, slots=True)
class TeamView:
    """A team as a template sees it."""

    id: int
    name: str
    created_at: str

    @classmethod
    def of(cls, team: Team) -> "TeamView":
        """Project a team."""
        return cls(team.id, team.name, team.created_at)


@dataclass(frozen=True, slots=True)
class TeamDetail:
    """A team and what the viewer may do with it, so a page never offers a refused action."""

    team: TeamView
    can_rename_and_delete: bool
    can_manage_members: bool
    can_manage_leaders: bool


class TeamService:
    """Applies team rules; every method takes the acting principal and decides for itself."""

    def __init__(
        self,
        *,
        db: Session,
        teams: TeamRepository,
        memberships: TeamMembershipRepository,
        roster: TeamRosterRepository,
        users: UserRepository,
        activity: ActivityRecorder,
    ) -> None:
        self._db = db
        self._teams = teams
        self._memberships = memberships
        self._roster = roster
        self._users = users
        self._activity = activity

    @audited(action="team_created", target_type="team")
    async def create(self, actor: Principal, *, name: str) -> TeamView:
        """Create a team; administrators only."""
        _require_administrator(actor)
        checked = _check_name(name)
        async with self._db.transaction():
            try:
                team = await self._teams.create(checked)
            except DuplicateError:
                raise ValidationFailed({"name": "That team name is taken."}) from None
            audit = current_audit()
            audit.target_id = str(team.id)
            audit.detail["name"] = checked
        return TeamView.of(team)

    async def list_page(
        self, actor: Principal, *, limit: int, cursor: str | None, name_prefix: str | None = None
    ) -> Page[TeamView]:
        """Return a page of teams: every team for an administrator, their own for anyone else."""
        if actor.is_administrator:
            page = await self._teams.list_page(limit=limit, cursor=cursor, name_prefix=name_prefix)
        else:
            page = await self._teams.list_page_for_user(
                actor.user_id, limit=limit, cursor=cursor, name_prefix=name_prefix
            )
        return Page([TeamView.of(team) for team in page.items], page.next_cursor)

    async def count(self, actor: Principal) -> int:
        """Count the teams list_page shows the caller."""
        if actor.is_administrator:
            return await self._teams.count()
        return await self._teams.count_for_user(actor.user_id)

    async def get(self, actor: Principal, team_id: int) -> TeamDetail:
        """Return a team the caller administers or belongs to."""
        team, membership = await self._visible(actor, team_id)
        is_leader = membership is not None and membership.is_leader
        return TeamDetail(
            team=TeamView.of(team),
            can_rename_and_delete=actor.is_administrator,
            can_manage_members=actor.is_administrator or is_leader,
            can_manage_leaders=actor.is_administrator,
        )

    @audited(action="team_renamed", target_type="team", target_id=lambda a: str(a["team_id"]))
    async def rename(self, actor: Principal, team_id: int, *, name: str) -> TeamView:
        """Rename a team; administrators only."""
        _require_administrator(actor)
        checked = _check_name(name)
        async with self._db.transaction():
            team = await self._load(team_id)
            try:
                await self._teams.rename(team_id, checked)
            except DuplicateError:
                raise ValidationFailed({"name": "That team name is taken."}) from None
            current_audit().detail.update(old_name=team.name, new_name=checked)
            renamed = await self._load(team_id)
        return TeamView.of(renamed)

    @audited(action="team_deleted", target_type="team", target_id=lambda a: str(a["team_id"]))
    async def delete(self, actor: Principal, team_id: int, *, expected_name: str) -> None:
        """Delete a team, its memberships and the grants made to it; administrators only.

        The team is deleted only while its name is still expected_name, the one the caller saw.
        """
        _require_administrator(actor)
        async with self._db.transaction():
            team = await self._load(team_id)
            if team.name != expected_name:
                raise ValidationFailed({"name": CHANGED_SINCE_REVIEW})
            await self._teams.delete(team_id)
            current_audit().detail["name"] = team.name

    async def list_members(
        self, actor: Principal, team_id: int, *, limit: int, cursor: str | None
    ) -> Page[RosterEntry]:
        """Return a page of a team's members to an administrator or a member."""
        await self._visible(actor, team_id)
        return await self._roster.list_for_team(team_id, limit=limit, cursor=cursor)

    @audited(action="team_member_added", target_type="team", target_id=lambda a: str(a["team_id"]))
    async def add_member(
        self, actor: Principal, team_id: int, *, username: str, is_leader: bool = False
    ) -> RosterEntry:
        """Add an enabled user to a team; only an administrator may appoint a leader."""
        async with self._db.transaction():
            await self._require_manager(actor, team_id)
            if is_leader and not actor.is_administrator:
                raise PermissionDenied("only an administrator can appoint a team leader")
            user = await self._users.get_by_username(username.strip())
            if user is None or not user.is_enabled:
                raise ValidationFailed({"username": "No enabled user has that name."})
            try:
                membership = await self._memberships.add(team_id, user.id, is_leader=is_leader)
            except DuplicateError:
                raise ValidationFailed({"username": "That user is already a member."}) from None
            current_audit().detail.update(user_id=str(user.id), leader=str(is_leader).lower())
        return RosterEntry(
            membership.id,
            team_id,
            user.id,
            user.username,
            membership.is_leader,
            membership.created_at,
        )

    @audited(
        action="team_member_removed", target_type="team", target_id=lambda a: str(a["team_id"])
    )
    async def remove_member(self, actor: Principal, team_id: int, *, user_id: int) -> None:
        """Remove a member; only an administrator may remove a leader."""
        async with self._db.transaction():
            await self._require_manager(actor, team_id)
            target = await self._memberships.get_for(team_id, user_id)
            if target is None:
                raise ValidationFailed({"user": "That user is not a member of this team."})
            if target.is_leader and not actor.is_administrator:
                raise PermissionDenied("only an administrator can remove a team leader")
            await self._memberships.remove(team_id, user_id)
            current_audit().detail["user_id"] = str(user_id)

    @audited(
        action="team_leader_changed", target_type="team", target_id=lambda a: str(a["team_id"])
    )
    async def set_leader(
        self, actor: Principal, team_id: int, *, user_id: int, is_leader: bool
    ) -> None:
        """Appoint or dismiss a team leader; administrators only."""
        _require_administrator(actor)
        async with self._db.transaction():
            await self._load(team_id)
            if not await self._memberships.set_leader(team_id, user_id, is_leader=is_leader):
                raise ValidationFailed({"user": "That user is not a member of this team."})
            current_audit().detail.update(user_id=str(user_id), leader=str(is_leader).lower())

    async def _load(self, team_id: int) -> Team:
        team = await self._teams.get(team_id)
        if team is None:
            raise TeamNotFound(f"team {team_id} does not exist")
        return team

    async def _visible(self, actor: Principal, team_id: int) -> tuple[Team, TeamMembership | None]:
        team = await self._load(team_id)
        if actor.is_administrator:
            return team, None
        membership = await self._memberships.get_for(team_id, actor.user_id)
        if membership is None:
            # Indistinguishable from a team that does not exist.
            raise TeamNotFound(f"team {team_id} does not exist")
        return team, membership

    async def _require_manager(self, actor: Principal, team_id: int) -> None:
        """Allow an administrator, or a leader of this very team, to change its membership.

        Read inside the caller's transaction, so a leader dismissed a moment ago cannot act on
        a stale answer.
        """
        _, membership = await self._visible(actor, team_id)
        if actor.is_administrator:
            return
        if membership is None or not membership.is_leader:
            raise PermissionDenied("only a team leader can change membership")


def _require_administrator(actor: Principal) -> None:
    if not actor.is_administrator:
        raise PermissionDenied("administrator role required")


def _check_name(name: str) -> str:
    stripped = name.strip()
    if not stripped or len(stripped) > TEAM_NAME_MAX or any(ord(c) < 32 for c in stripped):
        raise ValidationFailed({"name": f"Use 1 to {TEAM_NAME_MAX} printable characters."})
    return stripped
