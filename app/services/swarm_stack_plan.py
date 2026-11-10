"""Swarm stack planning: validation, restriction layers and the diff against the host."""

import asyncio
import copy
import hashlib
import ipaddress
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.compose_cli.errors import (
    ComposeBusy,
    ComposeCommandFailed,
    ComposeError,
    ComposeOutputInvalid,
    ComposeOutputTooLarge,
    ComposeTimedOut,
)
from app.compose_cli.parsers import ResolvedProject
from app.compose_cli.sources import number_sources
from app.db.repositories.environment import Environment
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.db.repositories.resource_control import ResourceControlRepository
from app.db.repositories.stack import StackRepository, StoredStack
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, ResponseTooLarge
from app.docker_engine.errors import NotFound as EngineNotFound
from app.docker_engine.models import (
    ConfigDocument,
    Network,
    ServiceDocument,
    SwarmConfig,
    SwarmSecret,
    Volume,
)
from app.logging import get_logger
from app.registry_client.references import ReferenceInvalid, parse_reference
from app.services.access import SWARM_NAMESPACE_LABEL, ResourceKey, canonical_stack_key
from app.services.acting import Acting, AutomationAuthority, is_administrator
from app.services.authorisation import Authoriser
from app.services.compose_setup import ComposeStatus, build_target
from app.services.container_restrictions import Finding
from app.services.guards import require_administrator
from app.services.image_bindings import CredentialOffer, RebindRefused, parse_pull_reference
from app.services.imported_volumes import ImportedVolumes
from app.services.object_inspector import (
    EngineObjectInspector,
    HostClients,
    ObjectTooLarge,
    unavailable_from,
)
from app.services.object_locks import ObjectBusy, ObjectKey, ObjectLockRegistry
from app.services.pull_credentials import (
    CandidateListTooLarge,
    CredentialChoice,
    CredentialChoiceRequired,
    CredentialSetMismatch,
    PullCredentialResolver,
)
from app.services.registry_resolution import CredentialSetUnavailable
from app.services.service_restrictions import find_restricted_service, references
from app.services.service_spec import mode_of
from app.services.stack_collisions import CollisionChecker
from app.services.stack_common import stronger_reason
from app.services.stack_deploy import (
    CONFIG_SLOT_WAIT_SECONDS,
    FindingView,
    PreexistingObjects,
    ServiceSummary,
    StackBusy,
    StackNameInUse,
    StackRejected,
    compose_to_stack_error,
    file_digest,
    file_secret_values,
    hide_keys,
    make_scrubber,
    raise_if_refused,
    secret_env_keys,
)
from app.services.stack_document import (
    COMPOSE_FIELD,
    CompanionFile,
    ComposeDocumentRejected,
    VariableInput,
    precheck,
    swarm_stack_name,
)
from app.services.stack_queries import StackQueryService, host_projects
from app.services.stack_restrictions import (
    StackFinding,
    limits,
    refused_stack_options,
    reserved_labels,
    resolved_digest,
    scan,
    stack_option_label,
    swarm_refusals,
)
from app.services.stack_views import RestrictedOption, ValidationReport, VariableSet
from app.services.swarm import SwarmListTooLarge, swarm_read
from app.services.swarm_common import (
    CONFIG_UNAVAILABLE,
    NETWORK_UNAVAILABLE,
    SECRET_UNAVAILABLE,
    PinnedImage,
    manager_node_id,
    pin_image,
    resolve_file,
    resolve_network,
    split_service_image,
    volume_reach,
)
from app.services.swarm_gate import require_swarm
from app.services.swarm_stack_translate import (
    FileItem,
    ServiceItem,
    Source,
    Translation,
    TranslationInput,
    TranslationRejected,
    bind_references,
    compose_path,
    translate,
)
from app.services.swarm_stacks import (
    NAMESPACE_ROUTE,
    NamespaceMembers,
    members_digest,
    read_namespace,
    read_namespace_volumes,
)

__all__ = [
    "NETWORK_DIFFERS",
    "SWARM_AUTOMATION_REFUSALS",
    "UNWRITABLE_MEMBERS",
    "ImageMove",
    "PlanAction",
    "PlanKind",
    "PlanLine",
    "PlannedService",
    "StackCredentialsRefused",
    "SwarmDeployPlan",
    "SwarmRemovePlan",
    "SwarmStackPlanner",
    "bound_spec",
    "drifted_services",
    "network_ids",
    "plan_digest",
    "spec_differs",
]

logger = get_logger(__name__)

PLACEHOLDER_TOKEN: Final = "0" * 12
PLACEHOLDER_ID: Final = "0" * 25
VXLAN_OPTION: Final = "com.docker.network.driver.overlay.vxlanid_list"
UNWRITABLE_MEMBERS: Final = "You cannot change every object in this stack."
NETWORK_DIFFERS: Final = (
    "This network exists with other settings, and Docker cannot change a network in place."
)
MODE_CHANGE: Final = "Swarm cannot change a service between replicated and global mode."
EXTERNAL_NETWORK_UNUSABLE: Final = "This network cannot be used by a service."
SECRET_IN_CONFIG: Final = (
    "This config contains the value of a secret variable, which every reader of configs would see."
)
SECRET_IN_SERVICE: Final = (
    "This value contains the value of a secret variable, which every reader of services would see."
)
ENVIRONMENT_PATH: Final = "TaskTemplate.ContainerSpec.Env"
_EQUIVALENT_OPTIONS: Final[Mapping[str, str]] = {"volume_driver": "volume_option"}
SWARM_AUTOMATION_REFUSALS: Final = frozenset(
    {"pending_changes", "would_remove", "would_create", "drifted", "unresolved"}
)

type CredentialRefusal = Literal["credential_mismatch", "credential_unavailable"]
type PlanAction = Literal["create", "update", "remove", "keep"]
type PlanKind = Literal["network", "config", "secret", "service"]
type Level = Literal["read", "write"]


def _no_poke(_environment_id: int) -> None:
    return None


class StackCredentialsRefused(StackRejected):
    """A stack whose images' credentials could not be settled, with the reason for each reference.

    `detail["refused_references"]` maps the reference as written to the credential reason; `reason`
    stays the single overall reason of the planner.
    """

    def __init__(
        self,
        fields: dict[str, str],
        reason: str,
        refused: Mapping[str, CredentialRefusal],
    ) -> None:
        super().__init__(fields, reason)
        self.detail: dict[str, dict[str, CredentialRefusal]] = {"refused_references": dict(refused)}


@dataclass(frozen=True, slots=True)
class PlanLine:
    """One object the plan acts on, or deliberately leaves; `detail` is a fixed phrase."""

    action: PlanAction
    kind: PlanKind
    name: str
    object_id: str | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PlannedService:
    """A service to create or update, with what the sequence needs to send it.

    `service_id`, `version` and `force_update` are those read when the plan was made and are
    read again under the service's lock. `image` is the reference the spec will run, pinned when
    the digest lookup worked; `reference` is the image as written, the key of `pinned`.
    `current_spec` is the Engine's spec of an existing service when the plan read it.
    """

    item: ServiceItem = field(repr=False)
    action: Literal["create", "update"]
    image: str
    reference: str
    service_id: str | None
    version: int | None
    force_update: int
    current_image: str | None
    current_spec: Mapping[str, Any] | None = field(default=None, repr=False)

    @property
    def image_changed(self) -> bool:
        """Report whether an update changes the image string, which needs an explicit credential."""
        return self.action == "update" and self.current_image != self.image


