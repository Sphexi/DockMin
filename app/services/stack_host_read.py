"""The audited read of a stack's files from its host, for a Compose project or a Swarm namespace."""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from app.auth.principal import Principal
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import EngineError, NotFound
from app.docker_engine.models import ContainerSummary
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import canonical_stack_key
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.compose_generate import SERVICE_LABEL, generate_compose
from app.services.compose_host_read import ComposeFileReader, FilesRead
from app.services.guards import require_administrator
from app.services.host_files import HostFileReader
from app.services.host_objects import audited_client, hold_all
from app.services.object_inspector import EngineObjectInspector, unavailable_from
from app.services.object_locks import LockHolder, ObjectLockRegistry
from app.services.stack_common import declared_service_images
from app.services.stack_deploy import StackRejected, in_project
from app.services.stack_document import ComposeDocumentRejected, swarm_stack_name
from app.services.stack_queries import StackQueryService, read_project_containers, stack_key
from app.services.stack_read_result import (
    HostReadResult,
    NeedsFile,
    ReadOutcome,
    compose_host_state,
    swarm_host_state,
)
from app.services.stack_runs import named_reasons
from app.services.stack_views import checked_name
from app.services.swarm_gate import require_swarm
from app.services.swarm_stack_plan import SwarmStackPlanner
from app.services.swarm_stack_reconstruct import reconstruct_stack
from app.services.swarm_stacks import read_namespace

__all__ = ["HostReadReport", "StackHostReader"]

ONEOFF_LABEL: Final = "com.docker.compose.oneoff"
NUMBER_LABEL: Final = "com.docker.compose.container-number"
MAX_INSPECTED_SERVICES: Final = 64

_KEPT_LABELS: Final = ("com.docker.compose.project", SERVICE_LABEL, ONEOFF_LABEL, NUMBER_LABEL)


@dataclass(frozen=True, slots=True)
class HostReadReport:
    """What reading a stack's files from its host produced.

    `outcome` is the file to adopt or the reason none exists. `attempted` is why the project's
    own files were not used when `outcome` is a generated file or a second refusal.
    """

    name: str
    outcome: ReadOutcome
    services: tuple[str, ...]
    attempted: NeedsFile | None = None
    files_read: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()
    helper_container: str | None = None
    helper_removed: bool | None = None

    @property
    def result(self) -> HostReadResult | None:
        """Return the prepared stack, or None when a file is still needed."""
        return self.outcome if isinstance(self.outcome, HostReadResult) else None


def _project_target(arguments: Arguments) -> str | None:
    return str(arguments["project"]).strip() or None


def _namespace_target(arguments: Arguments) -> str | None:
    return str(arguments["namespace"]).strip() or None


def _checked_namespace(namespace: str) -> str:
    try:
        return swarm_stack_name(namespace)
    except ComposeDocumentRejected as rejected:
        raise StackRejected(rejected.fields, rejected.reason) from None


