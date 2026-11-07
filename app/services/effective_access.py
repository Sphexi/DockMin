"""The effective-access view: what one user can reach, and the reason for each answer."""

from collections.abc import Sequence
from dataclasses import dataclass

from app.auth.errors import PermissionDenied, UserNotFound
from app.auth.principal import Principal
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.user import Role, UserRepository
from app.services.access import AccessLevel, Controllable, Reason, ResourceType
from app.services.access_policy import AccessPolicyService, UncontrolledDefault
from app.services.authorisation import Authoriser


@dataclass(frozen=True, slots=True)
class AccessExplanation:
    """One object's outcome for the user, with every reason that applies."""

    resource_id: str
    level: AccessLevel | None
    reasons: tuple[Reason, ...]
    via_teams: tuple[str, ...]
    inherited_from: str | None


@dataclass(frozen=True, slots=True)
class EffectiveAccess:
    """The account examined, the default in force, and an explanation per object."""

    user_id: int
    username: str
    role: Role
    is_enabled: bool
    uncontrolled_default: UncontrolledDefault
    environment_access: bool
    entries: tuple[AccessExplanation, ...]


class EffectiveAccessService:
    """Explains the model by running it: the answers come from the same predicate as enforcement."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        policy: AccessPolicyService,
        controls: ResourceControlRepository,
        users: UserRepository,
    ) -> None:
        self._authoriser = authoriser
        self._policy = policy
        self._controls = controls
        self._users = users

    async def explain(
        self,
        actor: Principal,
        user_id: int,
        environment_id: int,
        resource_type: ResourceType,
        items: Sequence[Controllable],
    ) -> EffectiveAccess:
        """Explain the user's access to each supplied object; administrators only.

        Refusals are listed with their reasons, so a standard user may not call it for themselves.
        """
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        user = await self._users.get(user_id)
        if user is None:
            raise UserNotFound(f"user {user_id} does not exist")
        subject = Principal(user.id, user.username, user.role)
        visibility = await self._authoriser.resolve_visibility(
            subject, environment_id, resource_type, {item.resource_id for item in items}
        )
        team_names: dict[int, tuple[str, ...]] = {}
        entries: list[AccessExplanation] = []
        for item in items:
            decision = visibility.decide(item.resource_id, item.labels)
            via: tuple[str, ...] = ()
            if Reason.TEAM_GRANT in decision.reasons and decision.control_id is not None:
                if decision.control_id not in team_names:
                    team_names[decision.control_id] = tuple(
                        await self._controls.list_granting_team_names(decision.control_id, user.id)
                    )
                via = team_names[decision.control_id]
            entries.append(
                AccessExplanation(
                    item.resource_id, decision.level, decision.reasons, via, decision.inherited_from
                )
            )
        return EffectiveAccess(
            user.id,
            user.username,
            user.role,
            user.is_enabled,
            await self._policy.uncontrolled_default_setting(),
            await self._authoriser.environment_visible(subject, environment_id),
            tuple(entries),
        )