@dataclass(frozen=True, slots=True)
class ImageMove:
    """A service whose image string changes; `pinned` is false when the digest lookup failed."""

    service: str
    reference: str
    current: str | None
    target: str
    pinned: bool


@dataclass(frozen=True, slots=True)
class SwarmDeployPlan:
    """What a deploy or redeploy would do, bound to the digest a confirmation carries.

    `lines` are in execution order: networks, configs, secrets, services, services no longer in
    the file, replaced configs and secrets, networks no longer declared. `files` holds the
    (kind, name) -> (id, name) of every config and secret that exists now, stack-owned or
    external, and `external_networks` the name -> id of every external network. `pinned` maps
    each image reference as written to its credentials and digest, None for an image id.
    `warnings` are the collision check's, empty unless it was asked for.
    """

    stack_name: str
    translation: Translation = field(repr=False)
    lines: tuple[PlanLine, ...]
    digest: str
    pinned: Mapping[str, PinnedImage | None] = field(repr=False)
    restricted: tuple[str, ...]
    compose_digest: str
    resolved_digest: str
    remove_orphans: bool
    services: tuple[PlannedService, ...] = ()
    warnings: tuple[str, ...] = ()
    files: Mapping[tuple[str, str], tuple[str, str]] = field(default_factory=dict, repr=False)
    external_networks: Mapping[str, str] = field(default_factory=dict)
    unpinned: tuple[str, ...] = ()
    image_moves: tuple[ImageMove, ...] = ()
    adopted_first_redeploy: bool = False
    members: int = 0

    def with_action(self, action: PlanAction, kind: PlanKind) -> tuple[PlanLine, ...]:
        """Return the lines of one action and kind, in plan order."""
        return tuple(line for line in self.lines if line.action == action and line.kind == kind)


@dataclass(frozen=True, slots=True)
class SwarmRemovePlan:
    """Every object carrying the stack's namespace, to be removed, and the volumes that stay.

    `digest` is the SHA-256 over the sorted member ids (the value a remove confirmation binds).
    """

    stack_id: int
    members: tuple[PlanLine, ...]
    digest: str
    volumes_kept: tuple[str, ...]


