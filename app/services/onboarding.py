"""Adopt everything on one host: a read-only review, then one confirmed, audited apply."""

from typing import Final

from app.auth.errors import ProductError, ValidationFailed
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.image_binding import ImageBindingRepository
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import StackKind
from app.logging import get_logger
from app.services.access import ResourceKey, canonical_stack_key
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.binding_candidates import BindingCandidateScanner, local_digests
from app.services.grantees import OWNER_FIELD, TEAM_FIELD, GranteeDirectory
from app.services.guards import require_administrator
from app.services.image_bindings import ImageBindingService
from app.services.image_jobs import JobProgress
from app.services.image_listing import read_images
from app.services.object_inspector import EnvironmentUnavailable, HostClients, ObjectTooLarge
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.onboarding_objects import MAX_OBJECTS, UncontrolledObjectScanner
from app.services.onboarding_ownership import LEVELS, Ownership
from app.services.onboarding_plan import (
    CHANGED_MESSAGE,
    ApplyItem,
    ApplyResult,
    Dropped,
    ImageTarget,
    ItemStatus,
    ObjectTarget,
    OnboardingReview,
    Plan,
    PlannedControl,
    PlannedImage,
    PlannedStack,
    ProtectedObject,
    Section,
    Selection,
    StackTarget,
    choice_text,
)
from app.services.pull_credentials import CredentialChoice
from app.services.resource_controls import ControlChanged, ControlSpec, ResourceControlService
from app.services.stack_deploy import ChangedSinceReview, StackNameInUse, StackRejected
from app.services.stack_host_read import StackHostReader
from app.services.stack_queries import StackQueryService
from app.services.stacks import StackService
from app.services.swarm_stack_adopt import SwarmStackAdopter
from app.services.swarm_stacks import SwarmStackQueries

logger = get_logger(__name__)

FIELD_BUDGET: Final = 940
FIXED_FIELDS: Final = 8
READING: Final = "reading_stacks"
CHECKING: Final = "checking_images"
SCANNING: Final = "scanning_objects"
HOLDER_ACTION: Final = "onboarding"
NOT_ELIGIBLE: Final = "That target cannot be selected."

_PROTECTED_ORDER: Final = ("stack", "container", "volume", "network", "service", "config", "secret")


class OnboardingChanged(ValidationFailed):
    """The plan no longer matches the one the caller confirmed; nothing was changed."""

    reason = "changed_since_review"

    def __init__(self) -> None:
        super().__init__({"plan": CHANGED_MESSAGE})


def _control_type(arguments: Arguments) -> str:
    planned: PlannedControl = arguments["planned"]
    return planned.target.type


def _control_id(arguments: Arguments) -> str:
    planned: PlannedControl = arguments["planned"]
    return planned.target.id[:255]


def _message_of(error: Exception) -> str:
    return str(error) if isinstance(error, ProductError) else "The operation failed."


