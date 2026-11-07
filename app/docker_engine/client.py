"""The per-environment Engine client: negotiation plus the typed resource surface."""

from types import TracebackType
from typing import Self

from app.docker_engine.engine_http import EngineHttp
from app.docker_engine.models import DaemonInfo
from app.docker_engine.resources import (
    ConfigsApi,
    ContainersApi,
    DistributionApi,
    ExecApi,
    ImagesApi,
    NetworksApi,
    NodesApi,
    SecretsApi,
    ServicesApi,
    SwarmApi,
    SystemApi,
    TasksApi,
    VolumesApi,
)
from app.docker_engine.transport import ConnectionSettings, TransportBundle


class EngineClient:
    """Talks to one Docker Engine; build it over any transport bundle, including a test double."""

    def __init__(
        self,
        environment_id: int,
        bundle: TransportBundle,
        settings: ConnectionSettings | None = None,
    ) -> None:
        self._http = EngineHttp(environment_id, bundle, settings)
        self.containers = ContainersApi(self._http)
        self.execs = ExecApi(self._http)
        self.images = ImagesApi(self._http)
        self.networks = NetworksApi(self._http)
        self.volumes = VolumesApi(self._http)
        self.system = SystemApi(self._http)
        self.swarm = SwarmApi(self._http)
        self.swarm_services = ServicesApi(self._http)
        self.swarm_tasks = TasksApi(self._http)
        self.swarm_nodes = NodesApi(self._http)
        self.swarm_configs = ConfigsApi(self._http)
        self.swarm_secrets = SecretsApi(self._http)
        self.distribution = DistributionApi(self._http)

    def __repr__(self) -> str:
        return f"EngineClient(environment_id={self.environment_id})"

    @property
    def environment_id(self) -> int:
        """Return the environment this client talks to."""
        return self._http.environment_id

    @property
    def closed(self) -> bool:
        """Report whether the client has been closed."""
        return self._http.closed

    @property
    def open_hijack_count(self) -> int:
        """Return how many hijacked connections are open and not yet closed by their holders."""
        return self._http.open_hijack_count

    @property
    def daemon(self) -> DaemonInfo | None:
        """Return the negotiated daemon facts, or None before negotiation has succeeded."""
        return self._http.daemon

    async def negotiate(self) -> DaemonInfo:
        """Negotiate now, replacing any earlier result; may raise VersionUnsupported."""
        return await self._http.negotiate()

    async def aclose(self) -> None:
        """Close every connection, ending streams with EnvironmentUnreachable and hijacks at EOF."""
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
