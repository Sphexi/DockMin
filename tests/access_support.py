"""Principals, identifiers, a scripted object inspector and seed data for authorisation tests."""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.db.engine import Database, Session
from app.db.repositories.clocked import Clock
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.resource_control import (
    AccessLevel,
    ControlStanding,
    ResourceControlRepository,
    ResourceType,
)
from app.db.repositories.user import UserRepository
from app.services.access import (
    COMPOSE_PROJECT_LABEL,
    SWARM_NAMESPACE_LABEL,
    ResourceKey,
)
from app.services.assembly import AccessServices
from app.services.resource_controls import ControlSpec, ControlView, control_digest
from tests.auth_support import ADMIN_ID, ADMIN_USERNAME, STANDARD_ID, STANDARD_USERNAME
from tests.db.helpers import CREATED_AT, PASSWORD_HASH, at

STAMP = "2026-01-02T03:04:05Z"
SOCKET_PATH = "/var/run/docker.sock"
REMOTE_URL = "tcp://docker-a.example.test:2376"

ENV_A = 1
ENV_B = 2
ENV_A_NAME = "environment-a"
ENV_B_NAME = "environment-b"

MEMBER_ID = 3
PEER_ID = 4
LEADER_ID = 5
MEMBER_USERNAME = "member-user"
PEER_USERNAME = "peer-user"
LEADER_USERNAME = "leader-user"

ADMIN = Principal(ADMIN_ID, ADMIN_USERNAME, "administrator")
STANDARD = Principal(STANDARD_ID, STANDARD_USERNAME, "standard")
MEMBER = Principal(MEMBER_ID, MEMBER_USERNAME, "standard")
PEER = Principal(PEER_ID, PEER_USERNAME, "standard")
LEADER = Principal(LEADER_ID, LEADER_USERNAME, "standard")

UNKNOWN_USER_ID = 9999
UNKNOWN_TEAM_ID = 9999

STACK_NAME = "shop"
TEAM_NAME = "platform"
OTHER_TEAM_NAME = "support"


def container_id(number: int) -> str:
    """Return a well-formed container id: 64 lower-case hex digits."""
    return f"{number:064x}"


def service_id(number: int) -> str:
    """Return a well-formed service id: 25 lower-case alphanumerics."""
    return f"{number:025d}"


def container_key(number: int = 1, environment_id: int = ENV_A) -> ResourceKey:
    """Return the key of the numbered container."""
    return ResourceKey(environment_id, "container", container_id(number))


def volume_key(name: str, environment_id: int = ENV_A) -> ResourceKey:
    """Return the key of a volume, which Docker identifies by name."""
    return ResourceKey(environment_id, "volume", name)


def stack_key(name: str, environment_id: int = ENV_A) -> ResourceKey:
    """Return the key of a stack, which is identified by name."""
    return ResourceKey(environment_id, "stack", name)


def compose_labels(project: str) -> dict[str, str]:
    """Return the labels Compose puts on a container of a project."""
    return {COMPOSE_PROJECT_LABEL: project}


def swarm_labels(namespace: str) -> dict[str, str]:
    """Return the labels Swarm puts on a service of a stack."""
    return {SWARM_NAMESPACE_LABEL: namespace}


@dataclass(frozen=True, slots=True)
class ListedObject:
    """A view model as a Docker listing would produce it."""

    environment_id: int
    resource_type: ResourceType
    resource_id: str
    labels: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, key: ResourceKey, labels: Mapping[str, str] | None = None) -> "ListedObject":
        """Build the listing entry for a key."""
        return cls(key.environment_id, key.resource_type, key.resource_id, labels or {})


class InspectorCall(NamedTuple):
    """One label lookup the inspector served."""

    environment_id: int
    resource_type: ResourceType
    resource_id: str


class FakeInspector:
    """Serves the labels of the objects a test says Docker holds, and records every lookup."""

    def __init__(self) -> None:
        self._objects: dict[InspectorCall, Mapping[str, str]] = {}
        self.calls: list[InspectorCall] = []

    def add(self, key: ResourceKey, labels: Mapping[str, str] | None = None) -> None:
        """Make Docker report the object with these labels."""
        self._objects[InspectorCall(*_parts(key))] = dict(labels or {})

    def remove(self, key: ResourceKey) -> None:
        """Make Docker forget the object, as a redeploy does to the old container ids."""
        del self._objects[InspectorCall(*_parts(key))]

    async def labels_of(
        self, environment_id: int, resource_type: ResourceType, resource_id: str
    ) -> Mapping[str, str]:
        """Return the labels Docker holds, or refuse as an unknown object is refused."""
        call = InspectorCall(environment_id, resource_type, resource_id)
        self.calls.append(call)
        if call not in self._objects:
            raise ResourceNotFound("not found")
        return self._objects[call]


class Scan(NamedTuple):
    """One read of a type's controls: the type and the ids it was narrowed to, None for all."""

    resource_type: ResourceType
    resource_ids: frozenset[str] | None