def plan_digest(lines: Sequence[PlanLine], extras: Sequence[str] = ()) -> str:
    """Return the SHA-256 over the sorted `action kind name` lines and any extra bound lines."""
    text = "\n".join(
        sorted([*(f"{line.action} {line.kind} {line.name}" for line in lines), *extras])
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bound_spec(
    plan: SwarmDeployPlan,
    planned: PlannedService,
    created: Mapping[tuple[str, str], tuple[str, str]] | None = None,
    *,
    force_update: int | None = None,
) -> dict[str, Any]:
    """Return the spec to send: the pinned image, config and secret ids, external network ids.

    `created` holds (kind, name) -> (id, name) of the configs and secrets the sequence created.
    `force_update` replaces the counter read when the plan was made; pass the one read under
    the service's lock. LookupError when a referenced config or secret is in neither mapping.
    """
    spec = bind_references(
        planned.item.spec,
        image=planned.image,
        files={**plan.files, **(created or {})},
        networks=plan.external_networks,
    )
    spec["TaskTemplate"]["ForceUpdate"] = (
        planned.force_update if force_update is None else force_update
    )
    return spec


def network_ids(plan: SwarmDeployPlan) -> dict[str, str]:
    """Return the id of every network the plan names that exists now, by name."""
    return {
        **{
            line.name: line.object_id
            for line in plan.lines
            if line.kind == "network" and line.object_id is not None
        },
        **plan.external_networks,
    }


def _comparable(spec: Mapping[str, Any], networks: Mapping[str, str]) -> dict[str, Any]:
    """Copy a service spec with the values the Engine fills in or rewrites made equal."""
    copied = copy.deepcopy(dict(spec))
    task = copied.get("TaskTemplate")
    if not isinstance(task, dict):
        return copied
    if task.get("Runtime") == "container":
        del task["Runtime"]
    container = task.get("ContainerSpec")
    if isinstance(container, dict) and container.get("Isolation") == "default":
        del container["Isolation"]
    attachments = task.get("Networks")
    if isinstance(attachments, list):
        for entry in attachments:
            if isinstance(entry, dict) and isinstance(entry.get("Target"), str):
                entry["Target"] = networks.get(entry["Target"], entry["Target"])
    return copied


def spec_differs(
    sent: Mapping[str, Any], held: Mapping[str, Any], networks: Mapping[str, str]
) -> bool:
    """Report whether two service specs differ once the Engine's own defaults are set aside.

    The Engine stores `Isolation: default` and `Runtime: container` unasked and stores a network
    attachment by id where the file names it; `networks` maps names to ids. Every other
    difference counts.
    """
    return _comparable(sent, networks) != _comparable(held, networks)


def _same_reference(running: str, written: str) -> bool:
    """Report whether a running image is a digest-pinned form of the reference the file names."""
    image = split_service_image(running)
    if image.kind != "tagged_digest":
        return False
    try:
        wanted = split_service_image(written).reference
        return (
            wanted is not None
            and image.reference is not None
            and parse_reference(image.reference).key == parse_reference(wanted).key
        )
    except ReferenceInvalid:
        return False


def drifted_services(plan: SwarmDeployPlan) -> tuple[str, ...]:
    """Name the services to update whose running spec differs from the plan's, bar the image.

    The force-update counter and the image digest are the two things a redeploy is meant to
    change, so they are put back before comparing. A running image that is not a digest-pinned
    form of the file's own reference counts as drift. A service whose spec cannot be built or
    was not read counts as drifted.
    """
    ids = network_ids(plan)
    found: list[str] = []
    for planned in plan.services:
        if planned.action != "update":
            continue
        held = planned.current_spec
        if held is None:
            found.append(planned.item.name)
            continue
        try:
            sent = bound_spec(plan, planned, force_update=planned.force_update)
        except LookupError:
            found.append(planned.item.name)
            continue
        current = planned.current_image
        if current is not None and current != planned.image:
            if not _same_reference(current, planned.reference):
                found.append(planned.item.name)
                continue
            sent["TaskTemplate"]["ContainerSpec"]["Image"] = current
        if spec_differs(sent, held, ids):
            found.append(planned.item.name)
    return tuple(found)


async def _owner_access(authority: AutomationAuthority, key: ResourceKey, level: Level) -> None:
    try:
        lock_key = ObjectKey(key.environment_id, key.resource_type, key.resource_id)
    except ValueError:
        raise PermissionDenied("insufficient access") from None
    if level == "write":
        await authority.authorise(lock_key)
    else:
        await authority.authorise_read(lock_key)


@dataclass(slots=True)
class _Loaded:
    """The inputs of one planning run after the cheap checks, before any Engine read."""

    environment: Environment
    client: EngineClient
    stack: StoredStack | None
    name: str
    text: str
    values: VariableSet = field(repr=False)
    secret_names: frozenset[str]
    tokens: dict[str, str] = field(repr=False)
    files: tuple[CompanionFile, ...] = field(default=(), repr=False)
    file_tokens: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class _Delivery:
    """What the companion files give a run: sources for the translator and strings to redact."""

    sources: Mapping[str, Source] = field(repr=False)
    companions: tuple[str, ...]
    secrets: frozenset[str] = field(repr=False)
    secret_keys: frozenset[str] = field(repr=False, default=frozenset())
    whole_files: frozenset[str] = field(repr=False, default=frozenset())


@dataclass(slots=True)
class _Checks:
    findings: list[StackFinding] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    gaps: int = 0


@dataclass(slots=True)
class _Cluster:
    members: NamespaceMembers
    documents: dict[str, ServiceDocument]


class SwarmStackPlanner:
    """Validates a Swarm stack and plans what a deploy, redeploy or remove would do.

    Nothing here changes the host or the database. Credentials are resolved here, so none of
    these methods may be called inside a write transaction.
    """

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        hosts: HostClients,
        stacks: StackRepository,
        queries: StackQueryService,
        credentials: PullCredentialResolver,
        control_repo: ResourceControlRepository,
        tls: EnvironmentTlsRepository,
        locks: ObjectLockRegistry,
        poke: Callable[[int], None] | None = None,
        imported: ImportedVolumes | None = None,
    ) -> None:
        self._imported = imported
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = hosts
        self._stacks = stacks
        self._queries = queries
        self._credentials = credentials
        self._control_repo = control_repo
        self._tls = tls
        self._locks = locks
        self._poke = poke or _no_poke
        self._collisions = CollisionChecker(authoriser=authoriser, inspector=inspector)

    async def require_stack(
        self, actor: Acting, environment_id: int, name: str, level: Level = "read"
    ) -> StoredStack:
        """Return the managed Swarm stack the caller holds the level over, found by folded name.

        ResourceNotFound for a missing or hidden stack, a Compose stack, a malformed name, or an
        environment that is not a Swarm manager the caller may see, all alike; PermissionDenied
        for a caller who may see it but not at the level. An automation run is checked as the
        rule's owner is now.
        """
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        if NAMESPACE_ROUTE.fullmatch(name) is None:
            raise ResourceNotFound("not found")
        row = await self._stacks.get_folded(environment_id, name)
        if row is None or row.kind != "swarm":
            raise ResourceNotFound("not found")
        await self._require_stack_access(actor, environment_id, row, level)
        return row

    async def _require_stack_access(
        self, actor: Acting, environment_id: int, row: StoredStack, level: Level
    ) -> None:
        if isinstance(actor, AutomationAuthority):
            await _owner_access(
                actor,
                ResourceKey(environment_id, "stack", canonical_stack_key(row.name)),
                level,
            )
        elif not actor.is_administrator:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "stack", row.name), level
            )

    async def busy_error(
        self, actor: Principal, environment_id: int, name: str
    ) -> ObjectBusy | None:
        """Return the refusal an action on the stack would meet now, or None; needs read."""
        row = await self.require_stack(actor, environment_id, name, "read")
        holder = self._locks.holder_of(
            ObjectKey(environment_id, "stack", canonical_stack_key(row.name))
        )
        return None if holder is None else ObjectBusy(holder)

    async def validate(
        self,
        actor: Principal,
        environment_id: int,
        *,
        name: str,
        compose_text: str,
        variables: Sequence[VariableInput] | None,
        existing: bool = False,
        adopt: bool = False,
        choices: Mapping[str, CredentialChoice] | None = None,
        resolve_images: bool = False,
        check_collisions: bool = False,
        files: Sequence[CompanionFile] | None = None,
    ) -> ValidationReport:
        """Check a candidate file and variables against the host; nothing is stored or created.

        See `candidate`.
        """
        return _report_of(
            await self.candidate(
                actor,
                environment_id,
                name=name,
                compose_text=compose_text,
                variables=variables,
                existing=existing,
                adopt=adopt,
                choices=choices,
                resolve_images=resolve_images,
                check_collisions=check_collisions,
                files=files,
            )
        )

    async def candidate(
        self,
        actor: Principal,
        environment_id: int,
        *,
        name: str,
        compose_text: str,
        variables: Sequence[VariableInput] | None,
        existing: bool = False,
        adopt: bool = False,
        choices: Mapping[str, CredentialChoice] | None = None,
        resolve_images: bool = False,
        check_collisions: bool = False,
        files: Sequence[CompanionFile] | None = None,
    ) -> SwarmDeployPlan:
        """Plan a candidate file and variables against the host; nothing is stored or created.

        `existing` validates a change to the stack called `name`, which needs write and every
        member writable. `adopt` validates a file for the namespace called `name`, which only an
        administrator may do and which must exist. Otherwise the caller needs create and the name
        must be free across stacks, Compose projects and namespaces. With `resolve_images` the
        credentials and digests are settled too, so a choice still needed is raised. With
        `check_collisions` published ports are compared with the host: a port another service
        holds is refused, the rest are warnings on the report. `files` is the candidate companion
        file set; without it a stack's stored files are used and a new stack has none.
        StackRejected carries the fields, findings and Compose's scrubbed stderr.
        """
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        if existing and adopt:
            raise ValueError("a validation is for an existing stack or an adoption, not both")
        stack: StoredStack | None = None
        if existing:
            stack = await self.require_stack(actor, environment_id, name, "write")
            name = stack.name
        elif adopt:
            require_administrator(actor)
            name = _checked_name(name)
        else:
            await self._authoriser.require_create(actor, environment_id)
            name = _checked_name(name)
        client = await self._hosts.client(environment_id)
        if adopt:
            await self.require_adoptable(environment_id, name, client)
        elif stack is None:
            await self._queries.require_free(environment_id, name, client)
        values = await self._queries.variable_set(stack, variables)
        loaded = await self._load(
            environment,
            client,
            stack,
            name,
            compose_text,
            values,
            submitted=variables is not None,
            candidate=files,
        )
        return await self._plan(
            actor,
            loaded,
            remove_orphans=False,
            choices=choices,
            resolve_images=resolve_images,
            documents=False,
            check_collisions=check_collisions,
        )

    async def deploy_plan(
        self,
        actor: Acting,
        environment_id: int,
        stack: StoredStack,
        *,
        remove_orphans: bool,
        choices: Mapping[str, CredentialChoice] | None = None,
        resolve_images: bool = True,
        check_collisions: bool = False,
    ) -> SwarmDeployPlan:
        """Plan a deploy or redeploy of the stored file and variables against the host now.

        The caller needs write on the stack and, as a standard user, on every member. With
        `resolve_images` false no credential is resolved and nothing is pinned. With
        `check_collisions` the plan refuses a published port another service holds, and carries
        the warnings.
        """
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        if stack.kind != "swarm" or stack.environment_id != environment_id:
            raise ResourceNotFound("not found")
        await self._require_stack_access(actor, environment_id, stack, "write")
        client = await self._hosts.client(environment_id)
        values = await self._queries.variable_set(stack, None)
        loaded = await self._load(
            environment, client, stack, stack.name, stack.compose_file, values, submitted=False
        )
        return await self._plan(
            actor,
            loaded,
            remove_orphans=remove_orphans,
            choices=choices,
            resolve_images=resolve_images,
            documents=True,
            check_collisions=check_collisions,
        )

    async def remove_plan(
        self, actor: Principal, environment_id: int, stack: StoredStack
    ) -> SwarmRemovePlan:
        """Name every object carrying the stack's namespace on the host now.

        StackRejected (`restricted`) for a standard user who cannot change every one of them.
        """
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        if stack.kind != "swarm" or stack.environment_id != environment_id:
            raise ResourceNotFound("not found")
        if not actor.is_administrator:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "stack", stack.name), "write"
            )
        client = await self._hosts.client(environment_id)
        key = canonical_stack_key(stack.name)
        members = await read_namespace(client, environment_id, self._poke, key)
        await self._require_writable(actor, environment_id, members)
        volumes = await read_namespace_volumes(client, key)
        return SwarmRemovePlan(
            stack_id=stack.id,
            members=_member_lines(members),
            digest=members_digest(members.ids),
            volumes_kept=tuple(volume.name for volume in volumes),
        )

    async def require_writable_members(
        self, actor: Principal, environment_id: int, stack: StoredStack
    ) -> None:
        """Raise StackRejected (`restricted`) unless a standard user may change every member."""
        if actor.is_administrator:
            return
        client = await self._hosts.client(environment_id)
        members = await read_namespace(
            client, environment_id, self._poke, canonical_stack_key(stack.name)
        )
        await self._require_writable(actor, environment_id, members)

    async def credential_pickers(
        self, actor: Principal, environment_id: int, references: Sequence[str]
    ) -> tuple[CredentialOffer, ...]:
        """Describe the credential picker each image reference shows; none without registries."""
        return await self._queries.credential_pickers(actor, environment_id, references)

    def compose_status(self) -> ComposeStatus:
        """Describe Compose from what is already known; it never starts a process."""
        return self._queries.compose_status()

    async def require_adoptable(self, environment_id: int, name: str, client: EngineClient) -> None:
        """Raise unless a Swarm namespace spelled exactly `name` is on the host, uncovered."""
        key = canonical_stack_key(name)
        if await self._stacks.get_folded(environment_id, name) is not None:
            raise StackNameInUse
        if key in await host_projects(client, namespaces=False):
            raise StackNameInUse
        spellings = (await read_namespace(client, environment_id, self._poke, key)).spellings
        if not spellings:
            raise StackRejected(
                {"name": "No Swarm stack has this name on this environment."}, "invalid"
            )
        if name not in spellings:
            raise StackRejected(
                {"name": "Use the name exactly as the stack's objects spell it."}, "invalid"
            )

    async def _require_writable(
        self, actor: Acting, environment_id: int, members: NamespaceMembers
    ) -> None:
        if is_administrator(actor):
            return
        refs = members.refs(environment_id)
        if isinstance(actor, AutomationAuthority):
            for ref in refs:
                try:
                    await _owner_access(actor, ref.key, "write")
                except (ResourceNotFound, PermissionDenied):
                    raise StackRejected({"stack": UNWRITABLE_MEMBERS}, "restricted") from None
            return
        writable = await self._authoriser.filter_visible(actor, refs, "write")
        if len(writable) != len(refs):
            raise StackRejected({"stack": UNWRITABLE_MEMBERS}, "restricted")

    async def _load(
        self,
        environment: Environment,
        client: EngineClient,
        stack: StoredStack | None,
        name: str,
        text: str,
        values: VariableSet,
        *,
        submitted: bool,
        candidate: Sequence[CompanionFile] | None = None,
    ) -> _Loaded:
        if values.writes is not None:
            secret_names = frozenset(write.name for write in values.writes if write.is_secret)
        elif stack is not None:
            secret_names = frozenset(
                variable.name
                for variable in await self._stacks.variables(stack.id)
                if variable.is_secret
            )
        else:
            secret_names = frozenset()
        stored = await self._stacks.variable_tokens(stack.id) if stack is not None else {}
        tokens = {
            variable: stored.get(variable, PLACEHOLDER_TOKEN if submitted else "")
            for variable in secret_names
        }
        files = await self._queries.stored_files(stack) if candidate is None else tuple(candidate)
        file_tokens = (
            {item.name: item.value_token for item in await self._stacks.files.files(stack.id)}
            if stack is not None and files and candidate is None
            else {}
        )
        return _Loaded(
            environment, client, stack, name, text, values, secret_names, tokens, files, file_tokens
        )

    async def _plan(
        self,
        actor: Acting,
        loaded: _Loaded,
        *,
        remove_orphans: bool,
        choices: Mapping[str, CredentialChoice] | None,
        resolve_images: bool,
        documents: bool,
        check_collisions: bool = False,
    ) -> SwarmDeployPlan:
        environment, client, stack = loaded.environment, loaded.client, loaded.stack
        project, directory, delivery = await self._config(loaded)
        _everyone(project, actor, delivery)
        scanned = scan(project, default_runtime=None, kind="swarm")
        checks = _Checks(findings=list(scanned.findings))
        translation = _translation(
            actor,
            loaded,
            project,
            directory,
            delivery,
            checks,
            client,
            self._queries.server_paths(),
        )
        _secret_in_output(translation, loaded.values.secrets | delivery.whole_files)
        key = canonical_stack_key(loaded.name)
        cluster = await self._cluster(
            environment.id,
            client,
            translation,
            key,
            stack,
            documents=documents or not is_administrator(actor),
        )
        if stack is not None:
            await self._require_writable(actor, environment.id, cluster.members)
        files, networks = await self._externals(actor, environment.id, translation, cluster, checks)
        if not is_administrator(actor):
            await self._standard_user(
                actor, environment.id, client, translation, cluster, files, networks, checks
            )
            _raise_restricted(checks)
        elif checks.errors:
            raise StackRejected(dict(checks.errors), "invalid")
        await self._preexisting(environment.id, client, translation, cluster, key)
        _network_differences(translation, cluster)
        _mode_changes(translation, cluster)
        warnings = (
            await self._collisions_of(actor, loaded, translation, delivery.secrets)
            if check_collisions
            else ()
        )
        pins = await self._pins(actor, environment.id, client, translation, choices, resolve_images)
        return self._assemble(
            loaded=loaded,
            project=hide_keys(project, delivery.secret_keys),
            translation=translation,
            cluster=cluster,
            pins=pins,
            files=files,
            networks=networks,
            checks=checks,
            remove_orphans=remove_orphans,
            actor=actor,
            warnings=warnings,
            secrets=delivery.secrets,
        )

    async def _collisions_of(
        self, actor: Acting, loaded: _Loaded, translation: Translation, secrets: frozenset[str]
    ) -> tuple[str, ...]:
        """Refuse a port another service holds; return the warnings."""
        if isinstance(actor, AutomationAuthority):
            raise ValueError("collision checks are for a user-initiated validation")
        report = await self._collisions.swarm(
            actor,
            loaded.environment.id,
            loaded.client,
            namespace=loaded.name,
            services=[(item.key, item.spec) for item in translation.services],
            scrubber=make_scrubber(secrets),
            poke=self._poke,
        )
        return raise_if_refused(report)

    async def _config(self, loaded: _Loaded) -> tuple[ResolvedProject, Path, _Delivery]:
        cli = await self._queries.ready()
        try:
            uses = await asyncio.to_thread(precheck, loaded.text, kind="swarm", files=loaded.files)
        except ComposeDocumentRejected as rejected:
            raise StackRejected(rejected.fields, rejected.reason, lines=rejected.lines) from None
        target = await build_target(loaded.environment, self._tls)
        sensitive = file_secret_values(loaded.files, uses)
        secrets = loaded.values.secrets | sensitive
        scrubber = make_scrubber(secrets)
        async with self._queries.scratch(
            canonical_stack_key(loaded.name), loaded.text, kind="swarm", files=loaded.files
        ) as project:
            delivery = _Delivery(
                {
                    name: Source(
                        item.section,
                        item.key,
                        item.content.encode("utf-8"),
                        item.secret,
                        loaded.file_tokens.get(item.file, PLACEHOLDER_TOKEN),
                        item.file,
                    )
                    for name, item in number_sources(project.sources)
                },
                tuple(f"{project.directory.as_posix()}/{item.name}" for item in loaded.files),
                secrets,
                secret_env_keys(loaded.files, uses),
                _whole_secret_files(loaded.files),
            )
            try:
                async with cli.invocation(project, target, loaded.values.values, scrubber) as run:
                    resolved = await run.config(slot_wait=CONFIG_SLOT_WAIT_SECONDS)
            except ComposeBusy:
                raise StackBusy from None
            except ComposeTimedOut as error:
                raise StackRejected(
                    {COMPOSE_FIELD: "Docker Compose did not finish checking this file in time."},
                    "invalid_compose",
                    stderr=error.stderr,
                ) from None
            except ComposeCommandFailed as error:
                raise StackRejected(
                    {COMPOSE_FIELD: "Docker Compose rejected this file."},
                    "invalid_compose",
                    stderr=error.stderr,
                ) from None
            except (ComposeOutputInvalid, ComposeOutputTooLarge):
                raise StackRejected(
                    {COMPOSE_FIELD: "Docker Compose returned a result dockmin cannot use."},
                    "invalid_compose",
                ) from None
            except ComposeError as error:
                raise compose_to_stack_error(error) from None
            return resolved, project.directory, delivery

    async def _cluster(
        self,
        environment_id: int,
        client: EngineClient,
        translation: Translation,
        key: str,
        stack: StoredStack | None,
        *,
        documents: bool,
    ) -> _Cluster:
        members = await read_namespace(client, environment_id, self._poke, key)
        found: dict[str, ServiceDocument] = {}
        if stack is not None and documents:
            own = {service.spec.name: service for service in members.services}
            for item in translation.services:
                service = own.get(item.name)
                if service is None:
                    continue
                document = await swarm_read(
                    client.swarm_services.document(service.id), environment_id, self._poke
                )
                if document.id != service.id:
                    raise ResourceNotFound("not found")
                found[item.name] = document
        return _Cluster(members, found)

    async def _externals(
        self,
        actor: Acting,
        environment_id: int,
        translation: Translation,
        cluster: _Cluster,
        checks: _Checks,
    ) -> tuple[dict[tuple[str, str], tuple[str, str]], dict[str, str]]:
        files: dict[tuple[str, str], tuple[str, str]] = {}
        networks: dict[str, str] = {}
        administrator = is_administrator(actor)
        for item in translation.networks:
            if not item.external:
                continue
            path = f"networks.{item.key}"
            try:
                network = await resolve_network(self._inspector, environment_id, item.name)
            except (ResourceNotFound, ObjectTooLarge):
                checks.errors.setdefault(path, NETWORK_UNAVAILABLE)
                continue
            if network.scope != "swarm" and network.driver != "host":
                checks.errors.setdefault(path, EXTERNAL_NETWORK_UNUSABLE)
                continue
            users = _network_users(translation, item.name)
            added = any(not _holds_network(cluster, user.name, network.id) for user in users)
            if (
                not administrator
                and added
                and not await self._may(
                    actor, ResourceKey(environment_id, "network", network.id), "write"
                )
            ):
                checks.errors.setdefault(path, NETWORK_UNAVAILABLE)
                continue
            if network.driver == "host":
                checks.findings.append(StackFinding("network_host", path))
            networks[item.name] = network.id
        for file in translation.files:
            if not file.external:
                continue
            path = f"{file.kind}s.{file.key}"
            unavailable = CONFIG_UNAVAILABLE if file.kind == "config" else SECRET_UNAVAILABLE
            try:
                object_id, object_name = await resolve_file(
                    self._inspector, environment_id, file.kind, file.name
                )
            except (ResourceNotFound, ObjectTooLarge):
                checks.errors.setdefault(path, unavailable)
                continue
            users = _file_users(translation, file)
            added = any(
                not _holds_file(cluster, user.name, file.kind, object_id, object_name)
                for user in users
            )
            level: Level = "read" if file.kind == "config" else "write"
            if (
                not administrator
                and added
                and not await self._may(
                    actor, ResourceKey(environment_id, file.kind, object_id), level
                )
            ):
                checks.errors.setdefault(path, unavailable)
                continue
            files[(file.kind, file.name)] = (object_id, object_name)
        return files, networks

    async def _may(self, actor: Acting, key: ResourceKey, level: Level) -> bool:
        try:
            if isinstance(actor, AutomationAuthority):
                await _owner_access(actor, key, level)
            else:
                await self._authoriser.require_access(actor, key, level)
        except (ResourceNotFound, PermissionDenied):
            return False
        return True

    async def _volume_reach(
        self,
        actor: Acting,
        environment_id: int,
        before: Mapping[str, Any] | None,
        spec: Mapping[str, Any],
        manager: str | None,
        slots: Mapping[str, Sequence[int]],
    ) -> tuple[list[Finding], dict[str, str]]:
        async def access(key: ResourceKey, level: Level) -> None:
            if isinstance(actor, AutomationAuthority):
                await _owner_access(actor, key, level)
            else:
                await self._authoriser.require_access(actor, key, level)

        return await volume_reach(
            access, self._inspector, environment_id, before, spec, manager, slots
        )

    async def _standard_user(
        self,
        actor: Acting,
        environment_id: int,
        client: EngineClient,
        translation: Translation,
        cluster: _Cluster,
        files: Mapping[tuple[str, str], tuple[str, str]],
        networks: Mapping[str, str],
        checks: _Checks,
    ) -> None:
        manager: str | None = None
        if translation.volumes:
            manager = await manager_node_id(client, environment_id, self._poke)
        placeholders = dict(files)
        for file in translation.files:
            placeholders.setdefault((file.kind, file.name), (PLACEHOLDER_ID, file.name))
        scanned_options = _options_by_service(checks.findings)
        for item in translation.services:
            spec = bind_references(
                item.spec, image=item.image, files=placeholders, networks=networks
            )
            for found in find_restricted_service(spec):
                if _covered(scanned_options, item.key, found.option):
                    continue
                checks.gaps += 1
                checks.findings.append(
                    StackFinding(found.option, compose_path(item, found.path), found.path)
                )
            document = cluster.documents.get(item.name)
            before = document.spec if document is not None else None
            reach, errors = await self._volume_reach(
                actor,
                environment_id,
                before,
                spec,
                manager,
                {},
            )
            for found in reach:
                checks.findings.append(
                    StackFinding(found.option, compose_path(item, found.path), found.path)
                )
            for field_name, message in errors.items():
                checks.errors.setdefault(_volume_error_path(item, field_name), message)
        if checks.gaps:
            logger.error("stack_scan_gap", extra={"count": checks.gaps})

    async def _preexisting(
        self,
        environment_id: int,
        client: EngineClient,
        translation: Translation,
        cluster: _Cluster,
        key: str,
    ) -> None:
        members = cluster.members
        own_services = {s.spec.name for s in members.services}
        own_networks = {n.name for n in members.networks}
        configs = {c.name: c for c in members.configs}
        secrets = {s.name for s in members.secrets}
        foreign: list[str] = []
        for service in translation.services:
            if service.name not in own_services and await self._service_exists(
                client, environment_id, service.name
            ):
                foreign.append(f"Service {service.name}")
        for network in translation.networks:
            if network.external or network.name in own_networks:
                continue
            if await self._network_exists(client, environment_id, network.name):
                foreign.append(f"Network {network.name}")
        for file in translation.files:
            if file.external:
                continue
            if file.kind == "config":
                held = configs.get(file.name)
                if held is None:
                    if await self._config_exists(client, environment_id, file.name):
                        foreign.append(f"Config {file.name}")
                elif not await self._config_matches(client, environment_id, held.id, file):
                    foreign.append(f"Config {file.name}")
            elif file.name not in secrets and await self._secret_exists(
                client, environment_id, file.name
            ):
                foreign.append(f"Secret {file.name}")
        for use in translation.volumes:
            if use.external or not use.source:
                continue
            if await self._volume_foreign(client, environment_id, use.source, key):
                foreign.append(f"Volume {use.source}")
        if foreign:
            raise PreexistingObjects(sorted(dict.fromkeys(foreign)))

    async def _service_exists(self, client: EngineClient, environment_id: int, name: str) -> bool:
        try:
            found = await swarm_read(
                client.swarm_services.inspect(name), environment_id, self._poke
            )
        except ResourceNotFound:
            return False
        except SwarmListTooLarge:
            return True
        return found.spec.name == name

    async def _config_exists(self, client: EngineClient, environment_id: int, name: str) -> bool:
        try:
            found = await swarm_read(client.swarm_configs.inspect(name), environment_id, self._poke)
        except ResourceNotFound:
            return False
        except SwarmListTooLarge:
            return True
        return found.config.name == name

    async def _config_matches(
        self, client: EngineClient, environment_id: int, config_id: str, file: FileItem
    ) -> bool:
        if file.data is None:
            return False
        try:
            document: ConfigDocument = await swarm_read(
                client.swarm_configs.inspect(config_id), environment_id, self._poke
            )
        except (ResourceNotFound, SwarmListTooLarge):
            return False
        return document.config.id == config_id and document.data == file.data.reveal()

    async def _secret_exists(self, client: EngineClient, environment_id: int, name: str) -> bool:
        try:
            found = await swarm_read(client.swarm_secrets.inspect(name), environment_id, self._poke)
        except ResourceNotFound:
            return False
        except SwarmListTooLarge:
            return True
        return found.name == name

    async def _network_exists(self, client: EngineClient, environment_id: int, name: str) -> bool:
        try:
            found = await client.networks.inspect(name)
        except EngineNotFound:
            return False
        except ResponseTooLarge:
            return True
        except EngineError as error:
            if error.status_code in (400, 409):
                return True
            raise unavailable_from(error) from None
        return found.name == name

    async def _volume_foreign(
        self, client: EngineClient, environment_id: int, name: str, key: str
    ) -> bool:
        try:
            volume: Volume = await client.volumes.inspect(name)
        except EngineNotFound:
            return False
        except ResponseTooLarge:
            return True
        except EngineError as error:
            raise unavailable_from(error) from None
        if volume.name != name:
            return False
        carried = volume.labels.get(SWARM_NAMESPACE_LABEL)
        if self._imported is not None and self._imported.holds(environment_id, key, volume.name):
            own = self._imported.accepts(environment_id, key, volume.name, volume.created_at)
        else:
            own = bool(carried) and canonical_stack_key(carried or "") == key
        if not own:
            return True
        return await self._control_repo.get_for(environment_id, "volume", volume.name) is not None

    async def _pins(
        self,
        actor: Acting,
        environment_id: int,
        client: EngineClient,
        translation: Translation,
        choices: Mapping[str, CredentialChoice] | None,
        resolve_images: bool,
    ) -> dict[str, PinnedImage | None]:
        if not resolve_images:
            return {}
        pins: dict[str, PinnedImage | None] = {}
        problems: dict[str, str] = {}
        refused: dict[str, CredentialRefusal] = {}
        reason = "credential_choice_required"
        for reference, keys in translation.images.items():
            if not _is_image_id(reference):
                try:
                    parse_pull_reference(reference)
                except ValidationFailed:
                    problems[f"services.{keys[0]}.image"] = "This image reference is not valid."
                    reason = stronger_reason(reason, "invalid")
                    continue
            try:
                pins[reference] = await pin_image(
                    self._credentials,
                    client,
                    actor,
                    environment_id,
                    reference,
                    (choices or {}).get(reference),
                )
            except CredentialChoiceRequired as error:
                problems[reference] = str(error)
            except CredentialSetMismatch as error:
                problems[reference] = str(error)
                refused[reference] = "credential_mismatch"
            except CredentialSetUnavailable as error:
                problems[reference] = str(error)
                refused[reference] = "credential_unavailable"
                reason = stronger_reason(reason, "credential_unavailable")
            except RebindRefused as error:
                problems[reference] = str(error)
                reason = stronger_reason(reason, "restricted")
            except CandidateListTooLarge as error:
                problems[reference] = str(error)
                reason = stronger_reason(reason, "invalid")
        if refused:
            raise StackCredentialsRefused(problems, reason, refused)
        if problems:
            raise StackRejected(problems, reason)
        return pins

    def _assemble(
        self,
        *,
        loaded: _Loaded,
        project: ResolvedProject,
        translation: Translation,
        cluster: _Cluster,
        pins: Mapping[str, PinnedImage | None],
        files: Mapping[tuple[str, str], tuple[str, str]],
        networks: Mapping[str, str],
        checks: _Checks,
        remove_orphans: bool,
        actor: Acting,
        warnings: tuple[str, ...],
        secrets: frozenset[str],
    ) -> SwarmDeployPlan:
        members = cluster.members
        stack = loaded.stack
        name = loaded.name
        prefix = f"{canonical_stack_key(name)}_"
        own_services = {s.spec.name: s for s in members.services}
        own_networks = {n.name: n for n in members.networks}
        own_configs = {c.name: c for c in members.configs}
        own_secrets = {s.name: s for s in members.secrets}
        lines: list[PlanLine] = []
        for network in translation.networks:
            if network.external:
                continue
            held = own_networks.get(network.name)
            lines.append(
                PlanLine("keep" if held else "create", "network", network.name, _id_of(held))
            )
        for file in translation.files:
            if file.external or file.kind != "config":
                continue
            held_config = own_configs.get(file.name)
            lines.append(
                PlanLine(
                    "keep" if held_config else "create",
                    "config",
                    file.name,
                    held_config.id if held_config else None,
                )
            )
        for file in translation.files:
            if file.external or file.kind != "secret":
                continue
            held_secret = own_secrets.get(file.name)
            lines.append(
                PlanLine(
                    "keep" if held_secret else "create",
                    "secret",
                    file.name,
                    held_secret.id if held_secret else None,
                )
            )
        planned: list[PlannedService] = []
        moves: list[ImageMove] = []
        for item in translation.services:
            pinned = pins.get(item.image)
            image = pinned.image if pinned is not None else item.image
            held_service = own_services.get(item.name)
            document = cluster.documents.get(item.name)
            current = _current_image(document, held_service)
            entry = PlannedService(
                item=item,
                action="update" if held_service else "create",
                image=image,
                reference=item.image,
                service_id=held_service.id if held_service else None,
                version=document.version if document else None,
                force_update=_force_update(document),
                current_image=current,
                current_spec=document.spec if document else None,
            )
            planned.append(entry)
            detail = ""
            if entry.image_changed:
                detail = "image changes"
                moves.append(
                    ImageMove(
                        item.name,
                        item.image,
                        current,
                        image,
                        pinned is not None and pinned.pinned,
                    )
                )
            lines.append(PlanLine(entry.action, "service", item.name, entry.service_id, detail))
        planned_services = {item.name for item in translation.services}
        staying: list[Any] = []
        for service in sorted(members.services, key=lambda s: s.spec.name):
            if service.spec.name in planned_services:
                continue
            if remove_orphans:
                lines.append(PlanLine("remove", "service", service.spec.name, service.id))
            else:
                staying.append(service)
                lines.append(
                    PlanLine("keep", "service", service.spec.name, service.id, "not in the file")
                )
        used = _referenced_by(staying)
        used_networks = _networks_of(staying)
        declared_files: set[tuple[str, str]] = {
            (file.kind, file.name) for file in translation.files
        }
        replaced: list[tuple[Literal["config", "secret"], SwarmConfig | SwarmSecret]] = [
            *(("config", held) for held in members.configs),
            *(("secret", held) for held in members.secrets),
        ]
        for kind, old in sorted(replaced, key=lambda pair: (pair[0], pair[1].name)):
            if (kind, old.name) in declared_files or not old.name.casefold().startswith(prefix):
                continue
            if old.id in used:
                lines.append(
                    PlanLine("keep", kind, old.name, old.id, "used by a service that stays")
                )
            else:
                lines.append(PlanLine("remove", kind, old.name, old.id, "replaced"))
        declared_networks = {n.name for n in translation.networks}
        for network_object in sorted(members.networks, key=lambda n: n.name):
            if network_object.name in declared_networks:
                continue
            if network_object.id in used_networks or network_object.name in used_networks:
                lines.append(
                    PlanLine(
                        "keep",
                        "network",
                        network_object.name,
                        network_object.id,
                        "used by a service that stays",
                    )
                )
            else:
                lines.append(PlanLine("remove", "network", network_object.name, network_object.id))
        unpinned = tuple(
            reference for reference, pin in pins.items() if pin is not None and not pin.pinned
        )
        restricted = refused_stack_options(checks.findings) if is_administrator(actor) else ()
        resolved = resolved_digest(project, secrets)
        bound = [
            *(f"image service {entry.item.name} {entry.image}" for entry in planned),
            f"resolved {resolved}",
        ]
        return SwarmDeployPlan(
            stack_name=name,
            translation=translation,
            lines=tuple(lines),
            digest=plan_digest(lines, bound),
            pinned=dict(pins),
            restricted=restricted,
            compose_digest=file_digest(loaded.text),
            resolved_digest=resolved,
            remove_orphans=remove_orphans,
            services=tuple(planned),
            files={**_own_files(members), **files},
            external_networks=dict(networks),
            unpinned=unpinned,
            image_moves=tuple(moves),
            adopted_first_redeploy=(
                stack is not None and stack.origin == "adopted" and stack.last_deployed_at is None
            ),
            members=members.count,
            warnings=warnings,
        )


