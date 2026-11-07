"""Shared helpers and the base class of the Swarm service query and action services."""

import copy
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from app.auth.errors import (
    PermissionDenied,
    ResourceNotFound,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.docker_engine.client import EngineClient
from app.docker_engine.models import Network, ServiceStatus, SwarmService
from app.logging import DEFAULT_SCRUBBER
from app.registry_client.references import ReferenceInvalid, parse_reference
from app.services.access import ResourceKey, ResourceRef, is_canonical_id, stack_of
from app.services.acting import (
    Acting,
    actor_of,
    is_administrator,
)
from app.services.activity import (
    Actor,
    Arguments,
    current_audit,
)
from app.services.authorisation import Authoriser
from app.services.container_restrictions import Finding, reach_finding
from app.services.container_spec import format_memory
from app.services.containers import (
    parse_engine_time,
    stack_name,
)
from app.services.image_bindings import RebindRefused
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
)
from app.services.object_locks import ObjectKey
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CredentialChoice,
    CredentialChoiceRequired,
    PullCredentialResolver,
    PullCredentials,
)
from app.services.registry_resolution import CredentialSetUnavailable
from app.services.service_restrictions import (
    ServiceReferences,
    added_references,
    find_restricted_service,
    references,
)
from app.services.service_spec import (
    ResolvedRefs,
    ServiceInput,
    build_spec,
    format_exact_duration,
    image_of,
    merge_spec,
    mode_of,
    replicas_of,
)
from app.services.swarm import (
    swarm_read,
    task_message,
)
from app.services.swarm_common import (
    CONFIG_UNAVAILABLE,
    NETWORK_UNAVAILABLE,
    SECRET_UNAVAILABLE,
    PinnedImage,
    ServiceImage,
    entries_of,
    manager_node_id,
    mapping_of,
    pin_image,
    resolve_file,
    resolve_network,
    volume_reach,
)
from app.services.swarm_service_views import (
    MAX_VERIFY_SECONDS,
    EnvView,
    MountView,
    Plan,
    PortView,
    Resolved,
    RestrictedService,
    ServiceFilters,
    ServiceInvalid,
    ServiceRow,
    UpdateStatusView,
)

__all__ = [
    "MAX_LIST_ROWS",
    "MAX_NAME",
    "UPDATE_STATES",
    "CreateRun",
    "Entry",
    "SwarmServiceBase",
    "as_int",
    "as_list",
    "binding_actor",
    "clip",
    "display_image",
    "entry_matches",
    "entry_order",
    "env_view",
    "guarded",
    "migrate_legacy_networks",
    "mode_text",
    "mount_view",
    "ports_of",
    "reference_key",
    "require_binding",
    "resources_view",
    "restart_view",
    "rollout_bound",
    "rollout_view",
    "row_of",
    "service_argument",
    "service_ref",
    "service_status",
    "spec_view_model",
    "state_name",
    "status_view",
]

MAX_NAME: Final = 255


MAX_LIST_ROWS: Final = 64


NETWORK_DUPLICATE: Final = "This network is listed more than once."


UPDATE_STATES: Final = frozenset(
    {
        "updating",
        "paused",
        "completed",
        "rollback_started",
        "rollback_paused",
        "rollback_completed",
    }
)


DEFAULT_MONITOR_SECONDS: Final = 5.0


BATCH_ALLOWANCE_SECONDS: Final = 30.0


NANOSECONDS: Final = 1_000_000_000


@dataclass(slots=True)
class CreateRun:
    service_id: str | None = None


