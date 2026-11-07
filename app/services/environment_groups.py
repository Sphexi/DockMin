"""Environment groups: named collections of environments that access can be granted on."""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.auth.errors import ProductError, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.environment import MAX_NAME_FILTER, EnvironmentRepository
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.environment_group import (
    EnvironmentGroup,
    EnvironmentGroupRepository,
    GroupSummary,
    GroupSummaryRepository,
)
from app.db.repositories.errors import DuplicateError, MissingReferenceError, RepositoryError
from app.db.repositories.pagination import Page
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.docker_engine.transport import has_hidden_characters
from app.logging import get_logger
from app.services.activity import ActivityRecorder, audited, current_audit
from app.services.environment_access import GrantList, GroupReference, list_grants, resolve_grantee
from app.services.environments import (
    CHANGED_SINCE_REVIEW,
    MAX_BATCH,
    MAX_IMPACT_NAMES,
    EnvironmentService,
)
from app.services.guards import require_administrator

logger = get_logger(__name__)

GROUP_NAME_MAX = 64
GROUP_DESCRIPTION_MAX = 256
GROUP_NAME_TAKEN = "That group name is taken."
UNGROUP_NOTE = "The environments stay where they are and become ungrouped."
GRANTS_NOTE = "Users and teams granted through the group lose access to its environments."