class OnboardingService:
    """Administrator-only onboarding of one environment.

    `review` writes only the `stack_files_read` rows of the stack reads, and the helper
    containers those reads create and remove. `apply` never reads the host's files again, so it
    creates no helper container, pulls nothing and starts, stops or removes nothing.
    """

    def __init__(
        self,
        *,
        db: Session,
        authoriser: Authoriser,
        hosts: HostClients,
        stacks: StackService,
        swarm_stacks: SwarmStackAdopter,
        reader: StackHostReader,
        stack_queries: StackQueryService,
        swarm_queries: SwarmStackQueries,
        scanner: BindingCandidateScanner,
        bindings: ImageBindingService,
        binding_rows: ImageBindingRepository,
        controls: ResourceControlService,
        control_rows: ResourceControlRepository,
        objects: UncontrolledObjectScanner,
        grantees: GranteeDirectory,
        locks: ObjectLockRegistry,
        activity: ActivityRecorder,
    ) -> None:
        self._db = db
        self._authoriser = authoriser
        self._hosts = hosts
        self._stacks = stacks
        self._swarm_stacks = swarm_stacks
        self._reader = reader
        self._stack_queries = stack_queries
        self._swarm_queries = swarm_queries
        self._scanner = scanner
        self._bindings = bindings
        self._binding_rows = binding_rows
        self._controls = controls
        self._control_rows = control_rows
        self._objects = objects
        self._grantees = grantees
        self._locks = locks
        self._activity = activity

    async def review(
        self,
        actor: Principal,
        environment_id: int,
        progress: JobProgress | None = None,
    ) -> OnboardingReview:
        """Read every unmanaged stack, check every unbound image and list every uncontrolled object.

        Administrators only. EnvironmentUnavailable and the list-too-large errors when the host
        cannot be read in full. One stack that cannot be read is that stack's `needs a file`.
        """
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        environment = await self._hosts.environment(environment_id)
        await self._hosts.client(environment_id)
        self._stage(progress, READING)
        stacks, protected, dismissed = await self._read_stacks(
            actor, environment_id, environment.type
        )
        self._stage(progress, CHECKING)
        scan = await self._scanner.scan(actor, environment_id)
        images = tuple(ImageTarget(outcome) for outcome in scan.outcomes)
        self._stage(progress, SCANNING)
        offered = [target for target in stacks if target.offered]
        rows = (
            len(offered)
            + sum(1 for image in images if image.offered)
            + sum(1 for image in images if image.outcome.outcome == "ambiguous")
        )
        cap = min(MAX_OBJECTS, max(0, FIELD_BUDGET - FIXED_FIELDS - rows))
        found = await self._objects.scan(
            actor,
            environment_id,
            offered_stacks=[target.name for target in offered],
            cap=cap,
        )
        every = sorted(
            {*protected, *found.protected}, key=lambda p: (_order(p), p.name.casefold(), p.id)
        )
        return OnboardingReview(
            environment_id=environment_id,
            environment_name=environment.name,
            stacks=stacks,
            images=images,
            objects=found.objects,
            protected=tuple(every),
            dismissed=dismissed,
            objects_beyond=found.beyond,
            images_complete=scan.complete,
            objects_complete=found.complete,
        )

    async def resolve_ownership(
        self, actor: Principal, owner_name: str, team_name: str, team_level: str
    ) -> Ownership:
        """Resolve the typed owner and team; ValidationFailed keyed `owner`, `team` and `level`."""
        require_administrator(actor)
        problems: dict[str, str] = {}
        if team_level not in LEVELS:
            problems["level"] = "Choose read or write."
        owner = owner_name.strip()
        team = team_name.strip()
        owner_id: int | None = None
        team_id: int | None = None
        try:
            _, teams, owner_id = await self._grantees.resolve(
                users=(), teams=(team,) if team else (), owner=owner
            )
            team_id = teams[0] if teams else None
        except ValidationFailed as unknown:
            if OWNER_FIELD in unknown.fields:
                problems["owner"] = unknown.fields[OWNER_FIELD]
            if TEAM_FIELD in unknown.fields:
                problems["team"] = unknown.fields[TEAM_FIELD]
        if problems:
            raise ValidationFailed(problems)
        return Ownership(
            owner_user_id=owner_id,
            owner_name=owner if owner_id is not None else "",
            team_id=team_id,
            team_name=team if team_id is not None else "",
            team_level=team_level,
        )

    async def plan(
        self,
        actor: Principal,
        environment_id: int,
        review: OnboardingReview,
        selection: Selection,
    ) -> Plan:
        """Recompute what the selection would write against the host and the database now.

        A ticked target that is no longer eligible is dropped and named. ValidationFailed for
        an index or a credential choice the review does not offer.
        """
        await self._authoriser.require_environment(actor, environment_id)
        require_administrator(actor)
        dropped: list[Dropped] = []
        stacks = await self._plan_stacks(actor, environment_id, review, selection, dropped)
        images = await self._plan_images(environment_id, review, selection, dropped)
        controls, left = await self._plan_controls(
            actor, environment_id, review, selection, dropped
        )
        return Plan(
            stacks=stacks,
            images=images,
            controls=controls,
            dropped=tuple(dropped),
            left=left,
            ownership=selection.ownership,
        )

    async def apply(
        self,
        actor: Principal,
        environment_id: int,
        review: OnboardingReview,
        selection: Selection,
        expected_plan: str,
    ) -> ApplyResult:
        """Write the confirmed plan: stacks, then image bindings, then controls.

        OnboardingChanged, before anything is written, when the plan recomputed now differs from
        `expected_plan`. Each target is its own audited operation; one that changed since the
        recomputation, or a stack whose containers or services changed since it was read, is
        skipped and reported, and the others proceed. A host that cannot be scanned again for
        the controls fails each control and still returns the result.
        """
        plan = await self.plan(actor, environment_id, review, selection)
        if plan.digest != expected_plan:
            raise OnboardingChanged()
        items: list[ApplyItem] = []
        for stack in plan.stacks:
            items.append(await self._adopt(actor, environment_id, stack, plan.ownership))
        for image in plan.images:
            items.append(await self._bind(actor, environment_id, image))
        items.extend(await self._write_controls(actor, environment_id, review, plan))
        return ApplyResult(environment_id, review.environment_name, tuple(items), plan.left)

    async def _read_stacks(
        self, actor: Principal, environment_id: int, environment_type: str | None
    ) -> tuple[tuple[StackTarget, ...], tuple[ProtectedObject, ...], int]:
        own = await self._objects.own_stack_keys(environment_id)
        targets: list[StackTarget] = []
        protected: list[ProtectedObject] = []
        projects, dismissed = await self._stack_queries.undismissed_unmanaged(actor, environment_id)
        for project in projects:
            if canonical_stack_key(project.name) in own:
                protected.append(ProtectedObject("stack", project.name, project.name))
            elif not project.adoptable:
                targets.append(StackTarget("compose", project.name, reason="invalid_name"))
            else:
                targets.append(await self._read_one(actor, environment_id, "compose", project.name))
        if environment_type == "swarm":
            page = await self._swarm_queries.page(actor, environment_id, show_dismissed=False)
            dismissed += page.dismissed_hidden
            for row in page.rows:
                if row.managed:
                    continue
                if canonical_stack_key(row.name) in own:
                    protected.append(ProtectedObject("stack", row.name, row.name))
                elif row.adoptable:
                    targets.append(await self._read_one(actor, environment_id, "swarm", row.name))
                else:
                    reason = "name_in_use" if row.compose_stack else "invalid_name"
                    targets.append(StackTarget("swarm", row.name, reason=reason))
        return tuple(targets), tuple(protected), dismissed

    async def _read_one(
        self, actor: Principal, environment_id: int, kind: str, name: str
    ) -> StackTarget:
        try:
            if kind == "compose":
                report = await self._reader.read_compose(actor, environment_id, name)
            else:
                report = await self._reader.read_swarm(actor, environment_id, name)
        except EnvironmentUnavailable:
            raise
        except ObjectBusy:
            return StackTarget(_kind(kind), name, reason="busy")
        except ProductError:
            return StackTarget(_kind(kind), name, reason="unreadable")
        return StackTarget(_kind(kind), name, report)

    async def _unmanaged(
        self, actor: Principal, environment_id: int, environment_type: str | None
    ) -> set[tuple[str, str]]:
        projects, _ = await self._stack_queries.undismissed_unmanaged(actor, environment_id)
        found = {("compose", project.name) for project in projects}
        if environment_type == "swarm":
            page = await self._swarm_queries.page(actor, environment_id, show_dismissed=False)
            found.update(("swarm", row.name) for row in page.rows if row.adoptable)
        return found

    async def _plan_stacks(
        self,
        actor: Principal,
        environment_id: int,
        review: OnboardingReview,
        selection: Selection,
        dropped: list[Dropped],
    ) -> tuple[PlannedStack, ...]:
        if not selection.stacks:
            return ()
        environment = await self._hosts.environment(environment_id)
        current = await self._unmanaged(actor, environment_id, environment.type)
        planned: list[PlannedStack] = []
        for index in sorted(selection.stacks):
            target = _at(review.stacks, index)
            if not target.offered:
                raise ValidationFailed({"stacks": NOT_ELIGIBLE})
            if (target.kind, target.name) not in current:
                dropped.append(
                    Dropped("stack", target.name, "It is no longer an unmanaged stack on the host.")
                )
            else:
                planned.append(PlannedStack(target, target.digest))
        return tuple(planned)

    async def _plan_images(
        self,
        environment_id: int,
        review: OnboardingReview,
        selection: Selection,
        dropped: list[Dropped],
    ) -> tuple[PlannedImage, ...]:
        if not selection.images:
            return ()
        client = await self._hosts.client(environment_id)
        references = [_at(review.images, index).reference for index in selection.images]
        stored = await self._binding_rows.for_references(environment_id, references)
        local = local_digests(await read_images(client))
        planned: list[PlannedImage] = []
        for index in sorted(selection.images):
            target = _at(review.images, index)
            choice = _chosen(target, selection.images[index])
            digest = target.digest_of[choice]
            name = next((c.name for c in target.outcome.matches if c.choice == choice), None)
            if target.reference in stored:
                dropped.append(Dropped("image", target.reference, "It has a binding now."))
            elif digest not in local.get(target.reference, ()):
                dropped.append(
                    Dropped(
                        "image", target.reference, "The image on the host is not the one checked."
                    )
                )
            else:
                planned.append(PlannedImage(target, choice, name, digest))
        return tuple(planned)

    async def _plan_controls(
        self,
        actor: Principal,
        environment_id: int,
        review: OnboardingReview,
        selection: Selection,
        dropped: list[Dropped],
    ) -> tuple[tuple[PlannedControl, ...], int]:
        if not selection.objects:
            return (), 0
        current = await self._current_objects(actor, environment_id, review)
        ownership = selection.ownership
        planned: list[PlannedControl] = []
        left = 0
        for index in sorted(selection.objects):
            target = _at(review.objects, index)
            now = current.get(target.key)
            if now is None:
                dropped.append(
                    Dropped(
                        "object",
                        f"{target.type} {target.name}",
                        "It is gone or has access settings now.",
                    )
                )
                continue
            if now.owner != target.owner:
                dropped.append(
                    Dropped(
                        "object",
                        f"{target.type} {target.name}",
                        "The containers that use it changed.",
                    )
                )
                continue
            owner = ownership.owner_user_id
            owner_name = ownership.owner_name
            source = None
            if owner is None and target.owner is not None:
                owner, owner_name, source = (
                    target.owner.user_id,
                    target.owner.username,
                    target.source,
                )
            if owner is None and ownership.team_id is None:
                left += 1
                continue
            planned.append(
                PlannedControl(
                    target,
                    owner,
                    owner_name,
                    ownership.team_id,
                    ownership.team_name,
                    ownership.level,
                    source,
                )
            )
        return tuple(planned), left

    async def _current_objects(
        self, actor: Principal, environment_id: int, review: OnboardingReview
    ) -> dict[tuple[str, str], ObjectTarget]:
        scan = await self._objects.scan(
            actor,
            environment_id,
            offered_stacks=[target.name for target in review.stacks if target.offered],
        )
        return {target.key: target for target in scan.objects}

    async def _adopt(
        self, actor: Principal, environment_id: int, planned: PlannedStack, ownership: Ownership
    ) -> ApplyItem:
        target = planned.target
        result = target.result
        if result is None:
            raise ValueError("only a stack with a file can be adopted")
        try:
            if target.kind == "compose":
                await self._stacks.adopt(
                    actor,
                    environment_id,
                    name=target.name,
                    prepared=result,
                    as_is=True,
                    onboarding=ownership,
                )
            else:
                await self._swarm_stacks.adopt(
                    actor,
                    environment_id,
                    target.name,
                    prepared=result,
                    as_is=True,
                    onboarding=ownership,
                )
        except StackNameInUse:
            return _item("stack", target.name, "skipped", "It is managed now.")
        except ChangedSinceReview:
            return _item(
                "stack", target.name, "skipped", "It changed on the host since the review."
            )
        except ObjectBusy:
            return _item("stack", target.name, "skipped", "Another action on it is in progress.")
        except StackRejected as rejected:
            if "name" in rejected.fields:
                return _item("stack", target.name, "skipped", "It is no longer on the host.")
            return _item("stack", target.name, "failed", _message_of(rejected))
        except Exception as error:
            return _failed("stack", target.name, error)
        return _item("stack", target.name, "done", "Adopted as is.")

    async def _bind(
        self, actor: Principal, environment_id: int, planned: PlannedImage
    ) -> ApplyItem:
        reference = planned.target.reference
        if planned.choice.credential_set_id is None:
            return _item("image", reference, "skipped", "It is read without a binding.")
        try:
            outcome = await self._bindings.bind_verified(
                actor, environment_id, reference, planned.choice, digest=planned.digest
            )
        except Exception as error:
            return _failed("image", reference, error)
        if outcome == "skipped":
            return _item("image", reference, "skipped", "It has a binding now.")
        return _item("image", reference, "done", f"Bound to {planned.set_name}.")

    async def _write_controls(
        self, actor: Principal, environment_id: int, review: OnboardingReview, plan: Plan
    ) -> list[ApplyItem]:
        if not plan.controls:
            return []
        try:
            current = await self._current_objects(actor, environment_id, review)
        except (EnvironmentUnavailable, ObjectTooLarge) as error:
            return [
                _failed("object", f"{planned.target.type} {planned.target.name}", error)
                for planned in plan.controls
            ]
        items: list[ApplyItem] = []
        for planned in plan.controls:
            label = f"{planned.target.type} {planned.target.name}"
            now = current.get(planned.target.key)
            if now is None:
                items.append(
                    _item("object", label, "skipped", "It has access settings now or is gone.")
                )
            elif now.owner != planned.target.owner:
                items.append(
                    _item("object", label, "skipped", "The containers that use it changed.")
                )
            else:
                items.append(await self._write_control(actor, environment_id, planned, label))
        return items

    async def _write_control(
        self, actor: Principal, environment_id: int, planned: PlannedControl, label: str
    ) -> ApplyItem:
        target = planned.target
        try:
            key = ObjectKey(environment_id, target.type, target.id)
            holder = LockHolder(UserActor.of(actor), actor.username, HOLDER_ACTION)
            async with self._locks.hold(key, holder):
                status = await self._write_locked(actor, environment_id, planned)
        except ObjectBusy:
            return _item("object", label, "skipped", "Another action on it is in progress.")
        except Exception as error:
            return _failed("object", label, error)
        if status == "skipped":
            return _item("object", label, "skipped", "It has access settings now.")
        return _item("object", label, "done", _describe(planned))

    async def _write_locked(
        self, actor: Principal, environment_id: int, planned: PlannedControl
    ) -> ItemStatus:
        target = planned.target
        if await self._control_rows.get_for(environment_id, target.type, target.id) is not None:
            return "skipped"
        if planned.team_id is None:
            return "done" if await self.claim_owner(actor, environment_id, planned) else "skipped"
        detail: dict[str, str | int | bool | None] = {"onboarding": True}
        if planned.source is not None:
            detail["candidate_source"] = planned.source
        try:
            await self._controls.create(
                actor,
                ResourceKey(environment_id, target.type, target.id),
                ControlSpec(
                    access_level=planned.level,
                    owner_user_id=planned.owner_user_id,
                    team_ids=(planned.team_id,),
                ),
                expected_digest="",
                detail=detail,
            )
        except ControlChanged:
            return "skipped"
        return "done"

    @audited(
        action="resource_control_created",
        target_type=_control_type,
        target_id=_control_id,
        environment_id=environment_argument,
    )
    async def claim_owner(
        self, actor: Principal, environment_id: int, planned: PlannedControl
    ) -> bool:
        """Put an uncontrolled object under a control owned by its planned owner.

        For `apply`, with the object's lock held; no route may call this with a caller's input.
        False, with no row, when the object already has a control.
        """
        require_administrator(actor)
        audit = current_audit()
        audit.detail.update(
            onboarding=True, owner_user_id=planned.owner_user_id, access_level=planned.level
        )
        if planned.source is not None:
            audit.detail["candidate_source"] = planned.source
        async with self._db.transaction():
            claimed = await self._controls.claim(
                ResourceKey(environment_id, planned.target.type, planned.target.id),
                planned.owner_user_id,
                access_level=planned.level,
            )
            if not claimed:
                audit.unchanged()
        return claimed

    @staticmethod
    def _stage(progress: JobProgress | None, stage: str) -> None:
        if progress is not None:
            progress.on_stage(stage)


