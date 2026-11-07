"""Start-up and shutdown of what registry credentials need: the exchange client and the caches."""

from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

from app.crypto.vault import Vault
from app.db.engine import Database
from app.db.repositories.clocked import Clock
from app.registry_client.ecr import DerivedSecretSource, EcrTokenSource, new_ecr_http_client
from app.registry_client.manifests import ManifestClient
from app.registry_client.probe import RegistryProbeCoordinator
from app.services.image_updates import UpdateCheckState
from app.services.registry_resolution import DerivedSecretCache


@dataclass(slots=True)
class RegistryRuntime:
    """The process-wide registry state: connection tests, derived secrets and update checks."""

    vault: Vault
    derivers: Mapping[str, DerivedSecretSource]
    cache: DerivedSecretCache
    probes: RegistryProbeCoordinator
    http: httpx.AsyncClient
    manifests: ManifestClient = field(default_factory=ManifestClient)
    update_checks: UpdateCheckState = field(default_factory=UpdateCheckState)

    async def aclose(self) -> None:
        """Cancel running tests and checks and close the clients; no task survives."""
        await self.update_checks.aclose()
        await self.probes.aclose()
        await self.manifests.aclose()
        await self.http.aclose()


def build_registry_runtime(*, database: Database, vault: Vault, clock: Clock) -> RegistryRuntime:
    """Assemble the registry runtime; it runs nothing until a test or a pull needs it."""
    http = new_ecr_http_client()
    derivers: Mapping[str, DerivedSecretSource] = {"ecr": EcrTokenSource(http, clock)}
    return RegistryRuntime(
        vault=vault,
        derivers=derivers,
        cache=DerivedSecretCache(database=database, vault=vault, derivers=derivers, clock=clock),
        probes=RegistryProbeCoordinator(derivers=derivers),
        http=http,
        manifests=ManifestClient(),
        update_checks=UpdateCheckState(clock),
    )
