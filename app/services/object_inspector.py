"""Host client lookup for one request, and the inspector the authoriser reads labels from."""

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

from app.auth.errors import ProductError, ResourceNotFound
from app.db.repositories.environment import Environment, EnvironmentRepository
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.docker_engine.client import EngineClient
from app.docker_engine.errors import (
    EngineError,
    EnvironmentUnreachable,
    NotFound,
    ResponseTooLarge,
    VersionUnsupported,
)
from app.docker_engine.models import (
    ConfigDocument,
    ContainerInspect,
    Network,
    ServiceDocument,
    SwarmNode,
    SwarmSecret,
    SwarmService,
    SwarmTask,
    Volume,
)
from app.logging import get_logger
from app.services.access import ResourceType, is_canonical_id
from app.services.environment_connector import EnvironmentConnector, MaterialUnavailable

logger = get_logger(__name__)

_AMBIGUOUS_STATUSES = frozenset({400, 409})
VOLUME_OR_NETWORK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
NETWORK_ID = re.compile(r"[0-9a-f]{64}|[0-9a-z]{25}")
SWARM_OBJECT_ID = re.compile(r"[0-9a-z]{25}")
SWARM_OBJECT_NAME_LENGTH = 64

SWARM_UNAVAILABLE_STATUS = 503

type UnavailableKind = Literal[
    "unreachable", "unsupported", "credentials", "engine", "outcome_unknown", "not_swarm_manager"
]

_UNAVAILABLE_MESSAGES: Mapping[UnavailableKind, str] = {
    "unreachable": "The Docker host could not be reached.",
    "unsupported": "The Docker host runs an Engine API version this product does not support.",
    "credentials": "The connection details stored for this environment cannot be used.",
    "engine": "The Docker host returned an unexpected response.",
    "outcome_unknown": "The Docker host did not answer in time. The action may still complete.",
    "not_swarm_manager": "This environment is not an active Swarm manager, or its swarm is locked.",
}


class EnvironmentUnavailable(ProductError):
    """The environment's host cannot be used now; the message is fixed and holds no Engine text."""

    def __init__(self, kind: UnavailableKind = "unreachable") -> None:
        super().__init__(_UNAVAILABLE_MESSAGES[kind])
        self.kind = kind


class SwarmUnavailable(EnvironmentUnavailable):
    """The host answered a Swarm call as a node that cannot serve it; the message is fixed."""

    def __init__(self) -> None:
        super().__init__("not_swarm_manager")


class ObjectTooLarge(ProductError):
    """An inspect reply is over the read limit; nothing partial is used."""

    def __init__(self, message: str = "This object's configuration is too large to read.") -> None:
        super().__init__(message)


class ContainerTooLarge(ObjectTooLarge):
    """A container's inspect reply is over the read limit; nothing partial is shown."""

    def __init__(self) -> None:
        super().__init__("This container's configuration is too large to show.")


def unavailable_from(error: EngineError) -> EnvironmentUnavailable:
    """Map an Engine failure to the product error that names its category and nothing else."""
    if isinstance(error, VersionUnsupported):
        return EnvironmentUnavailable("unsupported")
    if isinstance(error, EnvironmentUnreachable):
        return EnvironmentUnavailable("unreachable")
    return EnvironmentUnavailable("engine")


def swarm_unavailable_from(
    error: EngineError, environment_id: int, poke: Callable[[int], None] | None
) -> EnvironmentUnavailable:
    """Map an Engine failure from a Swarm call; a 503 asks for a fresh snapshot of the host.

    The stored type can lag the host (a demoted manager, a node that left), and the Engine
    answers every Swarm call on a node that is not a usable manager with 503.
    """
    if error.status_code == SWARM_UNAVAILABLE_STATUS:
        if poke is not None:
            poke(environment_id)
        return SwarmUnavailable()
    return unavailable_from(error)