def _kind(kind: str) -> StackKind:
    return "swarm" if kind == "swarm" else "compose"


def _order(item: ProtectedObject) -> int:
    return (
        _PROTECTED_ORDER.index(item.type)
        if item.type in _PROTECTED_ORDER
        else len(_PROTECTED_ORDER)
    )


def _at[T](items: tuple[T, ...], index: int) -> T:
    if not 0 <= index < len(items):
        raise ValidationFailed({"selection": NOT_ELIGIBLE})
    return items[index]


def _chosen(target: ImageTarget, picked: CredentialChoice | None) -> CredentialChoice:
    offered = tuple(target.digest_of)
    if not target.offered or not offered:
        raise ValidationFailed({"images": NOT_ELIGIBLE})
    if picked is None:
        if len(offered) == 1 and target.outcome.outcome == "binds":
            return offered[0]
        raise ValidationFailed({"images": f"Choose a credential set for {target.reference}."})
    if picked not in target.digest_of:
        raise ValidationFailed(
            {"images": f"{choice_text(picked)} did not read {target.reference}."}
        )
    return picked


def _describe(planned: PlannedControl) -> str:
    parts = []
    if planned.owner_user_id is not None:
        parts.append(f"owner {planned.owner_name}")
    if planned.team_id is not None:
        parts.append(f"team {planned.team_name} ({planned.level})")
    return "Access set: " + ", ".join(parts) + "."


def _item(section: Section, label: str, status: ItemStatus, message: str) -> ApplyItem:
    return ApplyItem(section, label, status, message)


def _failed(section: Section, label: str, error: Exception) -> ApplyItem:
    if not isinstance(error, ProductError):
        logger.error("onboarding_item_failed", extra={"error": type(error).__name__})
    return ApplyItem(section, label, "failed", _message_of(error))