def _is_image_id(reference: str) -> bool:
    return reference.startswith("sha256:") and ":" not in reference[7:]


def _checked_name(name: str) -> str:
    try:
        return swarm_stack_name(name)
    except ComposeDocumentRejected as rejected:
        raise StackRejected(rejected.fields, rejected.reason) from None


def _id_of(item: Network | None) -> str | None:
    return item.id if item is not None else None


def _everyone(project: ResolvedProject, actor: Acting, delivery: _Delivery) -> None:
    refused = swarm_refusals(
        project,
        companions=delivery.companions,
        generated={(item.section, item.key): name for name, item in delivery.sources.items()},
    )
    if refused:
        raise StackRejected(refused, "refused_construct")
    problems = limits(project, kind="swarm", administrator=is_administrator(actor))
    if problems:
        raise StackRejected(problems, "invalid")
    labels = reserved_labels(project, kind="swarm")
    if labels:
        raise _restricted_error(labels, {})


def _translation(
    actor: Acting,
    loaded: _Loaded,
    project: ResolvedProject,
    directory: Path,
    delivery: _Delivery,
    checks: _Checks,
    client: EngineClient,
    protected: tuple[str, ...],
) -> Translation:
    try:
        return translate(
            TranslationInput(
                namespace=loaded.name,
                project=project,
                variables=loaded.values.values,
                secret_variables=loaded.secret_names,
                variable_tokens=loaded.tokens,
                project_directory=directory,
                api_version=_api_version(client),
                protected_paths=protected,
                sources=delivery.sources,
                companions=delivery.companions,
            )
        )
    except TranslationRejected as rejected:
        if not is_administrator(actor) and checks.findings:
            raise _restricted_error(checks.findings, rejected.fields) from None
        raise StackRejected(rejected.fields, rejected.reason) from None


