"""Container attachment to and detachment from networks."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Final, Literal

import anyio

from app.auth.errors import (
    SELF_PROTECTED_REASON,
    ProductError,
    ResourceNotFound,
    SelfProtected,
    ValidationFailed,
)
from app.auth.principal import Principal
from app.db.engine import Database, retry_busy
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.clocked import Clock
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    RequestOutcomeUnknown,
)
from app.docker_engine.models import (
    ContainerInspect,
    EndpointSettings,
    Network,
)
from app.logging import get_logger
from app.self_identity import SelfIdentitySource, no_identity
from app.services.access import ResourceKey, is_canonical_id
from app.services.activity import (
    ActivityRecorder,
    Arguments,
    UserActor,
    audited,
    current_audit,
    environment_argument,
)
from app.services.authorisation import Authoriser
from app.services.container_actions import BusyView, ContainerGone, engine_detail
from app.services.container_spec import DNS_LABEL
from app.services.containers import (
    display_name,
    short_id,
)
from app.services.host_objects import (
    busy_view,
    failure_reason,
    hold_all,
    run_shielded,
    settled,
)
from app.services.network_addressing import (
    AddressingInvalid,
    parse_static_address,
    pools_of,
)
from app.services.networks import (
    MAX_NAME,
    MAX_TEXT,
    NAMESPACE_SHARED,
    SETTLE_SECONDS,
    UNJOINABLE_DRIVERS,
    NetworkGone,
    clip_text,
    container_joins_networks,
    network_kind,
)
from app.services.object_inspector import (
    EngineObjectInspector,
    EnvironmentUnavailable,
    ObjectTooLarge,
)
from app.services.object_locks import LockHolder, ObjectBusy, ObjectKey, ObjectLockRegistry

logger = get_logger(__name__)

__all__ = [
    "AttachInvalid",
    "AttachRefused",
    "ConnectOutcome",
    "DetachRefused",
    "DisconnectImpact",
    "DisconnectOutcome",
    "NetworkAttachmentService",
]

MAX_ALIASES: Final = 16


class AttachRefused(ProductError):
    """Docker refused to connect a container; the Engine's text is not part of the message.

    engine_detail is that text, scrubbed and cut, for a page shown to an administrator.
    """

    def __init__(self, engine_detail: str | None = None) -> None:
        super().__init__(
            "Docker refused to connect this container to the network. The address may be in use."
        )
        self.engine_detail = engine_detail


class DetachRefused(ProductError):
    """Docker refused to disconnect a container; the Engine's text is not part of the message.

    engine_detail is that text, scrubbed and cut, for a page shown to an administrator.
    """

    def __init__(self, engine_detail: str | None = None) -> None:
        super().__init__("Docker refused to disconnect this container from the network.")
        self.engine_detail = engine_detail


class AttachInvalid(ValidationFailed):
    """A connect request the product refuses before asking Docker; fields name the cause."""


@dataclass(frozen=True, slots=True)
class ConnectOutcome:
    """What a connect did."""

    environment_id: int
    container_id: str
    container_name: str
    network_id: str
    network_name: str
    aliases: tuple[str, ...]
    ipv4_address: str | None
    ipv6_address: str | None


@dataclass(frozen=True, slots=True)
class DisconnectImpact:
    """What detaching a container from a network names."""

    environment_id: int
    environment_name: str
    container_id: str
    short_id: str
    container_name: str
    state: str
    network_id: str
    network_name: str
    ipv4_address: str
    ipv6_address: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DisconnectOutcome:
    """What a disconnect did."""

    environment_id: int
    container_id: str
    container_name: str
    network_id: str
    network_name: str


def _container_argument(arguments: Arguments) -> str:
    return str(arguments["container_id"])[:64]


class NetworkAttachmentService:
    """Connect a container to a network and disconnect it, each one audited operation."""

    def __init__(
        self,
        *,
        database: Database,
        authoriser: Authoriser,
        inspector: EngineObjectInspector,
        locks: ObjectLockRegistry,
        activity: ActivityRecorder,
        clock: Clock,
        self_identity: SelfIdentitySource | None = None,
    ) -> None:
        self._database = database
        self._authoriser = authoriser
        self._inspector = inspector
        self._hosts = inspector.hosts
        self._locks = locks
        self._activity = activity
        self._clock = clock
        self._self_identity = self_identity or no_identity

    @audited(
        action="network_connected",
        target_type="container",
        target_id=_container_argument,
        environment_id=environment_argument,
    )
    async def connect(
        self,
        actor: Principal,
        environment_id: int,
        container_id: str,
        network_id: str,
        *,
        aliases: Sequence[str] = (),
        ipv4: str = "",
        ipv6: str = "",
    ) -> ConnectOutcome:
        """Attach a container to a network, with aliases and static addresses where it allows.

        Needs write access over both. Works on a stopped container: Docker applies the endpoint
        at start. AttachInvalid, with the cause keyed by form field, before any Engine call when
        the container or network cannot take the connection; ObjectBusy; AttachRefused.
        """
        audit = current_audit()
        if not is_canonical_id("container", container_id) or not is_canonical_id(
            "network", network_id
        ):
            raise ResourceNotFound("not found")
        try:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "container", container_id), "write"
            )
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "network", network_id), "write"
            )
        except (EnvironmentUnavailable, ObjectTooLarge):
            audit.refuse()
            raise
        await self._hosts.environment(environment_id)
        container, network, client = await self._load(environment_id, container_id, network_id)
        name = display_name([container.name], container.id)
        audit.detail.update(
            name=name,
            network_id=network.id,
            network_name=network.name[:MAX_NAME],
            aliases=len(aliases),
            ipv4_address=ipv4.strip()[:MAX_TEXT] or None,
            ipv6_address=ipv6.strip()[:MAX_TEXT] or None,
        )
        endpoint, kept, address4, address6 = self._checked(container, network, aliases, ipv4, ipv6)
        body: dict[str, Any] = {"Container": container.id}
        if endpoint:
            body["EndpointConfig"] = endpoint
        holder = LockHolder(UserActor.of(actor), actor.username, "connect")
        keys = [
            ObjectKey(environment_id, "container", container.id),
            ObjectKey(environment_id, "network", network.id),
        ]
        try:
            async with hold_all(self._locks, keys, holder):
                try:
                    await run_shielded(
                        partial(client.networks.connect, network.id, body),
                        partial(
                            self._interrupted,
                            actor,
                            "network_connected",
                            environment_id,
                            container.id,
                        ),
                    )
                except EngineError as error:
                    raise await self._failure(
                        error, environment_id, container.id, network.id, connecting=True
                    ) from None
        finally:
            self._inspector.forget(environment_id, container.id)
        return ConnectOutcome(
            environment_id=environment_id,
            container_id=container.id,
            container_name=name,
            network_id=network.id,
            network_name=network.name[:MAX_NAME],
            aliases=kept,
            ipv4_address=address4,
            ipv6_address=address6,
        )

    @audited(
        action="network_disconnected",
        target_type="container",
        target_id=_container_argument,
        environment_id=environment_argument,
    )
    async def disconnect(
        self, actor: Principal, environment_id: int, container_id: str, network_id: str
    ) -> DisconnectOutcome:
        """Detach a container from one of its networks; write access over the container suffices.

        ResourceNotFound when the network is not one of the container's endpoints. Docker is never
        asked to force the disconnect.
        """
        audit = current_audit()
        if not is_canonical_id("container", container_id) or not is_canonical_id(
            "network", network_id
        ):
            raise ResourceNotFound("not found")
        try:
            await self._authoriser.require_access(
                actor, ResourceKey(environment_id, "container", container_id), "write"
            )
        except (EnvironmentUnavailable, ObjectTooLarge):
            audit.refuse()
            raise
        await self._hosts.environment(environment_id)
        container, network, client = await self._load(environment_id, container_id, network_id)
        found = _endpoint_of(container, network_id, network.name)
        if found is None:
            raise ResourceNotFound("not found")
        network_name, _ = found
        name = display_name([container.name], container.id)
        audit.detail.update(name=name, network_id=network_id, network_name=network_name[:MAX_NAME])
        if self._self_identity().protects_container(container.id):
            audit.reason = SELF_PROTECTED_REASON
            raise SelfProtected.detach()
        holder = LockHolder(UserActor.of(actor), actor.username, "disconnect")
        keys = [
            ObjectKey(environment_id, "container", container.id),
            ObjectKey(environment_id, "network", network_id),
        ]
        try:
            async with hold_all(self._locks, keys, holder):
                try:
                    await run_shielded(
                        partial(client.networks.disconnect, network_id, container_id=container.id),
                        partial(
                            self._interrupted,
                            actor,
                            "network_disconnected",
                            environment_id,
                            container.id,
                        ),
                    )
                except EngineError as error:
                    raise await self._failure(
                        error, environment_id, container.id, network_id, connecting=False
                    ) from None
        finally:
            self._inspector.forget(environment_id, container.id)
        return DisconnectOutcome(
            environment_id, container.id, name, network_id, network_name[:MAX_NAME]
        )

    async def disconnect_impact(
        self, actor: Principal, environment_id: int, container_id: str, network_id: str
    ) -> DisconnectImpact:
        """Name what detaching the container from the network does; write holders only."""
        if not is_canonical_id("container", container_id) or not is_canonical_id(
            "network", network_id
        ):
            raise ResourceNotFound("not found")
        await self._authoriser.require_access(
            actor, ResourceKey(environment_id, "container", container_id), "write"
        )
        environment = await self._hosts.environment(environment_id)
        container = await self._inspector.inspect(environment_id, container_id)
        network = await self._inspector.network(environment_id, network_id)
        found = _endpoint_of(container, network_id, network.name)
        if found is None:
            raise ResourceNotFound("not found")
        network_name, endpoint = found
        return DisconnectImpact(
            environment_id=environment_id,
            environment_name=environment.name,
            container_id=container.id,
            short_id=short_id(container.id),
            container_name=display_name([container.name], container.id),
            state=container.state.status,
            network_id=network_id,
            network_name=network_name[:MAX_NAME],
            ipv4_address=clip_text(endpoint.ip_address),
            ipv6_address=clip_text(endpoint.global_ipv6_address),
            aliases=tuple(clip_text(a) for a in endpoint.aliases[:MAX_ALIASES]),
        )

    def describe_busy(self, busy: ObjectBusy, viewer: Principal) -> BusyView:
        """Describe the lock holder to a viewer: names only for administrators."""
        return busy_view(busy, viewer, self._locks.now())

    async def _load(
        self, environment_id: int, container_id: str, network_id: str
    ) -> tuple[ContainerInspect, Network, EngineClient]:
        container, client = await self._load_container(environment_id, container_id)
        audit = current_audit()
        try:
            network = await self._inspector.network(environment_id, network_id)
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise
        except ResourceNotFound:
            audit.reason = "network_gone"
            raise NetworkGone() from None
        return container, network, client

    async def _load_container(
        self, environment_id: int, container_id: str
    ) -> tuple[ContainerInspect, EngineClient]:
        audit = current_audit()
        try:
            container = await self._inspector.inspect(environment_id, container_id)
            client = await self._hosts.client(environment_id)
        except EnvironmentUnavailable as error:
            audit.reason = error.kind
            raise
        except ObjectTooLarge:
            audit.reason = "too_large"
            raise
        except ResourceNotFound:
            audit.reason = "container_gone"
            raise ContainerGone() from None
        return container, client

    def _checked(
        self,
        container: ContainerInspect,
        network: Network,
        aliases: Sequence[str],
        ipv4: str,
        ipv6: str,
    ) -> tuple[dict[str, Any], tuple[str, ...], str | None, str | None]:
        audit = current_audit()
        fields: dict[str, str] = {}
        if not container_joins_networks(container):
            fields["container"] = NAMESPACE_SHARED
        settings = container.network_settings.networks
        if network.driver in UNJOINABLE_DRIVERS:
            fields["network"] = "A container cannot join this network."
        elif network.id in {e.network_id for e in settings.values()} or network.name in settings:
            fields["network"] = "The container is already connected to this network."
        elif network.driver == "overlay" and not network.attachable:
            fields["network"] = "This overlay network is not attachable."
        predefined = network_kind(network) == "predefined"
        kept = tuple(alias.strip() for alias in aliases if alias.strip())
        if kept and predefined:
            fields["aliases"] = "Aliases are not available on this network."
        elif len(kept) > MAX_ALIASES or any(DNS_LABEL.fullmatch(alias) is None for alias in kept):
            fields["aliases"] = (
                f"Use at most {MAX_ALIASES} aliases, each made of letters, digits and hyphens."
            )
        addresses: dict[Literal[4, 6], str] = {}
        in_use = [
            held
            for endpoint in network.containers.values()
            for held in (endpoint.ipv4_address, endpoint.ipv6_address)
            if held
        ]
        families: tuple[tuple[Literal[4, 6], str], ...] = ((4, ipv4), (6, ipv6))
        for version, text in families:
            text = text.strip()
            if not text:
                continue
            field = f"ipv{version}"
            if predefined:
                fields[field] = "Static addresses are not available on this network."
                continue
            try:
                parsed = parse_static_address(
                    text,
                    pools=pools_of(network),
                    enable_ipv6=network.enable_ipv6,
                    in_use=in_use,
                    field=field,
                    version=version,
                )
            except AddressingInvalid as invalid:
                fields.update(invalid.errors)
            else:
                addresses[version] = str(parsed)
        if fields:
            audit.reason = "invalid"
            audit.detail["fields"] = ",".join(sorted(fields))
            raise AttachInvalid(fields)
        endpoint: dict[str, Any] = {}
        if kept:
            endpoint["Aliases"] = list(kept)
        if addresses:
            endpoint["IPAMConfig"] = {
                ("IPv4Address" if version == 4 else "IPv6Address"): text
                for version, text in addresses.items()
            }
        return endpoint, kept, addresses.get(4), addresses.get(6)

    async def _failure(
        self,
        error: EngineError,
        environment_id: int,
        container_id: str,
        network_id: str,
        *,
        connecting: bool,
    ) -> ProductError:
        audit = current_audit()
        logger.warning(
            "network_attachment_refused",
            extra={
                "environment_id": environment_id,
                "connecting": connecting,
                "error": type(error).__name__,
                "status": error.status_code,
            },
        )
        if isinstance(error, NotFound):
            return await self._missing(environment_id, container_id)
        if isinstance(error, RequestOutcomeUnknown):
            audit.reason = "outcome_unknown"
            return EnvironmentUnavailable("outcome_unknown")
        if isinstance(error, EnvironmentUnreachable):
            audit.reason = "unreachable"
            return EnvironmentUnavailable("unreachable")
        audit.reason = "engine_rejected"
        detail = engine_detail(error)
        return AttachRefused(detail) if connecting else DetachRefused(detail)

    async def _missing(self, environment_id: int, container_id: str) -> ProductError:
        audit = current_audit()
        self._inspector.forget(environment_id, container_id)
        try:
            await self._inspector.inspect(environment_id, container_id)
        except ResourceNotFound:
            audit.reason = "container_gone"
            return ContainerGone()
        except ProductError:
            pass
        audit.reason = "network_gone"
        return NetworkGone()

    async def _interrupted(
        self,
        actor: Principal,
        action: str,
        environment_id: int,
        container_id: str,
        task: asyncio.Future[None],
    ) -> None:
        audit = current_audit()
        with anyio.CancelScope(shield=True):
            finished, error = await settled(task, SETTLE_SECONDS)
            detail: dict[str, DetailValue] = {**audit.detail, "interrupted": True}
            self._inspector.forget(environment_id, container_id)
            user = UserActor.of(actor)
            try:
                async with self._database.session() as db:
                    recorder = ActivityRecorder(db, self._clock)
                    if finished and error is None:
                        await retry_busy(
                            partial(
                                recorder.record_event,
                                user,
                                action=action,
                                target_type="container",
                                target_id=container_id,
                                environment_id=environment_id,
                                result="success",
                                detail=detail,
                            )
                        )
                        return
                    reason = failure_reason(error) if error is not None else "outcome_unknown"
                    await recorder.record_failure(
                        user,
                        action=action,
                        target_type="container",
                        target_id=container_id,
                        environment_id=environment_id,
                        reason=reason,
                        detail=detail,
                        message=str(EnvironmentUnavailable("outcome_unknown"))
                        if reason == "outcome_unknown"
                        else None,
                    )
            except Exception as write_error:
                logger.error(
                    "activity_event_write_failed",
                    extra={
                        "action": action,
                        "actor_user_id": actor.user_id,
                        "target_type": "container",
                        "target_id": container_id,
                        "environment_id": environment_id,
                        "error": type(write_error).__name__,
                    },
                )


def _endpoint_of(
    container: ContainerInspect, network_id: str, network_name: str
) -> tuple[str, EndpointSettings] | None:
    """Find the container's endpoint on a network; Docker leaves the id empty before first start."""
    endpoints = container.network_settings.networks
    for name, endpoint in endpoints.items():
        if endpoint.network_id == network_id:
            return name, endpoint
    for name, endpoint in endpoints.items():
        if not endpoint.network_id and name in (network_name, network_id):
            return name, endpoint
    return None
