"""A self identity for the services under test, and the checks every refusal shares."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.auth.errors import SELF_PROTECTED_REASON
from app.db.engine import Session
from app.db.repositories.environment import ConnectionKind
from app.self_identity import HomeEnvironment, SelfIdentity, SelfIdentitySource
from tests.access_support import ENV_A, container_id, service_id
from tests.environment_support import activity_rows

SELF_ID = container_id(0x5E1F)
SELF_NAME = "dockmin"
SELF_VOLUME = "dockmin-data"
SELF_PROJECT = "dockmin-stack"
SELF_SERVICE = service_id(0x5E)
SELF_NODE = service_id(0x4E)


def home_identity(
    environment_id: int = ENV_A,
    *,
    container: str = SELF_ID,
    kind: ConnectionKind = "tcp_tls",
    volumes: tuple[str, ...] = (),
    compose_project: str | None = None,
    swarm_service: str | None = None,
    swarm_node: str | None = None,
) -> SelfIdentity:
    """Return the identity of a dockmin whose container the environment's Engine reports."""
    return SelfIdentity(
        container_id=container,
        source="mountinfo",
        verified=(HomeEnvironment(environment_id, kind, SELF_NAME),),
        volumes={environment_id: frozenset(volumes)} if volumes else {},
        compose_project={environment_id: compose_project.casefold()} if compose_project else {},
        swarm_service={environment_id: swarm_service} if swarm_service else {},
        swarm_node={environment_id: swarm_node} if swarm_node else {},
        restartable_via=environment_id if kind == "unix_socket" else None,
    )


@dataclass(slots=True)
class Held:
    """An identity a test can replace after the services were built around it."""

    identity: SelfIdentity = field(default_factory=SelfIdentity)

    def __call__(self) -> SelfIdentity:
        return self.identity


def source_of_identity(identity: SelfIdentity) -> SelfIdentitySource:
    return Held(identity)


async def self_protected_rows(db: Session) -> list[dict[str, Any]]:
    """Return every recorded failure whose reason is the self-protection one."""
    return [
        row
        for row in await activity_rows(db)
        if row["result"] == "failure" and row["detail"].get("reason") == SELF_PROTECTED_REASON
    ]


async def failure_reasons(db: Session) -> Mapping[str, str | None]:
    """Return each failure row's action mapped to its recorded reason."""
    return {
        row["action"]: row["detail"].get("reason")
        for row in await activity_rows(db)
        if row["result"] == "failure"
    }
