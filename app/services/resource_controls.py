"""Create, change and remove the resource control over one object."""

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from app.auth.errors import PermissionDenied, ResourceControlNotFound, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.errors import DuplicateError, MissingReferenceError
from app.db.repositories.resource_control import (
    MAX_GRANTS_PER_KIND,
    ResourceControlRepository,
)
from app.services.access import (
    INHERITING_TYPES,
    RESOURCE_TYPES,
    AccessLevel,
    ResourceKey,
    ResourceRef,
    ResourceType,
    canonical_stack_key,
    is_canonical_id,
    stack_of,
)
from app.services.activity import ActivityRecorder, Arguments, audited, current_audit

CONTROL_CHANGED: Final = (
    "The access settings changed since this page was loaded. Review them and save again."
)
CHANGED_REASON: Final = "changed_since_review"
ACCESS_LEVELS: tuple[AccessLevel, ...] = ("read", "write")
RESOURCE_ID_MAX = 255
SUB_RESOURCES_MAX = 5000


@dataclass(frozen=True, slots=True)
class ControlSpec:
    """The desired state of a control, as submitted."""

    access_level: str
    is_public: bool = False
    is_administrators_only: bool = False
    owner_user_id: int | None = None
    user_ids: Collection[int] = ()
    team_ids: Collection[int] = ()


@dataclass(frozen=True, slots=True)
class ControlView:
    """A control as a template sees it."""

    id: int
    key: ResourceKey
    is_public: bool
    is_administrators_only: bool
    access_level: AccessLevel
    owner_user_id: int | None
    user_ids: tuple[int, ...]
    team_ids: tuple[int, ...]
    sub_resource_ids: tuple[str, ...]
    updated_at: str


class ControlChanged(ValidationFailed):
    """The control is no longer the one the caller reviewed; nothing was changed."""

    def __init__(self) -> None:
        super().__init__({"resource": CONTROL_CHANGED})