class RecordingControls(ResourceControlRepository):
    """The real repository, noting every standing scan the authoriser asks for."""

    def __init__(self, session: Session, clock: Clock) -> None:
        super().__init__(session, clock)
        self.scans: list[Scan] = []

    async def list_standing(
        self,
        user_id: int,
        environment_id: int,
        resource_type: ResourceType,
        resource_ids: Collection[str] | None = None,
    ) -> list[ControlStanding]:
        """Record the scan, then run it."""
        narrowed = None if resource_ids is None else frozenset(resource_ids)
        self.scans.append(Scan(resource_type, narrowed))
        return await super().list_standing(user_id, environment_id, resource_type, resource_ids)


def _parts(key: ResourceKey) -> tuple[int, ResourceType, str]:
    return key.environment_id, key.resource_type, key.resource_id


def standing(
    *,
    control_id: int = 1,
    resource_id: str = "object",
    is_public: bool = False,
    is_administrators_only: bool = False,
    access_level: AccessLevel = "read",
    is_owner: bool = False,
    user_granted: bool = False,
    team_granted: bool = False,
) -> ControlStanding:
    """Build one user's standing on one control, with nothing granted unless asked."""
    return ControlStanding(
        control_id=control_id,
        resource_id=resource_id,
        is_public=is_public,
        is_administrators_only=is_administrators_only,
        access_level=access_level,
        is_owner=is_owner,
        user_granted=user_granted,
        team_granted=team_granted,
    )


async def control(
    access: AccessServices,
    key: ResourceKey,
    *,
    level: str = "read",
    public: bool = False,
    administrators_only: bool = False,
    owner: int | None = None,
    users: tuple[int, ...] = (),
    teams: tuple[int, ...] = (),
) -> ControlView:
    """Put an object under a control through the service, as an administrator would."""
    spec = ControlSpec(
        access_level=level,
        is_public=public,
        is_administrators_only=administrators_only,
        owner_user_id=owner,
        user_ids=users,
        team_ids=teams,
    )
    return await access.controls.create(
        ADMIN, key, spec, expected_digest=await digest_of(access, key)
    )


async def digest_of(access: AccessServices, key: ResourceKey) -> str:
    """Return the digest of the control over an object as an administrator's form would carry it."""
    return control_digest(await access.controls.get(ADMIN, key))


async def seed_world(path: Path) -> None:
    """Add three more standard users and two environments to a database that holds ids 1 and 2.

    ENV_A is a remote TLS host and ENV_B the local socket, where a standard user is capped at
    read. Every standard user is granted both.
    """
    async with Database(path).session() as session:
        users = UserRepository(session, clock=at(CREATED_AT))
        created = [
            (await users.create(name, PASSWORD_HASH, "standard")).id
            for name in (MEMBER_USERNAME, PEER_USERNAME, LEADER_USERNAME)
        ]
        if created != [MEMBER_ID, PEER_ID, LEADER_ID]:
            raise RuntimeError(f"seeded users received unexpected ids: {created}")
        environments = [
            (
                await session.execute(
                    "INSERT INTO environment (name, type, connection_kind, socket_path, url, "
                    "created_at, updated_at) VALUES (?, 'standalone', ?, ?, ?, ?, ?)",
                    (name, kind, socket_path, url, STAMP, STAMP),
                )
            ).last_row_id
            for name, kind, socket_path, url in (
                (ENV_A_NAME, "tcp_tls", None, REMOTE_URL),
                (ENV_B_NAME, "unix_socket", SOCKET_PATH, None),
            )
        ]
        if environments != [ENV_A, ENV_B]:
            raise RuntimeError(f"seeded environments received unexpected ids: {environments}")
        grants = EnvironmentAccessRepository(session)
        for environment_id in (ENV_A, ENV_B):
            for user_id in (STANDARD_ID, MEMBER_ID, PEER_ID, LEADER_ID):
                await grants.add_user("environment", environment_id, user_id)


async def team_of(access: AccessServices, name: str, *members: Principal) -> int:
    """Create a team through the service, add the members, and return its id."""
    team = await access.teams.create(ADMIN, name=name)
    for member in members:
        await access.teams.add_member(ADMIN, team.id, username=member.username)
    return team.id


async def bulk_controls(
    db: Session,
    environment_id: int,
    resource_type: ResourceType,
    id_format: str,
    count: int,
) -> None:
    """Insert count controls that grant nobody anything, named by printf-formatting 1..count."""
    await db.execute(
        "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM seq WHERE n < ?) "
        "INSERT INTO resource_control (environment_id, resource_type, resource_id, "
        "access_level, created_at, updated_at) "
        "SELECT ?, ?, printf(?, n), 'read', ?, ? FROM seq",
        (count, environment_id, resource_type, id_format, STAMP, STAMP),
    )


async def plant(
    controls: ResourceControlRepository,
    key: ResourceKey,
    *,
    public: bool = False,
    administrators_only: bool = False,
    level: AccessLevel = "read",
    owner: int | None = None,
) -> int:
    """Write a control row exactly as given, skipping the service's normalisation; return its id."""
    created = await controls.create(
        key.environment_id,
        key.resource_type,
        key.resource_id,
        is_public=public,
        is_administrators_only=administrators_only,
        access_level=level,
        owner_user_id=owner,
    )
    return created.id