def _api_version(client: EngineClient) -> tuple[int, int]:
    daemon = client.daemon
    if daemon is None:
        return 1, 0
    major, _, minor = daemon.negotiated_api_version.partition(".")
    if major.isdecimal() and minor.isdecimal():
        return int(major), int(minor)
    return 1, 0


def _whole_secret_files(files: Sequence[CompanionFile]) -> frozenset[str]:
    return frozenset(
        text
        for item in files
        if item.secret
        for text in (item.content, item.content.strip())
        if text
    )


def _secret_in_output(translation: Translation, secrets: frozenset[str]) -> None:
    wanted = [value for value in secrets if value]
    if not wanted:
        return
    found: dict[str, str] = {}
    encoded = [value.encode("utf-8") for value in wanted]
    for file in translation.files:
        if file.kind != "config" or file.external or file.data is None:
            continue
        content = file.data.reveal()
        if any(value in content for value in encoded):
            found[f"configs.{file.key}"] = SECRET_IN_CONFIG
    for item in translation.services:
        for path, text in _readable_strings(item.spec, ""):
            if any(value in text for value in wanted):
                found.setdefault(compose_path(item, path), SECRET_IN_SERVICE)
    if found:
        raise StackRejected(found, "secret_in_config")


def _readable_strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Yield every string a reader of the service would see, with its ServiceSpec path.

    Environment values are left out: they carry secrets by design and are redacted when shown.
    """
    if path == ENVIRONMENT_PATH:
        return
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, entry in value.items():
            if isinstance(key, str):
                yield f"{path}.{key}" if path else key, key
                yield from _readable_strings(entry, f"{path}.{key}" if path else key)
    elif isinstance(value, list | tuple):
        for ordinal, entry in enumerate(value):
            yield from _readable_strings(entry, f"{path}[{ordinal}]")


def _network_users(translation: Translation, name: str) -> list[ServiceItem]:
    return [
        item
        for item in translation.services
        if any(
            attachment.get("Target") == name
            for attachment in item.spec["TaskTemplate"].get("Networks", ())
        )
    ]


def _file_users(translation: Translation, file: FileItem) -> list[ServiceItem]:
    group, name_key = (
        ("Configs", "ConfigName") if file.kind == "config" else ("Secrets", "SecretName")
    )
    return [
        item
        for item in translation.services
        if any(
            reference.get(name_key) == file.name
            for reference in item.spec["TaskTemplate"]["ContainerSpec"].get(group, ())
        )
    ]


def _holds_network(cluster: _Cluster, service: str, network_id: str) -> bool:
    document = cluster.documents.get(service)
    return document is not None and any(
        target == network_id for target, _ in references(document.spec).networks
    )


def _holds_file(
    cluster: _Cluster, service: str, kind: str, object_id: str, object_name: str
) -> bool:
    document = cluster.documents.get(service)
    if document is None:
        return False
    held = references(document.spec)
    pairs = held.configs if kind == "config" else held.secrets
    return any((i, n) == (object_id, object_name) for i, n, _ in pairs)


def _options_by_service(findings: Sequence[StackFinding]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for finding in findings:
        parts = finding.path.split(".")
        if len(parts) >= 2 and parts[0] == "services":
            found.setdefault(parts[1], set()).add(finding.option)
    return found


def _covered(options: Mapping[str, set[str]], service: str, option: str) -> bool:
    held = options.get(service, set())
    return option in held or _EQUIVALENT_OPTIONS.get(option) in held


def _volume_error_path(item: ServiceItem, field_name: str) -> str:
    ordinal = field_name.removeprefix("mount-").removesuffix("-source")
    if ordinal.isdecimal():
        return compose_path(item, f"TaskTemplate.ContainerSpec.Mounts[{ordinal}].Source")
    return f"services.{item.key}"


def _network_differences(translation: Translation, cluster: _Cluster) -> None:
    own = {network.name: network for network in cluster.members.networks}
    differs: dict[str, str] = {}
    for item in translation.networks:
        held = own.get(item.name)
        if not item.external and held is not None and _network_differs(held, item.body):
            differs[f"networks.{item.key}"] = NETWORK_DIFFERS
    if differs:
        raise StackRejected(differs, "network_differs")


def _network_differs(held: Network, body: Mapping[str, Any]) -> bool:
    """Compare what the Engine keeps of a network with what the file states.

    The Engine adds the vxlan id option, a gateway and, when no subnet is given, an allocated
    subnet, so an unstated subnet, gateway or range matches anything.
    """
    if (
        held.driver != body.get("Driver", "overlay")
        or held.internal != bool(body.get("Internal", False))
        or held.attachable != bool(body.get("Attachable", False))
        or held.enable_ipv6 != bool(body.get("EnableIPv6", False))
    ):
        return True
    options = {k: v for k, v in held.options.items() if k != VXLAN_OPTION}
    if options != dict(body.get("Options", {})):
        return True
    stated_driver = body.get("IPAM", {}).get("Driver")
    if stated_driver is not None and held.ipam_driver != stated_driver:
        return True
    stated = body.get("IPAM", {}).get("Config", ())
    pools = {_subnet(pool.subnet): pool for pool in held.ipam}
    for entry in stated:
        pool = pools.get(_subnet(entry["Subnet"]))
        if pool is None:
            return True
        if "Gateway" in entry and pool.gateway != entry["Gateway"]:
            return True
        if "IPRange" in entry and _subnet(pool.ip_range) != _subnet(entry["IPRange"]):
            return True
    return False


def _subnet(text: str) -> str:
    try:
        return str(ipaddress.ip_network(text, strict=False))
    except ValueError:
        return text


def _mode_changes(translation: Translation, cluster: _Cluster) -> None:
    own = {service.spec.name: service for service in cluster.members.services}
    changed: dict[str, str] = {}
    for item in translation.services:
        held = own.get(item.name)
        if held is not None and mode_of({"Mode": held.spec.mode}) != mode_of(item.spec):
            changed[f"services.{item.key}.deploy.mode"] = MODE_CHANGE
    if changed:
        raise StackRejected(changed, "invalid")


def _current_image(document: ServiceDocument | None, held: Any) -> str | None:
    if document is not None:
        container = document.spec.get("TaskTemplate", {}).get("ContainerSpec", {})
        image = container.get("Image") if isinstance(container, Mapping) else None
        return image if isinstance(image, str) else None
    if held is not None:
        image = held.spec.task_template.get("ContainerSpec", {}).get("Image")
        return image if isinstance(image, str) else None
    return None


def _force_update(document: ServiceDocument | None) -> int:
    if document is None:
        return 0
    task = document.spec.get("TaskTemplate", {})
    counter = task.get("ForceUpdate") if isinstance(task, Mapping) else None
    return counter if isinstance(counter, int) and not isinstance(counter, bool) else 0


def _referenced_by(services: Sequence[Any]) -> set[str]:
    ids: set[str] = set()
    for service in services:
        container = service.spec.task_template.get("ContainerSpec", {})
        if not isinstance(container, Mapping):
            continue
        for group, id_key in (("Configs", "ConfigID"), ("Secrets", "SecretID")):
            for reference in container.get(group) or ():
                if isinstance(reference, Mapping) and isinstance(reference.get(id_key), str):
                    ids.add(reference[id_key])
    return ids


def _networks_of(services: Sequence[Any]) -> set[str]:
    targets: set[str] = set()
    for service in services:
        attachments = service.spec.task_template.get("Networks")
        if not isinstance(attachments, list):
            continue
        targets.update(
            entry["Target"]
            for entry in attachments
            if isinstance(entry, Mapping) and isinstance(entry.get("Target"), str)
        )
    return targets


def _own_files(members: NamespaceMembers) -> dict[tuple[str, str], tuple[str, str]]:
    return {
        **{("config", c.name): (c.id, c.name) for c in members.configs},
        **{("secret", s.name): (s.id, s.name) for s in members.secrets},
    }


def _member_lines(members: NamespaceMembers) -> tuple[PlanLine, ...]:
    return (
        *(PlanLine("remove", "service", s.spec.name, s.id) for s in members.services),
        *(PlanLine("remove", "network", n.name, n.id) for n in members.networks),
        *(PlanLine("remove", "config", c.name, c.id) for c in members.configs),
        *(PlanLine("remove", "secret", s.name, s.id) for s in members.secrets),
    )


def _restricted_error(findings: Sequence[StackFinding], errors: Mapping[str, str]) -> StackRejected:
    views = [FindingView(f.path, f.option, f.label, f.message) for f in findings]
    fields: dict[str, str] = {}
    for found in findings:
        message = found.message
        if found.engine_path:
            message = f"{message} ({found.engine_path})"
        fields[found.path] = message
    fields.update(errors)
    return StackRejected(
        fields, "restricted", findings=views, restricted=refused_stack_options(findings)
    )


def _raise_restricted(checks: _Checks) -> None:
    if checks.findings or checks.errors:
        raise _restricted_error(checks.findings, checks.errors)


def _report_of(plan: SwarmDeployPlan) -> ValidationReport:
    translation = plan.translation
    return ValidationReport(
        name=plan.stack_name,
        services=tuple(_summary_of(item) for item in translation.services),
        networks=tuple(sorted(n.name for n in translation.networks if not n.external)),
        volumes=tuple(sorted({use.source for use in translation.volumes if not use.external})),
        images=tuple(translation.images),
        to_pull=(),
        restricted=plan.restricted,
        compose_digest=plan.compose_digest,
        resolved_digest=plan.resolved_digest,
        restricted_options=tuple(
            RestrictedOption(option, stack_option_label(option)) for option in plan.restricted
        ),
        warnings=plan.warnings,
    )


def _summary_of(item: ServiceItem) -> ServiceSummary:
    spec = item.spec
    ports = tuple(
        f"{head}{port.get('TargetPort')}/{port.get('Protocol', 'tcp')}"
        for port in spec.get("EndpointSpec", {}).get("Ports", ())
        for head in [f"{port['PublishedPort']}:" if port.get("PublishedPort") else ""]
    )
    container = spec["TaskTemplate"]["ContainerSpec"]
    return ServiceSummary(
        name=item.key,
        image=item.image,
        ports=ports[:8],
        networks=tuple(a["Target"] for a in spec["TaskTemplate"].get("Networks", ())),
        volumes=len(container.get("Mounts", ())),
    )
