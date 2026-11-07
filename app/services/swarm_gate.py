"""The Swarm gate: a visible environment that is a manager."""

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.db.repositories.environment import Environment
from app.services.acting import Acting
from app.services.authorisation import Authoriser
from app.services.object_inspector import (
    SWARM_UNAVAILABLE_STATUS,
    HostClients,
    SwarmUnavailable,
    swarm_unavailable_from,
)

__all__ = [
    "SWARM_UNAVAILABLE_STATUS",
    "SwarmUnavailable",
    "require_swarm",
    "swarm_unavailable_from",
]


async def require_swarm(
    authoriser: Authoriser, hosts: HostClients, actor: Acting, environment_id: int
) -> Environment:
    """Return the environment when the caller may see it and it is a Swarm manager.

    ResourceNotFound for a missing, hidden, standalone, worker or never-contacted environment,
    before any Engine call and for every role. An automation run is not asked whether it may see
    the environment; its owner is checked on the object by `authorise`.
    """
    if isinstance(actor, Principal):
        await authoriser.require_environment(actor, environment_id)
    environment = await hosts.environment(environment_id)
    if environment.type != "swarm":
        raise ResourceNotFound("not found")
    return environment
