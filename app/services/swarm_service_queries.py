"""Swarm service lists, details, forms and reviews."""

import shlex
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Literal

from app.auth.errors import (
    PermissionDenied,
    ProductError,
    ResourceNotFound,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.repositories.environment import Environment
from app.db.repositories.pagination import Page
from app.docker_engine.client import EngineClient
from app.docker_engine.models import ServiceStatus, SwarmService, SwarmTask
from app.services.access import ResourceKey, ResourceRef, is_canonical_id
from app.services.authorisation import Authoriser
from app.services.container_restrictions import refused_options
from app.services.containers import (
    AccessPanel,
    parse_engine_time,
    stack_name,
)
from app.services.host_objects import (
    DEFAULT_PAGE_SIZE,
    check_limit,
    label_views,
    read_networks,
    row_id,
)
from app.services.image_bindings import RebindRefused
from app.services.list_order import ListOrder, Ordering, moment_key, number_key, text_key
from app.services.object_inspector import (
    EngineObjectInspector,
)
from app.services.pull_credentials import (
    CredentialChoice,
    PullCredentialResolver,
)
from app.services.service_restrictions import (
    references,
)
from app.services.service_spec import (
    MAX_REPLICAS,
    MAX_REPLICAS_STANDARD,
    ServiceInput,
    changed_categories,
    force_counter,
    form_values,
    image_of,
    mode_of,
    parse_service_form,
    replicas_of,
)
from app.services.swarm import (
    TaskTable,
    build_task_table,
    link_candidates,
    linkable_tasks,
    node_hostnames,
    swarm_read,
)
from app.services.swarm_common import (
    entries_of,
    mapping_of,
)
from app.services.swarm_configs import AccessPanelReader
from app.services.swarm_gate import require_swarm
from app.services.swarm_service_base import (
    MAX_LIST_ROWS,
    MAX_NAME,
    Entry,
    SwarmServiceBase,
    as_int,
    as_list,
    clip,
    display_image,
    entry_matches,
    entry_order,
    env_view,
    migrate_legacy_networks,
    mode_text,
    mount_view,
    ports_of,
    resources_view,
    restart_view,
    rollout_view,
    row_of,
    service_ref,
    service_status,
    spec_view_model,
    status_view,
)
from app.services.swarm_service_views import (
    FileRef,
    ForceImpact,
    NetworkUse,
    NodeCount,
    NoPreviousSpec,
    NotReplicated,
    RemoveImpact,
    RollbackImpact,
    ScaleImpact,
    ServiceChangedSinceReview,
    ServiceDetail,
    ServiceEditOffer,
    ServiceFilters,
    ServiceInvalid,
    ServicePage,
    StateCount,
    UpdateReview,
)

__all__ = [
    "MODE_FILTERS",
    "SwarmServiceQueries",
    "check_mode",
    "parse_replicas_for",
]

MAX_QUERY: Final = 128


MAX_NODES_NAMED: Final = 50


MODE_FILTERS: Final = ("replicated", "global", "job")


MODE_SWITCH: Final = "A service cannot change between replicated and global mode. Create a new one."


def _desired_replicas(entry: Entry) -> int | None:
    status = entry.service.service_status
    if status is not None:
        return status.desired_tasks
    return replicas_of(spec_view_model(entry.service))


LIST_ORDER: Final = ListOrder[Entry](
    default="name",
    keys={
        "name": text_key(lambda e: e.sort_name),
        "mode": text_key(lambda e: mode_text(spec_view_model(e.service))),
        "replicas": number_key(_desired_replicas),
        "image": text_key(lambda e: display_image(image_of(spec_view_model(e.service)) or "")),
        "stack": text_key(lambda e: stack_name(e.service.spec.labels)),
        "updated": number_key(lambda e: moment_key(parse_engine_time(e.service.updated_at))),
    },
    name=lambda e: e.sort_name,
    row_id=lambda e: e.row_id,
    entry_id=lambda e: e.service.id,
)


class SwarmServiceQueries(SwarmServiceBase):
    """Reads services through the authoriser and prepares the forms and confirmations."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        pull: PullCredentialResolver,
        controls_read: AccessPanelReader,
        poke: Callable[[int], None],
    ) -> None:
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._pull = pull
        self._panels = controls_read
        self.grantees = controls_read.grantees
        self._poke = poke

    async def page(
        self,
        actor: Principal,
        environment_id: int,
        filters: ServiceFilters | None = None,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
        sort: str | None = None,
        direction: str | None = None,
    ) -> ServicePage:
        """Return one page of the services the caller may see, read live from the manager.

        ValidationFailed for a bad filter, limit, sort, direction or cursor; ResourceNotFound
        unless the environment is a Swarm manager the caller can see; EnvironmentUnavailable and
        SwarmListTooLarge when the manager cannot be read in full.
        """
        wanted = filters or ServiceFilters()
        if wanted.mode and wanted.mode not in MODE_FILTERS:
            raise ValidationFailed({"mode": "Choose replicated, global or job."})
        if len(wanted.q or "") > MAX_QUERY or len(wanted.stack or "") > MAX_QUERY:
            raise ValidationFailed({"q": f"Filters are at most {MAX_QUERY} characters."})
        wanted = ServiceFilters(
            (wanted.q or "").strip() or None,
            wanted.mode or None,
            (wanted.stack or "").strip().casefold() or None,
        )
        size = check_limit(limit)
        ordering = Ordering.resolve(LIST_ORDER, sort, direction)
        position = ordering.position(cursor)
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        client = await self._hosts.client(environment_id)
        services = await swarm_read(client.swarm_services.list(), environment_id, self._poke)
        entries = [
            Entry(s, service_ref(environment_id, s), s.spec.name.casefold(), row_id(s.id))
            for s in services
        ]
        allowed = {
            r.resource_id
            for r in await self._authoriser.filter_visible(actor, [e.ref for e in entries])
        }
        visible = [e for e in entries if e.service.id in allowed]
        window = ordering.window((e for e in visible if entry_matches(e, wanted)), position, size)
        writable = {
            r.resource_id
            for r in await self._authoriser.filter_visible(
                actor, [e.ref for e in window.items], "write"
            )
        }
        rows = [
            row_of(environment_id, e.service, can_write=e.service.id in writable)
            for e in window.items
        ]
        return ServicePage(
            page=Page(rows, window.next_cursor),
            total=window.total,
            visible=len(visible),
            limit=size,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            can_create=await self._authoriser.may_create(actor, environment_id),
        )

    async def names(
        self, actor: Principal, environment_id: int, prefix: str, *, limit: int
    ) -> list[str]:
        """Return the first `limit` names, by name, of visible services starting with `prefix`.

        Case is ignored. ResourceNotFound unless the environment is a Swarm manager the caller
        may see; one manager read, EnvironmentUnavailable when it cannot be read.
        """
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        client = await self._hosts.client(environment_id)
        services = await swarm_read(client.swarm_services.list(), environment_id, self._poke)
        wanted = prefix.casefold()
        entries = [
            Entry(s, service_ref(environment_id, s), s.spec.name.casefold(), row_id(s.id))
            for s in services
            if s.spec.name.casefold().startswith(wanted)
        ]
        allowed = {
            r.resource_id
            for r in await self._authoriser.filter_visible(actor, [e.ref for e in entries])
        }
        visible = sorted((e for e in entries if e.service.id in allowed), key=entry_order)
        return [e.service.spec.name for e in visible[:limit]]

    async def origin_name(self, actor: Principal, environment_id: int, service_id: str) -> str:
        """Return the name of a service the caller may read; ResourceNotFound otherwise."""
        _, service = await self._load(actor, environment_id, service_id, "read")
        return service.spec.name[:MAX_NAME]

    async def detail(self, actor: Principal, environment_id: int, service_id: str) -> ServiceDetail:
        """Return one service for the detail page; ResourceNotFound unless it may be read."""
        environment, service = await self._load(actor, environment_id, service_id, "read")
        administrator = actor.is_administrator
        spec = service.spec
        task = spec.task_template
        container = mapping_of(task.get("ContainerSpec"))
        placement = mapping_of(task.get("Placement"))
        client = await self._hosts.client(environment_id)
        view = spec_view_model(service)
        image = image_of(view) or ""
        held = references({"TaskTemplate": task})
        targets = {target for target, _ in held.networks}
        names = await self._network_names(actor, environment_id, client, targets)
        args = container.get("Args")
        previous = service.previous_spec
        writable = bool(
            await self._authoriser.filter_visible(
                actor, [service_ref(environment_id, service)], "write"
            )
        )
        status = await self._status_of(client, environment_id, service.id)
        replicas = replicas_of(view)
        return ServiceDetail(
            environment_id=environment_id,
            environment_name=environment.name,
            is_local=environment.connection_kind == "unix_socket",
            id=service.id,
            short_id=service.id[:12],
            name=spec.name[:MAX_NAME],
            created_at=parse_engine_time(service.created_at),
            updated_at=parse_engine_time(service.updated_at),
            version=service.version.index,
            mode=mode_text(view),
            replicas=replicas,
            running=status.running_tasks,
            desired=status.desired_tasks,
            image=image[:512],
            image_display=display_image(image)[:512],
            digest_pinned="@sha256:" in image,
            command=shlex.join(args)[:4096]
            if isinstance(args, list) and all(isinstance(a, str) for a in args)
            else "",
            stack=stack_name(spec.labels),
            labels=label_views(spec.labels, administrator),
            env=env_view(container, administrator),
            ports=ports_of(service),
            endpoint_mode=str(mapping_of(spec.endpoint_spec).get("Mode") or "vip"),
            constraints=tuple(
                clip(c) for c in as_list(placement.get("Constraints")) if isinstance(c, str)
            ),
            preferences=tuple(
                f"spread={clip(mapping_of(mapping_of(p).get('Spread')).get('SpreadDescriptor'))}"
                for p in as_list(placement.get("Preferences"))
            ),
            max_replicas_per_node=as_int(placement.get("MaxReplicas")) or None,
            resources=resources_view(task),
            restart=restart_view(task),
            update_config=rollout_view(spec.update_config),
            rollback_config=rollout_view(spec.rollback_config),
            networks=tuple(
                NetworkUse(
                    clip(mapping_of(n).get("Target")),
                    names.get(str(mapping_of(n).get("Target"))),
                    tuple(
                        clip(a, 128)
                        for a in as_list(mapping_of(n).get("Aliases"))
                        if isinstance(a, str)
                    ),
                )
                for n in as_list(task.get("Networks"))[:MAX_LIST_ROWS]
            ),
            mounts=mount_view(container, administrator),
            configs=await self._file_views(actor, environment_id, "config", container),
            secrets=await self._file_views(actor, environment_id, "secret", container),
            update_status=status_view(service),
            previous_image=None
            if previous is None
            else image_of({"TaskTemplate": previous.task_template}),
            force_counter=force_counter({"TaskTemplate": task}),
            can_write=writable,
            can_scale=writable and replicas is not None,
            can_roll_back=writable and previous is not None,
            can_manage_access=administrator,
            replicas_maximum=MAX_REPLICAS if administrator else MAX_REPLICAS_STANDARD,
        )

    async def tasks(self, actor: Principal, environment_id: int, service_id: str) -> TaskTable:
        """Return the service's tasks grouped by slot or node; read access to the service.

        This is all the tasks panel needs: the table names its environment and service, so a
        poll does not read the service detail. Container links are offered only for tasks that
        ran on the connected manager.
        """
        _, service = await self._load(actor, environment_id, service_id, "read")
        client = await self._hosts.client(environment_id)
        tasks = await swarm_read(
            client.swarm_tasks.list(filters={"service": [service_id]}),
            environment_id,
            self._poke,
        )
        nodes = await swarm_read(client.swarm_nodes.list(), environment_id, self._poke)
        manager = await self._manager_node_id(client, environment_id)
        linkable = await linkable_tasks(
            self._authoriser, actor, environment_id, client, link_candidates(tasks, manager)
        )
        return build_task_table(
            tasks,
            mode=mode_text(spec_view_model(service)),
            hostnames=node_hostnames(nodes),
            linkable=linkable,
            environment_id=environment_id,
            service_id=service_id,
        )

    async def access_panel(
        self, actor: Principal, environment_id: int, service_id: str
    ) -> AccessPanel:
        """Return the access tab's content; administrators only."""
        _, service = await self._load(actor, environment_id, service_id, "read")
        if not actor.is_administrator:
            raise PermissionDenied("administrator role required")
        key = ResourceKey(environment_id, "service", service.id)
        return await self._panels.panel(actor, key, service.spec.name[:MAX_NAME], service.id[:12])

    async def require_creator(self, actor: Principal, environment_id: int) -> None:
        """Raise ResourceNotFound or PermissionDenied unless the caller may create services."""
        await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authoriser.require_create(actor, environment_id)

    async def create_offer(self, actor: Principal, environment_id: int) -> None:
        """Refuse unless the caller may create and the manager answers.

        ResourceNotFound or PermissionDenied from the gate; EnvironmentUnavailable when the
        manager cannot be read.
        """
        await self.require_creator(actor, environment_id)
        client = await self._hosts.client(environment_id)
        await self._manager_node_id(client, environment_id)

    async def edit_offer(
        self, actor: Principal, environment_id: int, service_id: str
    ) -> ServiceEditOffer:
        """Return the edit form for a service the caller may write, with withheld values empty.

        `version` is the version the form is built from. The form posts it back as
        `form_version`, and `review` refuses a form built from an older one.
        """
        environment, _ = await self._load(actor, environment_id, service_id, "write")
        self._inspector.forget_service(environment_id, service_id)
        document = await self._document(environment_id, service_id)
        current = migrate_legacy_networks(document.spec)
        return ServiceEditOffer(
            environment_id=environment_id,
            environment_name=environment.name,
            service_id=service_id,
            name=str(document.spec.get("Name") or "")[:MAX_NAME],
            version=document.version,
            mode=mode_text(current),
            values=form_values(current, administrator=actor.is_administrator),
            administrator=actor.is_administrator,
        )

    async def review(
        self,
        actor: Principal,
        environment_id: int,
        service_id: str,
        form: Mapping[str, str],
        *,
        form_version: int,
        credential_choice: CredentialChoice | None = None,
    ) -> UpdateReview:
        """Check an edit exactly as the update would and name what it changes; nothing is sent.

        `form_version` is the version the edit form was built from. ServiceChangedSinceReview
        when the service has moved on since, ServiceInvalid and RestrictedService with fields
        keyed by form field name, CredentialChoiceRequired when several credential sets fit a
        changed image.
        """
        environment, _ = await self._load(actor, environment_id, service_id, "write")
        client = await self._hosts.client(environment_id)
        self._inspector.forget_service(environment_id, service_id)
        document = await self._document(environment_id, service_id)
        if document.version != form_version:
            raise ServiceChangedSinceReview()
        current = migrate_legacy_networks(document.spec)
        parsed = parse_service_form(
            form, creating=False, administrator=actor.is_administrator, current=current
        )
        check_mode(parsed, current)
        plan = await self._assemble(
            actor,
            environment_id,
            client,
            parsed,
            current=current,
            choice=credential_choice,
            credentials=False,
        )
        if plan.image_changed and not parsed.image.startswith("sha256:"):
            decide = getattr(self._pull, "decide", None)
            if decide is not None:
                try:
                    await decide(
                        environment_id, parsed.image, principal=actor, choice=credential_choice
                    )
                except RebindRefused as refused:
                    raise ServiceInvalid({"image": str(refused)}, reason="restricted") from None
        categories = changed_categories(current, plan.spec)
        status = await self._status_of(client, environment_id, service_id)
        return UpdateReview(
            environment_id=environment_id,
            environment_name=environment.name,
            service_id=service_id,
            name=str(document.spec.get("Name") or "")[:MAX_NAME],
            version=document.version,
            categories=categories,
            image_before=image_of(current),
            image_after=image_of(plan.spec),
            image_changed=plan.image_changed,
            tasks_replaced=bool(set(categories) & TASK_CATEGORIES),
            running_tasks=status.running_tasks,
            restricted=refused_options(plan.findings) if actor.is_administrator else (),
        )

    async def scale_impact(
        self, actor: Principal, environment_id: int, service_id: str, replicas: int
    ) -> ScaleImpact:
        """Describe a change of replica count; NotReplicated unless the service is replicated."""
        environment, service = await self._load(actor, environment_id, service_id, "write")
        current = replicas_of(spec_view_model(service))
        if current is None:
            raise NotReplicated()
        parse_replicas_for(actor, replicas)
        return ScaleImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            service_id=service_id,
            name=service.spec.name[:MAX_NAME],
            version=service.version.index,
            current=current,
            requested=replicas,
            stops=max(current - replicas, 0),
            confirm_required=replicas < current,
        )

    async def force_impact(
        self, actor: Principal, environment_id: int, service_id: str
    ) -> ForceImpact:
        """Name the running tasks a forced update replaces, by node."""
        environment, service = await self._load(actor, environment_id, service_id, "write")
        client = await self._hosts.client(environment_id)
        tasks, hostnames = await self._running_tasks(client, environment_id, service_id)
        nodes, more = _by_node(tasks, hostnames)
        return ForceImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            service_id=service_id,
            name=service.spec.name[:MAX_NAME],
            version=service.version.index,
            running_tasks=len(tasks),
            nodes=nodes,
            nodes_more=more,
        )

    async def rollback_impact(
        self, actor: Principal, environment_id: int, service_id: str
    ) -> RollbackImpact:
        """Describe returning to the previous configuration; NoPreviousSpec when there is none.

        A standard user is refused here, with the fields that would be refused, exactly as the
        rollback would.
        """
        environment, _ = await self._load(actor, environment_id, service_id, "write")
        self._inspector.forget_service(environment_id, service_id)
        document = await self._document(environment_id, service_id)
        if document.previous_spec is None:
            raise NoPreviousSpec()
        current = migrate_legacy_networks(document.spec)
        previous = migrate_legacy_networks(document.previous_spec)
        client = await self._hosts.client(environment_id)
        await self._rollback_checks(actor, environment_id, client, current, previous)
        status = await self._status_of(client, environment_id, service_id)
        return RollbackImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            service_id=service_id,
            name=str(document.spec.get("Name") or "")[:MAX_NAME],
            version=document.version,
            categories=changed_categories(current, previous),
            image_before=image_of(current),
            image_after=image_of(previous),
            running_tasks=status.running_tasks,
        )

    async def remove_impact(
        self, actor: Principal, environment_id: int, service_id: str
    ) -> RemoveImpact:
        """Name what removing the service takes away: its tasks by state and node, and its ports."""
        environment, service = await self._load(actor, environment_id, service_id, "write")
        client = await self._hosts.client(environment_id)
        tasks = await swarm_read(
            client.swarm_tasks.list(filters={"service": [service_id]}),
            environment_id,
            self._poke,
        )
        nodes = await swarm_read(client.swarm_nodes.list(), environment_id, self._poke)
        live = [t for t in tasks if t.desired_state == "running"]
        states: dict[str, int] = {}
        for task in live:
            states[task.status.state] = states.get(task.status.state, 0) + 1
        by_node, more = _by_node(live, node_hostnames(nodes))
        view = spec_view_model(service)
        return RemoveImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            service_id=service_id,
            name=service.spec.name[:MAX_NAME],
            version=service.version.index,
            mode=mode_text(view),
            replicas=replicas_of(view),
            states=tuple(StateCount(state, count) for state, count in sorted(states.items())),
            nodes=by_node,
            nodes_more=more,
            ports=ports_of(service),
            stack=stack_name(service.spec.labels),
        )

    async def _load(
        self,
        actor: Principal,
        environment_id: int,
        service_id: str,
        level: Literal["read", "write"],
    ) -> tuple[Environment, SwarmService]:
        if not is_canonical_id("service", service_id):
            raise ResourceNotFound("not found")
        environment = await require_swarm(self._authoriser, self._hosts, actor, environment_id)
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "service", service_id), level
        )
        return environment, await self._inspector.service(environment_id, service_id)

    async def _document(self, environment_id: int, service_id: str) -> Any:
        return await self._inspector.service_document(environment_id, service_id)

    async def _status_of(
        self, client: EngineClient, environment_id: int, service_id: str
    ) -> ServiceStatus:
        return await service_status(client, environment_id, service_id, self._poke)

    async def _running_tasks(
        self, client: EngineClient, environment_id: int, service_id: str
    ) -> tuple[list[SwarmTask], dict[str, str]]:
        tasks = await swarm_read(
            client.swarm_tasks.list(filters={"service": [service_id]}),
            environment_id,
            self._poke,
        )
        nodes = await swarm_read(client.swarm_nodes.list(), environment_id, self._poke)
        running = [t for t in tasks if t.status.state == "running" and t.desired_state == "running"]
        return running, node_hostnames(nodes)

    async def _network_names(
        self, actor: Principal, environment_id: int, client: EngineClient, targets: set[str]
    ) -> dict[str, str]:
        if not targets:
            return {}
        try:
            networks = await read_networks(client)
        except ProductError:
            return {}
        wanted = [n for n in networks if n.id in targets]
        if not actor.is_administrator:
            refs = [ResourceRef(environment_id, "network", n.id, n.labels) for n in wanted]
            allowed = {r.resource_id for r in await self._authoriser.filter_visible(actor, refs)}
            wanted = [n for n in wanted if n.id in allowed]
        return {n.id: n.name[:MAX_NAME] for n in wanted}

    async def _file_views(
        self,
        actor: Principal,
        environment_id: int,
        kind: Literal["config", "secret"],
        container: Mapping[str, Any],
    ) -> tuple[FileRef, ...]:
        group = "Configs" if kind == "config" else "Secrets"
        title = "Config" if kind == "config" else "Secret"
        views: list[FileRef] = []
        for entry in entries_of(container.get(group))[:MAX_LIST_ROWS]:
            file = mapping_of(entry.get("File"))
            if not file:
                continue
            object_id = str(entry.get(f"{title}ID") or "")
            linkable = is_canonical_id(kind, object_id)
            if linkable and not actor.is_administrator:
                try:
                    await self._authoriser.require_access(
                        actor, ResourceKey(environment_id, kind, object_id), "read"
                    )
                except (ResourceNotFound, PermissionDenied):
                    linkable = False
            mode = as_int(file.get("Mode"))
            views.append(
                FileRef(
                    object_id=object_id,
                    name=clip(entry.get(f"{title}Name")),
                    target=clip(file.get("Name")),
                    uid=clip(file.get("UID"), 32),
                    gid=clip(file.get("GID"), 32),
                    mode=format(mode, "04o") if mode is not None else "",
                    linkable=linkable,
                )
            )
        return tuple(views)