def control_digest(control: ControlView | None) -> str:
    """Return the SHA-256 hex digest of a control as a form shows it; empty with no control."""
    if control is None:
        return ""
    canonical = json.dumps(
        {
            "public": control.is_public,
            "administrators_only": control.is_administrators_only,
            "access_level": control.access_level,
            "owner": control.owner_user_id,
            "users": sorted(control.user_ids),
            "teams": sorted(control.team_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit_target_type(arguments: Arguments) -> str:
    resource_type = arguments["key"].resource_type
    return resource_type if resource_type in RESOURCE_TYPES else "unknown"


def _audit_target_id(arguments: Arguments) -> str:
    resource_id: str = arguments["key"].resource_id
    return resource_id.strip()[:RESOURCE_ID_MAX]


def _audit_environment(arguments: Arguments) -> int:
    environment_id: int = arguments["key"].environment_id
    return environment_id


def _note_change(key: ResourceKey, spec: ControlSpec) -> None:
    audit = current_audit()
    audit.target_id = key.resource_id
    audit.detail.update(
        access_level=spec.access_level,
        public=str(spec.is_public).lower(),
        administrators_only=str(spec.is_administrators_only).lower(),
        users=str(len(set(spec.user_ids))),
        teams=str(len(set(spec.team_ids))),
    )


class ResourceControlService:
    """Administrator-only management of controls, plus the trusted hooks deploys call."""

    def __init__(
        self,
        *,
        db: Session,
        controls: ResourceControlRepository,
        activity: ActivityRecorder,
    ) -> None:
        self._db = db
        self._controls = controls
        self._activity = activity

    async def get(self, actor: Principal, key: ResourceKey) -> ControlView | None:
        """Return the control over an object, or None if it is uncontrolled."""
        _require_administrator(actor)
        control = await self._controls.get_for(*_parts(_canonical(key)))
        return None if control is None else await self._view(control.id)

    @audited(
        action="resource_control_created",
        target_type=_audit_target_type,
        target_id=_audit_target_id,
        environment_id=_audit_environment,
    )
    async def create(
        self,
        actor: Principal,
        key: ResourceKey,
        spec: ControlSpec,
        *,
        expected_digest: str,
        detail: Mapping[str, DetailValue] | None = None,
    ) -> ControlView:
        """Put an uncontrolled object under a control.

        ControlChanged when the object's control is not the one `expected_digest` was taken from.
        `detail` is added to the activity row; only a trusted service passes it.
        """
        _require_administrator(actor)
        async with self._db.transaction():
            existing = await self._controls.get_for(*_parts(_canonical(key)))
            await self._require_reviewed(None if existing is None else existing.id, expected_digest)
            key, checked = _check(key, spec)
            try:
                control = await self._controls.create(
                    *_parts(key),
                    is_public=spec.is_public,
                    is_administrators_only=spec.is_administrators_only,
                    access_level=checked,
                    owner_user_id=spec.owner_user_id,
                )
                await self._controls.replace_grants(
                    control.id, user_ids=spec.user_ids, team_ids=spec.team_ids
                )
            except DuplicateError:
                raise ValidationFailed({"resource": "This object already has a control."}) from None
            except MissingReferenceError:
                raise ValidationFailed({"grants": "A user or team no longer exists."}) from None
            _note_change(key, spec)
            if detail:
                current_audit().detail.update(detail)
            view = await self._view(control.id)
        return view

    @audited(
        action="resource_control_updated",
        target_type=_audit_target_type,
        target_id=_audit_target_id,
        environment_id=_audit_environment,
    )
    async def update(
        self, actor: Principal, key: ResourceKey, spec: ControlSpec, *, expected_digest: str
    ) -> ControlView:
        """Replace the flags, level, owner and grants of an existing control.

        ControlChanged when the control is not the one `expected_digest` was taken from.
        """
        _require_administrator(actor)
        async with self._db.transaction():
            control = await self._controls.get_for(*_parts(_canonical(key)))
            if control is None:
                raise ResourceControlNotFound("this object has no control")
            await self._require_reviewed(control.id, expected_digest)
            key, checked = _check(key, spec)
            try:
                await self._controls.update(
                    control.id,
                    is_public=spec.is_public,
                    is_administrators_only=spec.is_administrators_only,
                    access_level=checked,
                    owner_user_id=spec.owner_user_id,
                )
                await self._controls.replace_grants(
                    control.id, user_ids=spec.user_ids, team_ids=spec.team_ids
                )
            except MissingReferenceError:
                raise ValidationFailed({"grants": "A user or team no longer exists."}) from None
            _note_change(key, spec)
            view = await self._view(control.id)
        return view

    @audited(
        action="resource_control_deleted",
        target_type=_audit_target_type,
        target_id=_audit_target_id,
        environment_id=_audit_environment,
    )
    async def delete(self, actor: Principal, key: ResourceKey, *, expected_digest: str) -> None:
        """Remove an object's control, returning it to the uncontrolled default.

        ControlChanged when the control is not the one `expected_digest` was taken from.
        """
        _require_administrator(actor)
        key = _canonical(key)
        async with self._db.transaction():
            control = await self._controls.get_for(*_parts(key))
            if control is None:
                raise ResourceControlNotFound("this object has no control")
            await self._require_reviewed(control.id, expected_digest)
            await self._controls.delete_for(*_parts(key))
            current_audit().target_id = key.resource_id

    async def release(self, key: ResourceKey) -> bool:
        """Drop the control of an object that has just been removed from Docker.

        For the service that removed it, after the removal succeeded; no route may call this with
        a caller's input. A stack is keyed by name, so a control left behind would govern the next
        stack deployed under that name.
        """
        return await self._controls.delete_for(*_parts(_canonical(key)))

    async def claim(
        self,
        key: ResourceKey,
        owner_user_id: int | None,
        *,
        administrators_only: bool = False,
        team_ids: Collection[int] = (),
        access_level: str = "write",
    ) -> bool:
        """Put an object that was just created under a control from the moment it exists.

        For the service that created it, inside its audited operation; no route may call this
        with a caller's input. By default a write control owned by owner_user_id that grants
        nothing to anyone else; administrators_only instead makes it visible to administrators
        alone, and team_ids grant the teams access at `access_level`. False, with nothing
        changed, if the object already has a control. Never public.
        """
        key, level = _check(
            key,
            ControlSpec(
                access_level=access_level,
                is_administrators_only=administrators_only,
                owner_user_id=owner_user_id,
                team_ids=team_ids,
            ),
        )
        try:
            control = await self._controls.create(
                *_parts(key),
                is_public=False,
                is_administrators_only=administrators_only,
                access_level=level,
                owner_user_id=owner_user_id,
            )
        except DuplicateError:
            return False
        if team_ids:
            try:
                await self._controls.replace_grants(control.id, user_ids=(), team_ids=team_ids)
            except MissingReferenceError:
                raise ValidationFailed({"grants": "A user or team no longer exists."}) from None
        return True

    async def transfer(self, old: ResourceKey, new: ResourceKey) -> bool:
        """Move an object's control, owner and grants to the id that replaced it.

        For the service that replaced the object, inside its audited operation; no route may
        call this with a caller's input. False if the old object had no control. Both keys must
        name one type in one environment, and the new id must not already have a control.
        """
        old, new = _same_kind(old, new)
        return await self._controls.rekey(*_parts(old), new.resource_id)

    async def copy(self, old: ResourceKey, new: ResourceKey) -> bool:
        """Give the id that replaced an object a copy of its control, owner and grants.

        For the service that replaced the object while it still exists under another id, inside
        its audited operation; no route may call this with a caller's input. False if the old
        object had no control. The rules for the keys are those of transfer.
        """
        old, new = _same_kind(old, new)
        return await self._controls.copy_to(*_parts(old), new.resource_id)

    async def refresh_sub_resources(
        self, stack: ResourceKey, members: Iterable[ResourceRef]
    ) -> bool:
        """Replace a stack control's display cache from the objects Docker reports now.

        Only members whose own labels name this stack are kept. The cache feeds display only; no
        decision reads it.
        """
        if stack.resource_type != "stack":
            raise ValueError("only a stack has sub-resources")
        control = await self._controls.get_for(*_parts(stack))
        if control is None:
            return False
        stack_key = canonical_stack_key(stack.resource_id)
        kept = sorted(
            {
                f"{member.resource_type}:{member.resource_id}"
                for member in members
                if member.environment_id == stack.environment_id
                and member.resource_type in INHERITING_TYPES
                and stack_of(member.labels) == stack_key
            }
        )
        return await self._controls.set_sub_resource_ids(control.id, kept[:SUB_RESOURCES_MAX])

    async def _require_reviewed(self, control_id: int | None, expected_digest: str) -> None:
        current = None if control_id is None else await self._view(control_id)
        if control_digest(current) != expected_digest:
            current_audit().reason = CHANGED_REASON
            raise ControlChanged()

    async def _view(self, control_id: int) -> ControlView:
        current = await self._controls.get(control_id)
        if current is None:
            raise ResourceControlNotFound("this object has no control")
        grants = await self._controls.list_grants(control_id)
        return ControlView(
            id=current.id,
            key=ResourceKey(current.environment_id, current.resource_type, current.resource_id),
            is_public=current.is_public,
            is_administrators_only=current.is_administrators_only,
            access_level=current.access_level,
            owner_user_id=current.owner_user_id,
            user_ids=grants.user_ids,
            team_ids=grants.team_ids,
            sub_resource_ids=current.sub_resource_ids,
            updated_at=current.updated_at,
        )


def _require_administrator(actor: Principal) -> None:
    if not actor.is_administrator:
        raise PermissionDenied("administrator role required")


def _parts(key: ResourceKey) -> tuple[int, ResourceType, str]:
    return key.environment_id, key.resource_type, key.resource_id


def _canonical(key: ResourceKey) -> ResourceKey:
    """Return the key as the control is stored: trimmed, and for a stack in canonical case."""
    resource_id = key.resource_id.strip()
    if key.resource_type == "stack":
        resource_id = canonical_stack_key(resource_id)
    return ResourceKey(key.environment_id, key.resource_type, resource_id)


def _same_kind(old: ResourceKey, new: ResourceKey) -> tuple[ResourceKey, ResourceKey]:
    old, new = _canonical(old), _canonical(new)
    if (old.environment_id, old.resource_type) != (new.environment_id, new.resource_type):
        raise ValueError("a control moves only between objects of one type in one environment")
    if not new.resource_id or not is_canonical_id(new.resource_type, new.resource_id):
        raise ValueError("the new object id is not a valid id for its type")
    return old, new


def _check(key: ResourceKey, spec: ControlSpec) -> tuple[ResourceKey, AccessLevel]:
    """Validate a key and spec; return the stored form of the key and the narrowed level."""
    problems: dict[str, str] = {}
    # Judged before folding, which maps some non-ASCII letters (an eszett) onto ASCII ones.
    submitted_ascii = key.resource_id.isascii()
    key = _canonical(key)
    if key.resource_type not in RESOURCE_TYPES:
        problems["resource_type"] = "Unknown resource type."
    resource_id = key.resource_id
    if (
        not resource_id
        or len(resource_id) > RESOURCE_ID_MAX
        or any(ord(char) < 32 for char in resource_id)
    ):
        problems["resource_id"] = "The object identifier is not valid."
    elif key.resource_type == "stack" and not submitted_ascii:
        # The stored form is compared with SQLite lower(), which folds ASCII only, and Docker
        # accepts no other stack name.
        problems["resource_id"] = "A stack name uses ASCII characters only."
    elif key.resource_type in RESOURCE_TYPES and not is_canonical_id(
        key.resource_type, resource_id
    ):
        problems["resource_id"] = "Use the full object id; a short id or a name governs nothing."
    if spec.is_public and spec.is_administrators_only:
        problems["is_public"] = "An object cannot be both public and administrators-only."
    if (spec.is_public or spec.is_administrators_only) and (spec.user_ids or spec.team_ids):
        problems["grants"] = "Grants apply only to an object that is neither public nor restricted."
    if (
        len(set(spec.user_ids)) > MAX_GRANTS_PER_KIND
        or len(set(spec.team_ids)) > MAX_GRANTS_PER_KIND
    ):
        problems["grants"] = f"At most {MAX_GRANTS_PER_KIND} users and {MAX_GRANTS_PER_KIND} teams."
    level = next((known for known in ACCESS_LEVELS if known == spec.access_level), None)
    if level is None:
        problems["access_level"] = "Choose read or write."
    if problems or level is None:
        raise ValidationFailed(problems)
    return key, level