class HostClients:
    """Resolves an environment id to its row and a negotiated Engine client, once per request."""

    def __init__(
        self,
        *,
        environments: EnvironmentRepository,
        tls: EnvironmentTlsRepository,
        connector: EnvironmentConnector,
    ) -> None:
        self._environments = environments
        self._tls = tls
        self._connector = connector
        self._rows: dict[int, Environment] = {}

    async def environment(self, environment_id: int) -> Environment:
        """Return the environment row; ResourceNotFound when it does not exist."""
        row = self._rows.get(environment_id)
        if row is None:
            row = await self._environments.get(environment_id)
            if row is None:
                raise ResourceNotFound("not found")
            self._rows[environment_id] = row
        return row

    async def client(self, environment_id: int) -> EngineClient:
        """Return a negotiated client, or raise EnvironmentUnavailable without any Engine call.

        A row already known to be version_unsupported is refused before a client is built.
        """
        environment = await self.environment(environment_id)
        if environment.status == "version_unsupported":
            raise EnvironmentUnavailable("unsupported")
        stored = await self._tls.get(environment_id)
        try:
            return await self._connector.client(environment, stored)
        except MaterialUnavailable:
            raise EnvironmentUnavailable("credentials") from None
        except EngineError as error:
            logger.debug(
                "host_client_unavailable",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            raise unavailable_from(error) from None
        except (OSError, ValueError) as error:
            logger.warning(
                "host_client_unusable",
                extra={"environment_id": environment_id, "error": type(error).__name__},
            )
            raise EnvironmentUnavailable("credentials") from None


class EngineObjectInspector:
    """Reads container, volume, network and Swarm object documents, remembered per request."""

    def __init__(self, hosts: HostClients, *, poke: Callable[[int], None] | None = None) -> None:
        self._hosts = hosts
        self._poke = poke
        self._inspected: dict[tuple[int, str], ContainerInspect] = {}
        self._volumes: dict[tuple[int, str], Volume] = {}
        self._networks: dict[tuple[int, str], Network] = {}
        self._network_ids: dict[tuple[int, str], str] = {}
        self._services: dict[tuple[int, str], SwarmService] = {}
        self._service_documents: dict[tuple[int, str], ServiceDocument] = {}
        self._configs: dict[tuple[int, str], ConfigDocument] = {}
        self._secrets: dict[tuple[int, str], SwarmSecret] = {}
        self._config_ids: dict[tuple[int, str], str] = {}
        self._secret_ids: dict[tuple[int, str], str] = {}
        self._nodes: dict[tuple[int, str], SwarmNode] = {}
        self._tasks: dict[tuple[int, str], SwarmTask] = {}

    @property
    def hosts(self) -> HostClients:
        """Return the host lookup this inspector reads through."""
        return self._hosts

    async def labels_of(
        self, environment_id: int, resource_type: ResourceType, resource_id: str
    ) -> Mapping[str, str]:
        """Return the labels of a container, volume (by name), network, service, config, secret."""
        match resource_type:
            case "container":
                return (await self.inspect(environment_id, resource_id)).config.labels
            case "volume":
                return (await self.volume(environment_id, resource_id)).labels
            case "network":
                return (await self.network(environment_id, resource_id)).labels
            case "service":
                return (await self.service(environment_id, resource_id)).spec.labels
            case "config":
                return (await self.config(environment_id, resource_id)).config.labels
            case "secret":
                return (await self.secret(environment_id, resource_id)).labels
            case _:
                raise RuntimeError(f"no inspector is wired for {resource_type} objects")

    async def inspect(self, environment_id: int, container_id: str) -> ContainerInspect:
        """Return the container's inspect, from this request's memo after the first call.

        ResourceNotFound when the id is not canonical, the Engine has no such container, or the
        Engine resolved the id to a different container (a name equal to another's id).
        ContainerTooLarge when the reply is over the read limit.
        """
        if not is_canonical_id("container", container_id):
            raise ResourceNotFound("not found")
        key = (environment_id, container_id)
        held = self._inspected.get(key)
        if held is not None:
            return held
        client = await self._hosts.client(environment_id)
        try:
            inspected = await client.containers.inspect(container_id)
        except NotFound:
            raise ResourceNotFound("not found") from None
        except ResponseTooLarge:
            raise ContainerTooLarge from None
        except EngineError as error:
            raise unavailable_from(error) from None
        if inspected.id != container_id:
            raise ResourceNotFound("not found")
        self._inspected[key] = inspected
        return inspected

    async def volume(self, environment_id: int, name: str) -> Volume:
        """Return the volume called `name`, from this request's memo after the first call.

        ResourceNotFound when the name is not a valid volume name, the Engine has no such
        volume, or the volume it returned has a different name. ObjectTooLarge past the read
        limit.
        """
        if VOLUME_OR_NETWORK_NAME.fullmatch(name) is None:
            raise ResourceNotFound("not found")
        key = (environment_id, name)
        held = self._volumes.get(key)
        if held is not None:
            return held
        client = await self._hosts.client(environment_id)
        inspected = await _read_object(client.volumes.inspect(name))
        if inspected.name != name:
            raise ResourceNotFound("not found")
        self._volumes[key] = inspected
        return inspected

    async def network(self, environment_id: int, network_id: str) -> Network:
        """Return the network with this full id, from this request's memo after the first call.

        ResourceNotFound when the id is not 64 hex digits or 25 base-36 characters, the Engine has
        no such network, or the Engine resolved the argument to a network with another id (a name
        or prefix that happens to look like one). ObjectTooLarge past the read limit.
        """
        if NETWORK_ID.fullmatch(network_id) is None:
            raise ResourceNotFound("not found")
        key = (environment_id, network_id)
        held = self._networks.get(key)
        if held is not None:
            return held
        client = await self._hosts.client(environment_id)
        inspected = await _read_object(client.networks.inspect(network_id))
        if inspected.id != network_id:
            raise ResourceNotFound("not found")
        self._networks[key] = inspected
        return inspected

    async def network_named(self, environment_id: int, name: str) -> Network:
        """Return the network called `name`; the result is also remembered under its id.

        ResourceNotFound when the name is not a valid network name, no single network has it,
        the Engine resolved it as an id or id prefix instead, or the network's id has neither id
        shape. ObjectTooLarge past the read limit.
        """
        if VOLUME_OR_NETWORK_NAME.fullmatch(name) is None:
            raise ResourceNotFound("not found")
        key = (environment_id, name)
        known = self._network_ids.get(key)
        if known is not None:
            return await self.network(environment_id, known)
        client = await self._hosts.client(environment_id)
        inspected = await _read_object(client.networks.inspect(name), ambiguous_is_absent=True)
        if inspected.name != name or NETWORK_ID.fullmatch(inspected.id) is None:
            raise ResourceNotFound("not found")
        self._networks[(environment_id, inspected.id)] = inspected
        self._network_ids[key] = inspected.id
        return inspected

    async def service(self, environment_id: int, service_id: str) -> SwarmService:
        """Return the service with this id, from this request's memo after the first call.

        ResourceNotFound when the id is not 25 base-36 characters, the Engine has no such
        service, or it resolved the argument to a service with another id (a name or prefix that
        looks like an id). ObjectTooLarge past the read limit.
        """
        if not is_canonical_id("service", service_id):
            raise ResourceNotFound("not found")
        key = (environment_id, service_id)
        held = self._services.get(key)
        if held is not None:
            return held
        client = await self._hosts.client(environment_id)
        inspected = await _read_object(
            client.swarm_services.inspect(service_id), swarm=(environment_id, self._poke)
        )
        if inspected.id != service_id:
            raise ResourceNotFound("not found")
        self._services[key] = inspected
        return inspected

    async def service_document(self, environment_id: int, service_id: str) -> ServiceDocument:
        """Return the raw, unbudgeted spec document an update round-trips, remembered per request.

        Same refusals as `service`.
        """
        if not is_canonical_id("service", service_id):
            raise ResourceNotFound("not found")
        key = (environment_id, service_id)
        held = self._service_documents.get(key)
        if held is not None:
            return held
        client = await self._hosts.client(environment_id)
        document = await _read_object(
            client.swarm_services.document(service_id), swarm=(environment_id, self._poke)
        )
        if document.id != service_id:
            raise ResourceNotFound("not found")
        self._service_documents[key] = document
        return document

    async def config(self, environment_id: int, config_id: str) -> ConfigDocument:
        """Return the config with this id and its data, from this request's memo.

        ResourceNotFound when the id is not 25 base-36 characters, the Engine has no such config,
        or it resolved the argument to a config with another id. ObjectTooLarge past the read
        limit.
        """
        if not is_canonical_id("config", config_id):
            raise ResourceNotFound("not found")
        key = (environment_id, config_id)
        held = self._configs.get(key)
        if held is not None:
            return held
        client = await self._hosts.client(environment_id)
        document = await _read_object(
            client.swarm_configs.inspect(config_id), swarm=(environment_id, self._poke)
        )
        if document.config.id != config_id:
            raise ResourceNotFound("not found")
        self._configs[key] = document
        return document

    async def secret(self, environment_id: int, secret_id: str) -> SwarmSecret:
        """Return the secret with this id; it never carries a value.

        Same refusals as `config`.
        """
        if not is_canonical_id("secret", secret_id):
            raise ResourceNotFound("not found")
        key = (environment_id, secret_id)
        held = self._secrets.get(key)
        if held is not None:
            return held
        client = await self._hosts.client(environment_id)
        inspected = await _read_object(
            client.swarm_secrets.inspect(secret_id), swarm=(environment_id, self._poke)
        )
        if inspected.id != secret_id:
            raise ResourceNotFound("not found")
        self._secrets[key] = inspected
        return inspected

    async def config_named(self, environment_id: int, name: str) -> ConfigDocument:
        """Return the config called `name`; the result is also remembered under its id.

        ResourceNotFound when the name is not a valid config name, no single config has it, the
        Engine resolved it as an id or id prefix instead, or the id is not 25 base-36
        characters. ObjectTooLarge past the read limit.
        """
        if not 0 < len(name) <= SWARM_OBJECT_NAME_LENGTH:
            raise ResourceNotFound("not found")
        key = (environment_id, name)
        known = self._config_ids.get(key)
        if known is not None:
            return await self.config(environment_id, known)
        client = await self._hosts.client(environment_id)
        document = await _read_object(
            client.swarm_configs.inspect(name),
            ambiguous_is_absent=True,
            swarm=(environment_id, self._poke),
        )
        if document.config.name != name or not is_canonical_id("config", document.config.id):
            raise ResourceNotFound("not found")
        self._configs[(environment_id, document.config.id)] = document
        self._config_ids[key] = document.config.id
        return document

    async def secret_named(self, environment_id: int, name: str) -> SwarmSecret:
        """Return the secret called `name`; the result is also remembered under its id.

        Same refusals as `config_named`.
        """
        if not 0 < len(name) <= SWARM_OBJECT_NAME_LENGTH:
            raise ResourceNotFound("not found")
        key = (environment_id, name)
        known = self._secret_ids.get(key)
        if known is not None:
            return await self.secret(environment_id, known)
        client = await self._hosts.client(environment_id)
        inspected = await _read_object(
            client.swarm_secrets.inspect(name),
            ambiguous_is_absent=True,
            swarm=(environment_id, self._poke),
        )
        if inspected.name != name or not is_canonical_id("secret", inspected.id):
            raise ResourceNotFound("not found")
        self._secrets[(environment_id, inspected.id)] = inspected
        self._secret_ids[key] = inspected.id
        return inspected

    async def node(self, environment_id: int, node_id: str) -> SwarmNode:
        """Return the node with this id, from this request's memo after the first call.

        ResourceNotFound when the id is not 25 base-36 characters, the Engine has no such node, or
        it resolved the argument to another node (a hostname that looks like an id).
        ObjectTooLarge past the read limit.
        """
        if SWARM_OBJECT_ID.fullmatch(node_id) is None:
            raise ResourceNotFound("not found")
        key = (environment_id, node_id)
        held = self._nodes.get(key)
        if held is not None:
            return held
        client = await self._hosts.client(environment_id)
        inspected = await _read_object(
            client.swarm_nodes.inspect(node_id), swarm=(environment_id, self._poke)
        )
        if inspected.id != node_id:
            raise ResourceNotFound("not found")
        self._nodes[key] = inspected
        return inspected

    async def task(self, environment_id: int, task_id: str) -> SwarmTask:
        """Return the task with this id, from this request's memo after the first call.

        Same refusals as `node`.
        """
        if SWARM_OBJECT_ID.fullmatch(task_id) is None:
            raise ResourceNotFound("not found")
        key = (environment_id, task_id)
        held = self._tasks.get(key)
        if held is not None:
            return held
        client = await self._hosts.client(environment_id)
        inspected = await _read_object(
            client.swarm_tasks.inspect(task_id), swarm=(environment_id, self._poke)
        )
        if inspected.id != task_id:
            raise ResourceNotFound("not found")
        self._tasks[key] = inspected
        return inspected

    def forget(self, environment_id: int, container_id: str) -> None:
        """Drop the remembered inspect so the next read sees the container as it is now."""
        self._inspected.pop((environment_id, container_id), None)

    def forget_network(self, environment_id: int, network_id: str) -> None:
        """Drop the remembered network so the next read sees its endpoints as they are now."""
        self._networks.pop((environment_id, network_id), None)

    def forget_volume(self, environment_id: int, name: str) -> None:
        """Drop the remembered volume so the next read sees its labels as they are now."""
        self._volumes.pop((environment_id, name), None)

    def forget_service(self, environment_id: int, service_id: str) -> None:
        """Drop the remembered service and its spec document so the next read sees them now."""
        self._services.pop((environment_id, service_id), None)
        self._service_documents.pop((environment_id, service_id), None)

    def forget_config(self, environment_id: int, config_id: str) -> None:
        """Drop the remembered config so the next read sees its labels as they are now."""
        self._configs.pop((environment_id, config_id), None)
        self._config_ids = {k: v for k, v in self._config_ids.items() if v != config_id}

    def forget_secret(self, environment_id: int, secret_id: str) -> None:
        """Drop the remembered secret so the next read sees its labels as they are now."""
        self._secrets.pop((environment_id, secret_id), None)
        self._secret_ids = {k: v for k, v in self._secret_ids.items() if v != secret_id}

    def forget_node(self, environment_id: int, node_id: str) -> None:
        """Drop the remembered node so the next read sees its availability as it is now."""
        self._nodes.pop((environment_id, node_id), None)


async def _read_object[T](
    call: Awaitable[T],
    *,
    ambiguous_is_absent: bool = False,
    swarm: tuple[int, Callable[[int], None] | None] | None = None,
) -> T:
    try:
        return await call
    except NotFound:
        raise ResourceNotFound("not found") from None
    except ResponseTooLarge:
        raise ObjectTooLarge from None
    except EngineError as error:
        if ambiguous_is_absent and error.status_code in _AMBIGUOUS_STATUSES:
            raise ResourceNotFound("not found") from None
        if swarm is not None:
            raise swarm_unavailable_from(error, *swarm) from None
        raise unavailable_from(error) from None