TASK_CATEGORIES: Final = frozenset(
    {
        "image",
        "command",
        "environment",
        "placement",
        "resources",
        "restart_policy",
        "networks",
        "mounts",
        "configs",
        "secrets",
        "healthcheck",
        "process",
        "host",
        "security",
        "ulimits",
        "logging",
        "other",
    }
)


def check_mode(form: ServiceInput, current: Mapping[str, Any]) -> None:
    """Refuse a change between replicated and global mode with a field error keyed `mode`."""
    held = mode_of(current)
    if held in ("replicated", "global") and form.mode != held:
        raise ServiceInvalid({"mode": MODE_SWITCH})


def parse_replicas_for(actor: Principal, replicas: int) -> None:
    """Raise ValidationFailed keyed `replicas` when the count is outside the caller's range."""
    ceiling = MAX_REPLICAS if actor.is_administrator else MAX_REPLICAS_STANDARD
    if isinstance(replicas, bool) or not 0 <= replicas <= ceiling:
        raise ValidationFailed({"replicas": f"Replicas must be between 0 and {ceiling}."})


def _by_node(
    tasks: Sequence[SwarmTask], hostnames: Mapping[str, str]
) -> tuple[tuple[NodeCount, ...], int]:
    counts: dict[str, int] = {}
    for task in tasks:
        name = hostnames.get(task.node_id, "unassigned") if task.node_id else "unassigned"
        counts[name] = counts.get(name, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (item[0].casefold(), item[0]))
    shown = ordered[:MAX_NODES_NAMED]
    return tuple(NodeCount(name, count) for name, count in shown), len(ordered) - len(shown)