def migrate_legacy_networks(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of the spec with the deprecated top-level Networks moved into the task.

    The entries become TaskTemplate.Networks when that is empty, and the top-level key is
    dropped either way.
    """
    plain: dict[str, Any] = copy.deepcopy(dict(spec))
    legacy = plain.pop("Networks", None)
    if not isinstance(legacy, list) or not legacy:
        return plain
    task = plain.setdefault("TaskTemplate", {})
    if isinstance(task, dict) and not task.get("Networks"):
        task["Networks"] = legacy
    return plain


def display_image(image: str) -> str:
    """Shorten a digest in an image reference to its first twelve hexadecimal digits."""
    name, at, digest = image.partition("@")
    if at and digest.startswith("sha256:"):
        return f"{name}@sha256:{digest[7:19]}"
    return image


def as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def clip(value: object, limit: int = MAX_NAME) -> str:
    return value[:limit] if isinstance(value, str) else ""


def service_argument(arguments: Arguments) -> str:
    return str(arguments["service_id"])[:25]


def mode_text(spec: Mapping[str, Any]) -> str:
    return mode_of(spec) or "unknown"


def ports_of(service: SwarmService) -> tuple[PortView, ...]:
    if service.endpoint.ports:
        return tuple(
            PortView(p.target_port, p.published_port, p.protocol, p.publish_mode)
            for p in service.endpoint.ports[:MAX_LIST_ROWS]
        )
    return tuple(
        PortView(
            as_int(p.get("TargetPort")) or 0,
            as_int(p.get("PublishedPort")) or None,
            str(p.get("Protocol") or "tcp"),
            str(p.get("PublishMode") or "ingress"),
        )
        for p in entries_of(mapping_of(service.spec.endpoint_spec).get("Ports"))[:MAX_LIST_ROWS]
    )


def row_of(environment_id: int, service: SwarmService, *, can_write: bool) -> ServiceRow:
    spec_mapping = spec_view_model(service)
    image = image_of(spec_mapping) or ""
    status = service.service_status
    update = service.update_status
    return ServiceRow(
        environment_id=environment_id,
        id=service.id,
        short_id=service.id[:12],
        name=service.spec.name[:MAX_NAME],
        mode=mode_text(spec_mapping),
        replicas=replicas_of(spec_mapping),
        running=None if status is None else status.running_tasks,
        desired=None if status is None else status.desired_tasks,
        image=image[:512],
        image_display=display_image(image)[:512],
        stack=stack_name(service.spec.labels),
        ports=ports_of(service),
        updated_at=parse_engine_time(service.updated_at),
        update_state=None if update is None or not update.state else update.state,
        can_write=can_write,
    )


def spec_view_model(service: SwarmService) -> Mapping[str, Any]:
    return {"Mode": service.spec.mode, "TaskTemplate": service.spec.task_template}


def service_ref(environment_id: int, service: SwarmService) -> ResourceRef:
    return ResourceRef(environment_id, "service", service.id, service.spec.labels)


@dataclass(frozen=True, slots=True)
class Entry:
    service: SwarmService
    ref: ResourceRef
    sort_name: str
    row_id: int


def entry_order(entry: Entry) -> tuple[str, int]:
    return entry.sort_name, entry.row_id


def entry_matches(entry: Entry, filters: ServiceFilters) -> bool:
    service = entry.service
    if filters.q and filters.q.casefold() not in service.spec.name.casefold():
        return False
    if filters.mode and mode_text(spec_view_model(service)) != filters.mode:
        return False
    if filters.stack:
        stack = stack_of(service.spec.labels)
        wanted = filters.stack.strip().casefold()
        if stack is None or stack != wanted:
            return False
    return True


def _pair(label: str, value: object) -> tuple[str, str] | None:
    if value is None or value == "" or value is False:
        return None
    return label, str(value)


def _pairs(*items: tuple[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple(pair for label, value in items if (pair := _pair(label, value)) is not None)


def _duration(value: Any) -> str | None:
    number = as_int(value)
    if number is None or number < 0:
        return None
    return format_exact_duration(number)


def resources_view(task: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    resources = mapping_of(task.get("Resources"))
    limits, reservations = (
        mapping_of(resources.get("Limits")),
        mapping_of(resources.get("Reservations")),
    )

    def cpus(value: Any) -> str | None:
        number = as_int(value)
        return format(Decimal(number).scaleb(-9).normalize(), "f") if number else None

    def memory(value: Any) -> str | None:
        number = as_int(value)
        return format_memory(number) if number else None

    return _pairs(
        ("CPU limit", cpus(limits.get("NanoCPUs"))),
        ("Memory limit", memory(limits.get("MemoryBytes"))),
        ("PIDs limit", as_int(limits.get("Pids")) or None),
        ("CPU reservation", cpus(reservations.get("NanoCPUs"))),
        ("Memory reservation", memory(reservations.get("MemoryBytes"))),
    )


def rollout_view(node: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    ratio = node.get("MaxFailureRatio")
    return _pairs(
        ("Parallelism", as_int(node.get("Parallelism"))),
        ("Delay", _duration(node.get("Delay"))),
        ("Failure action", node.get("FailureAction")),
        ("Monitor", _duration(node.get("Monitor"))),
        (
            "Maximum failure ratio",
            ratio if isinstance(ratio, int | float) and not isinstance(ratio, bool) else None,
        ),
        ("Order", node.get("Order")),
    )


def restart_view(task: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    restart = mapping_of(task.get("RestartPolicy"))
    return _pairs(
        ("Condition", restart.get("Condition")),
        ("Delay", _duration(restart.get("Delay"))),
        ("Maximum attempts", as_int(restart.get("MaxAttempts"))),
        ("Window", _duration(restart.get("Window"))),
    )


def env_view(container: Mapping[str, Any], administrator: bool) -> tuple[EnvView, ...]:
    views: list[EnvView] = []
    for entry in as_list(container.get("Env"))[: MAX_LIST_ROWS * 2]:
        if not isinstance(entry, str):
            continue
        key, _, value = entry.partition("=")
        if not administrator:
            views.append(EnvView(clip(key), None, False))
        elif DEFAULT_SCRUBBER.is_sensitive(key):
            views.append(EnvView(clip(key), None, True))
        else:
            views.append(EnvView(clip(key), clip(DEFAULT_SCRUBBER.scrub_text(value), 4096), False))
    return tuple(views)


def mount_view(container: Mapping[str, Any], administrator: bool) -> tuple[MountView, ...]:
    views: list[MountView] = []
    for mount in entries_of(container.get("Mounts"))[:MAX_LIST_ROWS]:
        kind = str(mount.get("Type") or "volume")
        source = mount.get("Source")
        shown = (
            clip(source) if isinstance(source, str) and (kind != "bind" or administrator) else None
        )
        views.append(
            MountView(
                clip(kind, 32),
                shown or None,
                clip(mount.get("Target")),
                mount.get("ReadOnly") is True,
            )
        )
    return tuple(views)


def status_view(service: SwarmService) -> UpdateStatusView | None:
    update = service.update_status
    if update is None or not update.state:
        return None
    return UpdateStatusView(
        update.state[:64],
        task_message(update.message),
        parse_engine_time(update.started_at),
        parse_engine_time(update.completed_at),
    )


@contextmanager
def guarded() -> Iterator[None]:
    """Name the failure on the open audit scope for the errors planning can raise."""
    audit = current_audit()
    try:
        yield
    except RestrictedService as error:
        audit.reason = error.reason
        audit.detail.update(error.detail)
        raise
    except CredentialChoiceRequired:
        audit.reason = "credential_choice_required"
        raise
    except BindingCredentialMissing:
        audit.reason = "credential_missing"
        raise
    except CredentialSetUnavailable:
        audit.reason = "credential_unavailable"
        raise
    except ValidationFailed as error:
        reason = error.reason if isinstance(error, ServiceInvalid) else "invalid"
        audit.reason = audit.reason or reason
        audit.detail.setdefault("fields", ",".join(sorted(error.fields))[:200])
        raise
    except EnvironmentUnavailable as error:
        audit.reason = error.kind
        raise
    except ObjectTooLarge:
        audit.reason = "too_large"
        raise


async def service_status(
    client: EngineClient, environment_id: int, service_id: str, poke: Callable[[int], None]
) -> ServiceStatus:
    """Return the service's running and desired task counts.

    A service inspect carries no task counts; only the list with status does, and its id filter
    matches by prefix, so the entry's own id is checked.
    """
    listed = await swarm_read(
        client.swarm_services.list(filters={"id": [service_id]}), environment_id, poke
    )
    for entry in listed:
        if entry.id == service_id and entry.service_status is not None:
            return entry.service_status
    tasks = await swarm_read(
        client.swarm_tasks.list(filters={"service": [service_id]}), environment_id, poke
    )
    desired = [t for t in tasks if t.desired_state == "running"]
    running = [t for t in desired if t.status.state == "running"]
    return ServiceStatus(running_tasks=len(running), desired_tasks=len(desired))


def reference_key(image: ServiceImage) -> str | None:
    """Return the binding key of a spec image's reference, None when it names no reference."""
    source = image.text.strip() if image.kind == "digest_only" else image.reference
    if source is None:
        return None
    try:
        return parse_reference(source).key
    except ReferenceInvalid:
        return None


def state_name(raw: str | None) -> str | None:
    if raw is None or not raw:
        return None
    return raw if raw in UPDATE_STATES else "other"


def _nanoseconds(value: Any, default: float) -> float:
    number = as_int(value)
    return default if number is None or number < 0 else number / NANOSECONDS


def rollout_bound(spec: Mapping[str, Any], running_before: int) -> float:
    """Return the longest, in seconds, a rolling update of this spec is waited for."""
    update = mapping_of(spec.get("UpdateConfig"))
    parallelism = max(as_int(update.get("Parallelism")) or 1, 1)
    monitor = _nanoseconds(update.get("Monitor"), DEFAULT_MONITOR_SECONDS)
    delay = _nanoseconds(update.get("Delay"), 0.0)
    replicas = replicas_of(spec)
    batches = max(math.ceil((running_before if replicas is None else replicas) / parallelism), 1)
    return min(float(MAX_VERIFY_SECONDS), batches * (monitor + delay + BATCH_ALLOWANCE_SECONDS))


def binding_actor(actor: Acting) -> Principal | Actor:
    return actor if isinstance(actor, Principal) else actor_of(actor)


def require_binding(credentials: PullCredentials) -> None:
    if credentials.source != "binding":
        current_audit().reason = "invalid_credentials"
        raise ValueError("an automated image update sends the credentials its binding names")


class SwarmServiceBase:
    """Reads and checks shared by the queries and the actions: reach, credentials and assembly."""

    _authoriser: Authoriser
    _inspector: EngineObjectInspector
    _pull: PullCredentialResolver
    _poke: Callable[[int], None]

    async def _manager_node_id(self, client: EngineClient, environment_id: int) -> str | None:
        return await manager_node_id(client, environment_id, self._poke)

    async def _network(self, environment_id: int, key: str) -> Network:
        return await resolve_network(self._inspector, environment_id, key)

    async def _file_object(
        self,
        environment_id: int,
        kind: Literal["config", "secret"],
        key: str,
    ) -> tuple[str, str]:
        return await resolve_file(self._inspector, environment_id, kind, key)

    async def _resolve(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        service: ServiceInput,
        held: ServiceReferences | None,
    ) -> Resolved:
        """Resolve every network, config and secret the form names, and check the caller's reach.

        Errors are keyed by form field. A reference the current spec already holds skips the
        access check, because write access to the service reaches it; it does not skip the
        host-network check.
        """
        found = Resolved()
        administrator = actor.is_administrator
        held_networks = {target for target, _ in held.networks} if held else set()
        held_configs = {(i, n) for i, n, _ in held.configs} if held else set()
        held_secrets = {(i, n) for i, n, _ in held.secrets} if held else set()
        chosen: set[str] = set()
        for ordinal, row in enumerate(service.networks):
            field_name = f"network-{row.slot}-name"
            try:
                network = await self._network(environment_id, row.name)
            except ResourceNotFound:
                found.errors[field_name] = NETWORK_UNAVAILABLE
                continue
            if not administrator and network.id not in held_networks:
                try:
                    await self._authoriser.require_access(
                        actor, ResourceKey(environment_id, "network", network.id), "write"
                    )
                except (ResourceNotFound, PermissionDenied):
                    found.errors[field_name] = NETWORK_UNAVAILABLE
                    continue
            if network.driver == "host":
                found.findings.append(
                    reach_finding(
                        "network_host", f"TaskTemplate.Networks[{ordinal}].Target", field_name
                    )
                )
            if network.id in chosen:
                found.errors.setdefault(field_name, NETWORK_DUPLICATE)
                continue
            chosen.add(network.id)
            found.networks[row.slot] = network.id
        for slot, name in ((row.slot, row.name) for row in service.configs):
            await self._resolve_file(
                actor, environment_id, "config", slot, name, held_configs, found
            )
        for slot, name in ((row.slot, row.name) for row in service.secrets):
            await self._resolve_file(
                actor, environment_id, "secret", slot, name, held_secrets, found
            )
        return found

    async def _resolve_file(
        self,
        actor: Principal,
        environment_id: int,
        kind: Literal["config", "secret"],
        slot: int,
        name: str,
        held: set[tuple[str, str]],
        found: Resolved,
    ) -> None:
        """Resolve one config or secret row; a config needs read access, a secret write."""
        field_name = f"{kind}-{slot}-name"
        try:
            object_id, object_name = await self._file_object(environment_id, kind, name)
            if not actor.is_administrator and (object_id, object_name) not in held:
                await self._authoriser.require_access(
                    actor,
                    ResourceKey(environment_id, kind, object_id),
                    "read" if kind == "config" else "write",
                )
        except (ResourceNotFound, PermissionDenied):
            found.errors[field_name] = (
                CONFIG_UNAVAILABLE if kind == "config" else SECRET_UNAVAILABLE
            )
            return
        target = found.configs if kind == "config" else found.secrets
        target[slot] = (object_id, object_name)

    async def _volume_reach(
        self,
        actor: Acting,
        environment_id: int,
        before: Mapping[str, Any] | None,
        spec: Mapping[str, Any],
        manager_node_id: str | None,
        slots: Mapping[str, Sequence[int]],
    ) -> tuple[list[Finding], dict[str, str]]:
        async def access(key: ResourceKey, level: Literal["read", "write"]) -> None:
            await self._require_reach(actor, key, level)

        return await volume_reach(
            access, self._inspector, environment_id, before, spec, manager_node_id, slots
        )

    async def _require_reach(
        self, actor: Acting, key: ResourceKey, level: Literal["read", "write"]
    ) -> None:
        """Check the caller's access to a referenced object; the rule owner is asked."""
        if isinstance(actor, Principal):
            await self._authoriser.require_access(actor, key, level)
            return
        try:
            object_key = ObjectKey(key.environment_id, key.resource_type, key.resource_id)
        except ValueError:
            raise ResourceNotFound("not found") from None
        await (actor.authorise if level == "write" else actor.authorise_read)(object_key)

    async def _pin(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        image: str,
        choice: CredentialChoice | None,
    ) -> PinnedImage | None:
        try:
            return await pin_image(self._pull, client, actor, environment_id, image, choice)
        except RebindRefused as refused:
            raise ServiceInvalid({"image": str(refused)}, reason="restricted") from None

    async def _assemble(
        self,
        actor: Principal,
        environment_id: int,
        client: EngineClient,
        form: ServiceInput,
        *,
        current: Mapping[str, Any] | None,
        choice: CredentialChoice | None,
        credentials: bool,
    ) -> Plan:
        """Resolve, pin, build and check a spec; ServiceInvalid or RestrictedService on refusal.

        With `credentials` false the image is left as typed and no credential is resolved or read.
        """
        administrator = actor.is_administrator
        held = None if current is None else references(current)
        resolved = await self._resolve(actor, environment_id, client, form, held)
        if resolved.errors:
            raise ServiceInvalid(resolved.errors)
        manager = None
        if form.run_on_manager or (
            not administrator and any(m.type == "volume" and m.source for m in form.mounts)
        ):
            manager = await self._manager_node_id(client, environment_id)
        image = form.image
        current_image = None if current is None else image_of(current)
        image_changed = current_image != image
        pinned: PinnedImage | None = None
        if credentials and image_changed:
            pinned = await self._pin(actor, environment_id, client, image, choice)
            if pinned is not None:
                image = pinned.image
        refs = ResolvedRefs(
            networks=resolved.networks,
            configs=resolved.configs,
            secrets=resolved.secrets,
            image=image,
            manager_node_id=manager,
        )
        spec = build_spec(form, refs) if current is None else merge_spec(current, form, refs)
        slots = _slots_of(form, spec)
        findings = [*find_restricted_service(spec, slots=slots), *resolved.findings]
        errors: dict[str, str] = {}
        if not administrator:
            more, errors = await self._volume_reach(
                actor, environment_id, current, spec, manager, slots
            )
            findings.extend(more)
        self._refuse(actor, findings, errors)
        return Plan(form, spec, tuple(findings), image_changed, pinned, manager)

    async def _rollback_checks(
        self,
        actor: Acting,
        environment_id: int,
        client: EngineClient,
        current: Mapping[str, Any],
        previous: Mapping[str, Any],
    ) -> list[Finding]:
        """Run the restriction walker and the reach rule over the spec a rollback restores."""
        findings = find_restricted_service(previous)
        errors: dict[str, str] = {}
        added = added_references(current, previous)
        administrator = is_administrator(actor)
        added_targets = {target for target, _ in added.networks}
        for target, path in references(previous).networks:
            try:
                network = await self._network(environment_id, target)
            except ResourceNotFound:
                errors[path] = NETWORK_UNAVAILABLE
                continue
            if not administrator and target in added_targets:
                try:
                    await self._require_reach(
                        actor, ResourceKey(environment_id, "network", network.id), "write"
                    )
                except (ResourceNotFound, PermissionDenied):
                    errors[path] = NETWORK_UNAVAILABLE
                    continue
            if network.driver == "host":
                findings.append(reach_finding("network_host", f"{path}.Target"))
        if not administrator:
            for object_id, _, file in added.configs:
                await self._reach_file(actor, environment_id, "config", object_id, file, errors)
            for object_id, _, file in added.secrets:
                await self._reach_file(actor, environment_id, "secret", object_id, file, errors)
            manager = await self._manager_node_id(client, environment_id)
            more, volume_errors = await self._volume_reach(
                actor, environment_id, current, previous, manager, {}
            )
            findings.extend(more)
            errors.update(volume_errors)
        self._refuse(actor, findings, errors)
        return findings

    async def _reach_file(
        self,
        actor: Acting,
        environment_id: int,
        kind: Literal["config", "secret"],
        object_id: str,
        path: str,
        errors: dict[str, str],
    ) -> None:
        try:
            if not is_canonical_id(kind, object_id):
                raise ResourceNotFound("not found")
            await self._require_reach(
                actor,
                ResourceKey(environment_id, kind, object_id),
                "read" if kind == "config" else "write",
            )
        except (ResourceNotFound, PermissionDenied):
            errors[path or kind] = CONFIG_UNAVAILABLE if kind == "config" else SECRET_UNAVAILABLE

    def _refuse(
        self, actor: Acting, findings: Sequence[Finding], errors: Mapping[str, str]
    ) -> None:
        if is_administrator(actor):
            return
        if findings:
            fields = {finding.key: finding.message for finding in reversed(findings)}
            fields.update(errors)
            raise RestrictedService(findings, fields)
        if errors:
            raise ServiceInvalid(errors, detail={"fields": ",".join(sorted(errors))[:200]})


def _slots_of(form: ServiceInput, spec: Mapping[str, Any]) -> dict[str, list[int]]:
    """Map each mount and network of the final spec to the form row that produced it."""
    container = mapping_of(mapping_of(spec.get("TaskTemplate")).get("ContainerSpec"))
    by_key = {(row.target, row.type): row.slot for row in form.mounts}
    mounts: list[int] = []
    for ordinal, mount in enumerate(entries_of(container.get("Mounts"))):
        mounts.append(by_key.get((str(mount.get("Target")), str(mount.get("Type"))), ordinal))
    return {"mounts": mounts, "networks": [row.slot for row in form.networks]}
