"""Registry credential sets: scripted probes and exchange, a wired service rig and seed helpers."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from app.auth.services import AuthRuntime
from app.crypto.vault import Vault
from app.db.engine import Database, Session
from app.db.repositories.registry_credential import (
    CredentialFields,
    RegistryCredentialRepository,
    StoredRegistryCredential,
)
from app.netcheck import StageOutcome
from app.registry_client.auth_config import basic_token
from app.registry_client.ecr import DerivationRequest, DerivedSecret
from app.registry_client.errors import TokenExchangeFailed, TokenExchangeKind
from app.registry_client.probe import (
    RegistryLogin,
    RegistryProbeResult,
    RegistryStage,
    RegistryStageResult,
    RegistryTarget,
)
from app.services.activity import ActivityRecorder
from app.services.assembly import RegistryServices, build_registry_services
from app.services.registry_resolution import DerivedSecretCache
from app.services.registry_setup import RegistryRuntime
from tests.auth_support import FakeClock

ADDRESS = "registry.example.com"
OTHER_ADDRESS = "mirror.example.com:5000"
ECR_ADDRESS = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"
ECR_REGION = "eu-west-2"
ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
USERNAME = "service-account"
SECRET = "stored-secret-marker-4417"
SECRET_ROTATED = "rotated-secret-marker-9921"
ISSUED_USERNAME = "AWS"
ISSUED_PASSWORD = "issued-ecr-password-marker-6612"
STAMP = "2026-01-02T03:04:05Z"


def passing_result() -> RegistryProbeResult:
    stage = RegistryStageResult(RegistryStage.DNS, StageOutcome.PASSED, 1, "The name resolved.")
    return RegistryProbeResult((stage,), True, None, "The registry is reachable.", 3)


def failing_result(stage: RegistryStage = RegistryStage.TCP) -> RegistryProbeResult:
    failed = RegistryStageResult(stage, StageOutcome.FAILED, 1, "The connection was refused.")
    return RegistryProbeResult((failed,), False, stage, "The connection was refused.", 3)


@dataclass(frozen=True, slots=True)
class ProbeCall:
    target: RegistryTarget
    login: RegistryLogin | None
    key: tuple[int, str] | None


@dataclass(slots=True)
class ScriptedRegistryProbes:
    """Stands in for the connection-test coordinator; nothing leaves the process."""

    result: RegistryProbeResult = field(default_factory=passing_result)
    calls: list[ProbeCall] = field(default_factory=list)

    async def probe(
        self, target: RegistryTarget, login: RegistryLogin | None, *, key: tuple[int, str] | None
    ) -> RegistryProbeResult:
        self.calls.append(ProbeCall(target, login, key))
        return self.result

    async def aclose(self) -> None:
        """Nothing is held."""


def issued_token(username: str = ISSUED_USERNAME, password: str = ISSUED_PASSWORD) -> SecretStr:
    return basic_token(username, SecretStr(password))


@dataclass(slots=True)
class ScriptedDeriver:
    """A deriver whose issued token and expiry the test sets; it counts and records requests."""

    clock: Callable[[], datetime]
    lifetime: timedelta = timedelta(hours=12)
    password: str = ISSUED_PASSWORD
    failure: Exception | None = None
    requests: list[DerivationRequest] = field(default_factory=list)
    gate: asyncio.Event | None = None
    before_return: Callable[[], Awaitable[None]] | None = None
    issued: int = 0

    async def derive(self, request: DerivationRequest) -> DerivedSecret:
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.failure is not None:
            raise self.failure
        self.issued += 1
        if self.before_return is not None:
            await self.before_return()
        return DerivedSecret(
            issued_token(password=f"{self.password}-{self.issued}"),
            self.clock() + self.lifetime,
        )

    def fail_with(self, kind: TokenExchangeKind) -> None:
        self.failure = TokenExchangeFailed(kind, "The exchange failed.")


@dataclass(slots=True)
class RegistryRig:
    """The registry services over a private database, with what a test reaches into."""

    services: RegistryServices
    database: Database
    db: Session
    clock: FakeClock
    vault: Vault
    runtime: AuthRuntime
    probes: ScriptedRegistryProbes
    deriver: ScriptedDeriver
    cache: DerivedSecretCache
    registries: RegistryRuntime
    credentials: RegistryCredentialRepository
    http: httpx.AsyncClient


async def refuse_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError("the registry rig made a network request")


def build_rig(
    *,
    database: Database,
    db: Session,
    runtime: AuthRuntime,
    clock: FakeClock,
    vault: Vault,
    **cache_options: Any,
) -> RegistryRig:
    """Wire the services, the resolver and the derived-secret cache over scripted fakes."""
    probes = ScriptedRegistryProbes()
    deriver = ScriptedDeriver(clock)
    derivers = {"ecr": deriver}
    cache = DerivedSecretCache(
        database=database, vault=vault, derivers=derivers, clock=clock, **cache_options
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse_network))
    registries = RegistryRuntime(
        vault=vault,
        derivers=derivers,
        cache=cache,
        probes=probes,  # type: ignore[arg-type]
        http=http,
    )
    services = build_registry_services(db, runtime, registries, ActivityRecorder(db, clock))
    return RegistryRig(
        services=services,
        database=database,
        db=db,
        clock=clock,
        vault=vault,
        runtime=runtime,
        probes=probes,
        deriver=deriver,
        cache=cache,
        registries=registries,
        credentials=RegistryCredentialRepository(db, vault, clock),
        http=http,
    )


def static_fields(name: str = "registry", **overrides: Any) -> CredentialFields:
    values: dict[str, Any] = {
        "name": name,
        "provider": "custom",
        "url": ADDRESS,
        "authentication_enabled": True,
        "username": USERNAME,
        "extras": {},
    }
    return CredentialFields(**{**values, **overrides})


def ecr_fields(name: str = "ecr", **overrides: Any) -> CredentialFields:
    values: dict[str, Any] = {
        "name": name,
        "provider": "ecr",
        "url": ECR_ADDRESS,
        "authentication_enabled": True,
        "username": ACCESS_KEY,
        "extras": {"region": ECR_REGION},
    }
    return CredentialFields(**{**values, **overrides})


async def add_static(
    rig: RegistryRig, name: str = "registry", secret: str = SECRET, **overrides: Any
) -> StoredRegistryCredential:
    return await rig.credentials.create(static_fields(name, **overrides), SecretStr(secret))


async def add_anonymous(
    rig: RegistryRig, name: str = "anonymous", **overrides: Any
) -> StoredRegistryCredential:
    fields = static_fields(name, authentication_enabled=False, username=None, **overrides)
    return await rig.credentials.create(fields, None)


async def add_ecr(
    rig: RegistryRig, name: str = "ecr", secret: str = SECRET, **overrides: Any
) -> StoredRegistryCredential:
    return await rig.credentials.create(ecr_fields(name, **overrides), SecretStr(secret))


async def grant_environment(db: Session, credential_id: int, environment_id: int) -> None:
    await db.execute(
        "INSERT INTO registry_environment_access (registry_credential_id, environment_id) "
        "VALUES (?, ?)",
        (credential_id, environment_id),
    )


async def grant_user(db: Session, credential_id: int, user_id: int) -> None:
    await db.execute(
        "INSERT INTO registry_credential_user_access (registry_credential_id, user_id) "
        "VALUES (?, ?)",
        (credential_id, user_id),
    )


async def grant_team(db: Session, credential_id: int, team_id: int) -> None:
    await db.execute(
        "INSERT INTO registry_credential_team_access (registry_credential_id, team_id) "
        "VALUES (?, ?)",
        (credential_id, team_id),
    )


BOUND_REFERENCE = "registry.example.com/app:1"


async def bind_image(
    db: Session,
    credential_id: int | None,
    environment_id: int,
    reference: str = BOUND_REFERENCE,
) -> None:
    await db.execute(
        "INSERT INTO image_credential_binding (environment_id, image_reference, "
        "registry_credential_id, state, bound_at, updated_at) "
        "VALUES (?, ?, ?, 'bound', ?, ?)",
        (environment_id, reference, credential_id, STAMP, STAMP),
    )


async def raw_row(db: Session, credential_id: int) -> Mapping[str, Any]:
    row = await db.fetch_one("SELECT * FROM registry_credential WHERE id = ?", (credential_id,))
    assert row is not None
    return dict(row)


async def activity(db: Session) -> list[dict[str, Any]]:
    rows = await db.fetch_limited(
        "SELECT action, result, actor_type, actor_user_id, target_type, target_id, "
        "environment_id, detail FROM activity_event ORDER BY id",
        (),
        limit=1000,
    )
    return [{**dict(row), "detail": json.loads(row["detail"] or "{}")} for row in rows]


async def make_team(db: Session, name: str, *user_ids: int) -> int:
    """Create a team holding the given users and return its id."""
    team = await db.execute("INSERT INTO team (name, created_at) VALUES (?, ?)", (name, STAMP))
    assert team.last_row_id is not None
    for user_id in user_ids:
        await db.execute(
            "INSERT INTO team_membership (team_id, user_id, created_at) VALUES (?, ?, ?)",
            (team.last_row_id, user_id, STAMP),
        )
    return team.last_row_id
