"""Sessions, reveal services and stacks with secret variables, shared by the reveal tests."""

import json
from datetime import timedelta
from typing import Any

from app.auth.errors import ResourceNotFound
from app.auth.principal import Principal
from app.auth.services import AuthRuntime, AuthServices
from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.stack import StackKind, StackRepository, StoredStack, VariableWrite
from app.db.repositories.user import Role
from app.db.repositories.user_session import UserSessionRepository
from app.services.activity import ActivityRecorder
from app.services.reveal_access import RevealAccess
from app.services.secret_mask import MASK
from app.services.stack_variable_reveal import StackLookup, StackVariableReveal
from tests.access_support import ENV_A
from tests.auth_support import read_events

SESSION_LIFETIME = timedelta(hours=12)
STACK_NAME = "shop"
SECRET_NAME = "API_TOKEN"
PLAIN_NAME = "MODE"
PLAIN_VALUE = "production"
SENTINEL_VALUE = "reveal-sentinel-0123456789-abcdef"
EXPOSED_SENTINEL = "reve" + MASK + "cdef"
SHORT_SECRET = "short-secret-value"
FILE = "services:\n  web:\n    image: x\n"
DIGEST = "d" * 64


async def open_session(
    db: Session,
    runtime: AuthRuntime,
    user_id: int,
    username: str,
    role: Role,
    *,
    token: str,
) -> Principal:
    """Create a password session for a user and return the principal that session resolves to."""
    created = await UserSessionRepository(db, runtime.clock).create(
        user_id, token, lifetime=SESSION_LIFETIME, user_agent=None, remote_address=None
    )
    return Principal(user_id, username, role, created.id, "password")


def reveal_access(db: Session, runtime: AuthRuntime, services: AuthServices) -> RevealAccess:
    """Wire the window service as one request would."""
    return RevealAccess(
        db=db,
        users=services.users,
        sessions=UserSessionRepository(db, runtime.clock),
        activity=ActivityRecorder(db, runtime.clock),
        hasher=runtime.hasher,
        throttle=services.throttle,
        clock=runtime.clock,
    )


def repository_lookup(stacks: StackRepository, kind: StackKind) -> StackLookup:
    """Return a stack lookup that finds a stack of one kind by name and nothing else."""

    async def find(actor: Principal, environment_id: int, name: str) -> StoredStack:
        found = await stacks.get(environment_id, name)
        if found is None or found.kind != kind:
            raise ResourceNotFound("not found")
        return found

    return find


def reveal_service(
    db: Session, runtime: AuthRuntime, vault: Vault, *, recorder: ActivityRecorder | None = None
) -> StackVariableReveal:
    """Wire the reveal service over repository lookups, as one request would."""
    stacks = StackRepository(db, vault, runtime.clock)
    return StackVariableReveal(
        db=db,
        stacks=stacks,
        sessions=UserSessionRepository(db, runtime.clock),
        compose=repository_lookup(stacks, "compose"),
        swarm=repository_lookup(stacks, "swarm"),
        activity=recorder or ActivityRecorder(db, runtime.clock),
        clock=runtime.clock,
    )


async def add_stack(
    stacks: StackRepository,
    variables: list[VariableWrite],
    *,
    kind: StackKind = "compose",
    name: str = STACK_NAME,
    environment_id: int = ENV_A,
) -> tuple[int, dict[str, int]]:
    """Insert a stack with the variables and return its id and each variable's id by name."""
    stack_id = await stacks.insert(
        environment_id=environment_id,
        name=name,
        kind=kind,
        origin="created",
        project_directory=_directory_token(name, kind),
        compose_file=FILE,
        compose_file_digest=DIGEST,
        created_by_user_id=None,
    )
    await stacks.replace_variables(stack_id, variables)
    return stack_id, {v.name: v.id for v in await stacks.variables(stack_id)}


def _directory_token(name: str, kind: StackKind) -> str:
    return f"{kind}-{name}".encode().hex().ljust(32, "0")[:32]


async def activity_text(db: Session) -> str:
    """Return every activity row, message and detail included, as one text."""
    rows = await db.fetch_limited("SELECT * FROM activity_event", (), limit=10_000)
    return json.dumps([dict(row) for row in rows], default=str)


async def events_named(db: Session, action: str) -> list[dict[str, Any]]:
    """Return the activity rows with this action, oldest first, detail decoded."""
    return [event for event in await read_events(db) if event["action"] == action]