class StackHostReader:
    """Reads stack files from a host, administrators only; each read is one audit scope."""

    def __init__(
        self,
        *,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        queries: StackQueryService,
        planner: SwarmStackPlanner,
        locks: ObjectLockRegistry,
        activity: ActivityRecorder,
        poke: Callable[[int], None] | None = None,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._authoriser = authoriser
        self._hosts = inspector.hosts
        self._queries = queries
        self._planner = planner
        self._locks = locks
        self._activity = activity
        self._poke = poke
        self._self_identity = self_identity or no_identity

    @audited(
        action="stack_files_read",
        target_type="stack",
        target_id=_project_target,
        environment_id=environment_argument,
    )
    async def read_compose(
        self, actor: Principal, environment_id: int, project: str
    ) -> HostReadReport:
        """Read an unmanaged project's files from its host, or write a file from its containers.

        A helper container is created and removed on the host; nothing is started, and nothing
        is stored here. The project's own files are tried first; when they cannot be used a
        file is generated from its containers, and the report names why.
        """
        audit = current_audit()
        with named_reasons():
            await self._authoriser.require_environment(actor, environment_id)
            require_administrator(actor)
            await self._hosts.environment(environment_id)
            name = checked_name(project)
            audit.target_id = name
            client = await audited_client(self._hosts, environment_id)
            holder = LockHolder(UserActor.of(actor), actor.username, "read")
            async with hold_all(self._locks, [stack_key(environment_id, name)], holder):
                await self._queries.require_adoptable(environment_id, name, client)
                report = await self._compose_report(actor, environment_id, name, client)
        self._record(report, "compose")
        return report

    async def _compose_report(
        self, actor: Principal, environment_id: int, name: str, client: EngineClient
    ) -> HostReadReport:
        key = canonical_stack_key(name)
        containers = [c for c in await read_project_containers(client) if in_project(c.labels, key)]
        identity = self._self_identity()
        try:
            if identity.mounts_unknown(environment_id):
                attempt = FilesRead(NeedsFile("identity_unverified", "self_identity"))
            else:
                attempt = await ComposeFileReader(HostFileReader(client), self._merge).read(
                    actor,
                    name,
                    containers,
                    lambda path: identity.protects_path(environment_id, path),
                )
            attempted: NeedsFile | None = None
            outcome = attempt.outcome
            if isinstance(outcome, NeedsFile):
                attempted = outcome
                outcome = await self._generated(name, containers, client)
        except EngineError as error:
            raise unavailable_from(error) from None
        return _report(
            name, _with_state(outcome, compose_host_state(containers)), attempt, attempted
        )

    async def _merge(self, name: str, texts: Sequence[str]) -> str:
        cli = await self._queries.ready()
        return await cli.merged_config(name, texts)

    async def _generated(
        self, name: str, containers: Sequence[ContainerSummary], client: EngineClient
    ) -> ReadOutcome:
        groups: dict[str, list[ContainerSummary]] = {}
        for container in containers:
            groups.setdefault(container.labels.get(SERVICE_LABEL, ""), []).append(container)
        if len(groups) > MAX_INSPECTED_SERVICES:
            return NeedsFile("too_many_services", SERVICE_LABEL)
        documents: list[Mapping[str, Any]] = []
        for members in groups.values():
            members.sort(key=_order)
            first = await client.containers.inspect_document(members[0].id)
            if first.get("Id") != members[0].id:
                return NeedsFile("inspect_mismatch", SERVICE_LABEL)
            documents.append(first)
            documents.extend(_stub(member) for member in members[1:])
        images: dict[str, Mapping[str, Any]] = {}
        for document in documents:
            image_id = document.get("Image")
            if isinstance(image_id, str) and image_id not in images and "Config" in document:
                try:
                    inspected = await client.images.inspect(image_id)
                except NotFound:
                    continue
                images[image_id] = {"Config": inspected.config}
        return await asyncio.to_thread(generate_compose, name, documents, images)

    @audited(
        action="stack_files_read",
        target_type="stack",
        target_id=_namespace_target,
        environment_id=environment_argument,
    )
    async def read_swarm(
        self, actor: Principal, environment_id: int, namespace: str
    ) -> HostReadReport:
        """Reconstruct an unmanaged namespace's file from the cluster's specs; nothing changes."""
        audit = current_audit()
        with named_reasons():
            await require_swarm(self._authoriser, self._hosts, actor, environment_id)
            require_administrator(actor)
            name = _checked_namespace(namespace)
            audit.target_id = name
            client = await audited_client(self._hosts, environment_id)
            holder = LockHolder(UserActor.of(actor), actor.username, "read")
            async with hold_all(self._locks, [stack_key(environment_id, name)], holder):
                await self._planner.require_adoptable(environment_id, name, client)
                try:
                    outcome = await self._reconstructed(environment_id, name, client)
                except EngineError as error:
                    raise unavailable_from(error) from None
        report = _report(name, outcome, FilesRead(outcome), None)
        self._record(report, "swarm")
        return report

    async def _reconstructed(
        self, environment_id: int, name: str, client: EngineClient
    ) -> ReadOutcome:
        key = canonical_stack_key(name)
        members = await read_namespace(client, environment_id, self._poke, key)
        services: list[dict[str, Any]] = []
        for service in members.services:
            document = await client.swarm_services.document(service.id)
            if document.id != service.id:
                return NeedsFile("inspect_mismatch", "services")
            services.append({"ID": document.id, "Spec": document.spec})
        configs: list[dict[str, Any]] = []
        for config in members.configs:
            held = await client.swarm_configs.inspect(config.id)
            if held.config.id != config.id:
                return NeedsFile("inspect_mismatch", "configs")
            spec: dict[str, Any] = {
                "Name": config.name,
                "Labels": config.labels,
                "Data": held.data,
            }
            if config.templated:
                spec["Templating"] = {"Name": "golang"}
            configs.append({"ID": config.id, "Spec": spec})
        secrets = [
            {"ID": secret.id, "Spec": {"Name": secret.name, "Labels": secret.labels}}
            for secret in members.secrets
        ]
        networks = await _network_documents(
            client, [n.id for n in members.networks], [s["Spec"] for s in services]
        )
        outcome = await asyncio.to_thread(
            reconstruct_stack, name, services, networks, configs, secrets
        )
        return _with_state(outcome, swarm_host_state(members.services))

    def _record(self, report: HostReadReport, kind: str) -> None:
        detail = current_audit().detail
        result = report.result
        detail["file_source"] = result.file_source if result else "none"
        detail["files_read"] = len(report.files_read)
        refused = sorted({reason for _, reason in report.refused})
        if refused:
            detail["refused"] = ",".join(refused)
        if report.helper_container:
            detail["helper_container"] = report.helper_container
        if isinstance(report.outcome, NeedsFile):
            detail["needs_file"] = report.outcome.reason
        if report.attempted is not None:
            detail["fallback_reason"] = report.attempted.reason
        if kind == "swarm":
            detail["kind"] = "swarm"


def _with_state(outcome: ReadOutcome, state: tuple[str, ...]) -> ReadOutcome:
    return replace(outcome, host_state=state) if isinstance(outcome, HostReadResult) else outcome


def _report(
    name: str, outcome: ReadOutcome, attempt: FilesRead, attempted: NeedsFile | None
) -> HostReadReport:
    services = (
        tuple(declared_service_images(outcome.compose_text))
        if isinstance(outcome, HostReadResult)
        else ()
    )
    return HostReadReport(
        name=name,
        outcome=outcome,
        services=services,
        attempted=attempted,
        files_read=attempt.files_read,
        refused=attempt.refused,
        helper_container=attempt.helper_container,
        helper_removed=attempt.helper_removed,
    )


def _order(container: ContainerSummary) -> tuple[int, str]:
    number = container.labels.get(NUMBER_LABEL, "")
    return (int(number) if number.isdigit() else 0, container.id)


def _stub(container: ContainerSummary) -> dict[str, Any]:
    labels = {key: container.labels[key] for key in _KEPT_LABELS if key in container.labels}
    return {"Id": container.id, "Config": {"Labels": labels}}


async def _network_documents(
    client: EngineClient, members: Sequence[str], specs: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    wanted = dict.fromkeys(members)
    for spec in specs:
        template = spec.get("TaskTemplate")
        attached = template.get("Networks") if isinstance(template, Mapping) else None
        for attachment in attached if isinstance(attached, list) else []:
            target = attachment.get("Target") if isinstance(attachment, Mapping) else None
            if isinstance(target, str) and target:
                wanted[target] = None
    documents: list[dict[str, Any]] = []
    for target in wanted:
        try:
            documents.append(await client.networks.inspect_document(target))
        except NotFound:
            continue
    return documents