@dataclass(frozen=True, slots=True)
class MemberEnvironment:
    """An environment named in an impact listing."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class CandidateEnvironment:
    """An environment that could join a group, and the group it is in now."""

    id: int
    name: str
    group: GroupReference | None


@dataclass(frozen=True, slots=True)
class GroupDeletionPreview:
    """What deleting a group does: which environments are ungrouped and who loses access."""

    group_id: int
    name: str
    environments: tuple[MemberEnvironment, ...]
    environments_total: int
    grants: GrantList
    standard_users_affected: int
    ungroup_note: str = UNGROUP_NOTE
    grants_note: str = GRANTS_NOTE


type AssignOutcome = Literal["assigned", "not_found", "failed"]


@dataclass(frozen=True, slots=True)
class AssignItemResult:
    """What happened to one environment of a multi-select assignment."""

    environment_id: int
    outcome: AssignOutcome
    message: str


@dataclass(frozen=True, slots=True)
class AssignResult:
    """One result per requested environment, in request order."""

    items: tuple[AssignItemResult, ...]


class EnvironmentGroupService:
    """Applies group rules; groups are managed by administrators only."""

    def __init__(
        self,
        *,
        db: Session,
        groups: EnvironmentGroupRepository,
        summaries: GroupSummaryRepository,
        environments: EnvironmentRepository,
        grants: EnvironmentAccessRepository,
        users: UserRepository,
        teams: TeamRepository,
        environment_service: EnvironmentService,
        activity: ActivityRecorder,
    ) -> None:
        self._db = db
        self._groups = groups
        self._summaries = summaries
        self._environments = environments
        self._grants = grants
        self._users = users
        self._teams = teams
        self._environment_service = environment_service
        self._activity = activity

    async def list_page(
        self, actor: Principal, *, limit: int, cursor: str | None = None
    ) -> Page[GroupSummary]:
        """Return a page of groups with their environment counts; administrators only."""
        require_administrator(actor)
        return await self._summaries.list_page(limit=limit, cursor=cursor)

    async def count(self, actor: Principal) -> int:
        """Count the groups list_page shows; administrators only."""
        require_administrator(actor)
        return await self._summaries.count()

    async def get(self, actor: Principal, group_id: int) -> GroupSummary:
        """Return one group with its environment count; administrators only."""
        require_administrator(actor)
        return await self._load(group_id)

    @audited(action="environment_group_created", target_type="environment_group")
    async def create(
        self, actor: Principal, *, name: str, description: str | None = None
    ) -> GroupSummary:
        """Create a group; administrators only."""
        require_administrator(actor)
        checked, note = _check_fields(name, description)
        async with self._db.transaction():
            await self._refuse_taken(checked, excluding=None)
            try:
                group = await self._groups.create(checked, note)
            except DuplicateError:
                raise ValidationFailed({"name": GROUP_NAME_TAKEN}) from None
            audit = current_audit()
            audit.target_id = str(group.id)
            audit.detail["name"] = checked
        return await self._load(group.id)

    @audited(
        action="environment_group_updated",
        target_type="environment_group",
        target_id=lambda a: str(a["group_id"]),
    )
    async def update(
        self, actor: Principal, group_id: int, *, name: str, description: str | None
    ) -> GroupSummary:
        """Rename a group or change its description; administrators only."""
        require_administrator(actor)
        checked, note = _check_fields(name, description)
        current = await self._load_group(group_id)
        audit = current_audit()
        if checked == current.name and note == current.description:
            audit.unchanged()
            return await self._load(group_id)
        async with self._db.transaction():
            await self._refuse_taken(checked, excluding=group_id)
            try:
                if not await self._groups.update(group_id, name=checked, description=note):
                    raise ResourceNotFound("not found")
            except DuplicateError:
                raise ValidationFailed({"name": GROUP_NAME_TAKEN}) from None
            if checked != current.name:
                audit.detail.update(previous_name=current.name, name=checked)
        return await self._load(group_id)

    @audited(
        action="environment_group_deleted",
        target_type="environment_group",
        target_id=lambda a: str(a["group_id"]),
    )
    async def delete(self, actor: Principal, group_id: int, *, expected_name: str) -> None:
        """Delete a group while its name is still expected_name; its environments are ungrouped."""
        require_administrator(actor)
        group = await self._load(group_id)
        async with self._db.transaction():
            latest = await self._groups.get(group_id)
            if latest is None:
                raise ResourceNotFound("not found")
            if latest.name != expected_name:
                raise ValidationFailed({"name": CHANGED_SINCE_REVIEW})
            await self._groups.delete(group_id)
            current_audit().detail.update(name=group.name, ungrouped=group.environment_count)

    async def delete_preview(self, actor: Principal, group_id: int) -> GroupDeletionPreview:
        """Name, before the delete, the environments left ungrouped and who loses access."""
        require_administrator(actor)
        group = await self._load(group_id)
        members = await self._environments.list_in_group(group_id, limit=MAX_IMPACT_NAMES)
        return GroupDeletionPreview(
            group_id=group.id,
            name=group.name,
            environments=tuple(MemberEnvironment(i, n) for i, n in members),
            environments_total=group.environment_count,
            grants=await list_grants(self._grants, "group", group_id),
            standard_users_affected=await self._grants.count_standard_users_with_group_access(
                group_id
            ),
        )

    async def candidate_environments(
        self, actor: Principal, group_id: int, *, q: str | None, limit: int = 50
    ) -> list[CandidateEnvironment]:
        """List environments outside the group whose name contains q; administrators only."""
        require_administrator(actor)
        await self._load_group(group_id)
        text = None if q is None else q.strip() or None
        if text is not None and len(text) > MAX_NAME_FILTER:
            raise ValidationFailed({"q": f"Search for at most {MAX_NAME_FILTER} characters."})
        found = await self._environments.list_candidates(group_id, name_contains=text, limit=limit)
        return [
            CandidateEnvironment(
                item.id,
                item.name,
                None
                if item.group_id is None
                else GroupReference(item.group_id, item.group_name or ""),
            )
            for item in found
        ]

    async def assign_environments(
        self, actor: Principal, group_id: int, environment_ids: Sequence[int]
    ) -> AssignResult:
        """Place up to fifty environments in the group, each in its own audited operation."""
        require_administrator(actor)
        await self._load_group(group_id)
        ids = list(dict.fromkeys(environment_ids))
        if not ids:
            raise ValidationFailed({"environments": "Select at least one environment."})
        if len(ids) > MAX_BATCH:
            raise ValidationFailed({"environments": f"Select at most {MAX_BATCH} environments."})
        results: list[AssignItemResult] = []
        for environment_id in ids:
            try:
                await self._environment_service.assign_group(actor, environment_id, group_id)
            except ResourceNotFound:
                results.append(
                    AssignItemResult(environment_id, "not_found", "It no longer exists.")
                )
            except (ProductError, RepositoryError, sqlite3.Error) as error:
                logger.error(
                    "environment_assign_failed",
                    extra={"environment_id": environment_id, "error": type(error).__name__},
                )
                results.append(AssignItemResult(environment_id, "failed", "It could not be moved."))
            else:
                results.append(AssignItemResult(environment_id, "assigned", "Assigned."))
        return AssignResult(tuple(results))

    @audited(
        action="environment_group_access_granted",
        target_type="environment_group",
        target_id=lambda a: str(a["group_id"]),
    )
    async def grant(
        self,
        actor: Principal,
        group_id: int,
        *,
        user_id: int | None = None,
        team_id: int | None = None,
    ) -> None:
        """Grant a user or a team access to every environment in the group."""
        require_administrator(actor)
        await self._load_group(group_id)
        grantee = await resolve_grantee(self._users, self._teams, user_id=user_id, team_id=team_id)
        async with self._db.transaction():
            try:
                if grantee.kind == "user":
                    await self._grants.add_user("group", group_id, grantee.id)
                else:
                    await self._grants.add_team("group", group_id, grantee.id)
            except DuplicateError:
                raise ValidationFailed({"grantee": "That grant already exists."}) from None
            except MissingReferenceError:
                raise ValidationFailed({"grantee": "The grantee no longer exists."}) from None
            current_audit().detail.update(grantee_type=grantee.kind, grantee=grantee.name)

    @audited(
        action="environment_group_access_revoked",
        target_type="environment_group",
        target_id=lambda a: str(a["group_id"]),
    )
    async def revoke(
        self,
        actor: Principal,
        group_id: int,
        *,
        user_id: int | None = None,
        team_id: int | None = None,
    ) -> None:
        """Withdraw a user's or a team's grant on the group."""
        require_administrator(actor)
        await self._load_group(group_id)
        grantee = await resolve_grantee(self._users, self._teams, user_id=user_id, team_id=team_id)
        async with self._db.transaction():
            if grantee.kind == "user":
                removed = await self._grants.remove_user("group", group_id, grantee.id)
            else:
                removed = await self._grants.remove_team("group", group_id, grantee.id)
            if not removed:
                raise ValidationFailed({"grantee": "That grant does not exist."})
            current_audit().detail.update(grantee_type=grantee.kind, grantee=grantee.name)

    async def _load(self, group_id: int) -> GroupSummary:
        group = await self._summaries.get(group_id)
        if group is None:
            raise ResourceNotFound("not found")
        return group

    async def _load_group(self, group_id: int) -> EnvironmentGroup:
        group = await self._groups.get(group_id)
        if group is None:
            raise ResourceNotFound("not found")
        return group

    async def _refuse_taken(self, name: str, *, excluding: int | None) -> None:
        clash = await self._groups.find_by_folded_name(name, excluding=excluding)
        if clash is not None:
            raise ValidationFailed({"name": GROUP_NAME_TAKEN})


def _printable(text: str) -> bool:
    return not has_hidden_characters(text)


def _check_fields(name: str, description: str | None) -> tuple[str, str | None]:
    checked = name.strip()
    fields: dict[str, str] = {}
    if not checked or len(checked) > GROUP_NAME_MAX or not _printable(checked):
        fields["name"] = f"Use 1 to {GROUP_NAME_MAX} printable characters."
    note = None if description is None else description.strip() or None
    if note is not None and (
        len(note) > GROUP_DESCRIPTION_MAX or not _printable(note.replace("\n", " "))
    ):
        fields["description"] = f"Use at most {GROUP_DESCRIPTION_MAX} printable characters."
    if fields:
        raise ValidationFailed(fields)
    return checked, note
