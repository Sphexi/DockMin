"""Turning probe results into stored status, and stored environments into connection settings."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import EnvironmentRepository
from app.db.repositories.environment_tls import EnvironmentTlsRepository
from app.docker_engine.probe import ProbeResult, ProbeStatus, Stage
from app.docker_engine.registry import EngineClientRegistry
from app.docker_engine.transport import UnixSocketSettings
from app.services import environment_connector
from app.services.assembly import EnvironmentServices
from app.services.environment_connector import (
    MATERIAL_UNAVAILABLE,
    MAX_STATUS_MESSAGE,
    EnvironmentConnector,
    ProbeCoordinator,
    status_record_of,
)
from tests.access_support import ADMIN, STAMP
from tests.crypto.helpers import SECOND_KEY, vault_with
from tests.docker_engine.tls_material import valid_material
from tests.environment_support import (
    FACTS,
    REMOTE_ENDPOINT,
    ScriptedProbes,
    failed_result,
    reachable_result,
    remote_spec,
    seed_environment,
)

ENVIRONMENT_ID = 1
FINGERPRINT = "fingerprint"
SETTINGS = UnixSocketSettings("/run/probe.sock")


def test_a_reachable_result_is_stored_as_contact_with_the_versions_the_engine_reported() -> None:
    record = status_record_of(reachable_result(), STAMP)

    assert (record.status, record.failed_stage, record.message) == ("reachable", None, None)
    assert (record.checked_at, record.contacted_at) == (STAMP, STAMP)
    assert (record.engine_version, record.api_version) == (FACTS.engine_version, FACTS.api_version)
    assert (record.type, record.swarm_state, record.swarm_control_available) == (
        "standalone",
        "inactive",
        False,
    )


@pytest.mark.parametrize("stage", list(Stage))
def test_a_failed_stage_is_stored_under_its_own_name_without_recording_contact(
    stage: Stage,
) -> None:
    record = status_record_of(failed_result(stage), STAMP)

    assert (record.status, record.failed_stage) == ("unreachable", stage.value)
    assert record.contacted_at is None


def test_an_unsupported_version_is_stored_with_its_own_status() -> None:
    result = failed_result(Stage.ENGINE, "Upgrade Docker.", ProbeStatus.VERSION_UNSUPPORTED)

    record = status_record_of(result, STAMP)

    assert (record.status, record.failed_stage) == ("version_unsupported", "engine")


def test_a_failure_message_is_cut_and_stripped_of_control_characters_before_it_is_stored() -> None:
    result = failed_result(Stage.TCP, "bad\x00‮" + "x" * 1000)

    record = status_record_of(result, STAMP)

    assert record.message is not None
    assert len(record.message) <= MAX_STATUS_MESSAGE
    assert record.message.startswith("badx")


@dataclass(slots=True)
class Runs:
    """Counts the underlying connection tests and holds them until released."""

    started: int = 0
    release: asyncio.Event = field(default_factory=asyncio.Event)
    crash: bool = False

    async def __call__(self, settings: Any, **options: Any) -> ProbeResult:
        self.started += 1
        await self.release.wait()
        if self.crash:
            raise RuntimeError("secret internal detail")
        return reachable_result()


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> Runs:
    scripted = Runs()
    monkeypatch.setattr(environment_connector, "probe_connection", scripted)
    return scripted


async def test_concurrent_tests_of_one_saved_connection_share_a_single_run(runs: Runs) -> None:
    coordinator = ProbeCoordinator()
    waiting = [
        asyncio.create_task(
            coordinator.probe(SETTINGS, environment_id=ENVIRONMENT_ID, fingerprint=FINGERPRINT)
        )
        for _ in range(3)
    ]
    await asyncio.sleep(0)

    runs.release.set()
    results = await asyncio.gather(*waiting)

    assert runs.started == 1
    assert results[0] is results[1] is results[2]
    await coordinator.aclose()


async def test_a_test_of_an_unsaved_connection_is_never_shared(runs: Runs) -> None:
    coordinator = ProbeCoordinator()
    waiting = [
        asyncio.create_task(
            coordinator.probe(SETTINGS, environment_id=ENVIRONMENT_ID, fingerprint=None)
        )
        for _ in range(2)
    ]
    await asyncio.sleep(0)

    runs.release.set()
    await asyncio.gather(*waiting)

    assert runs.started == 2
    await coordinator.aclose()


async def test_a_test_that_crashes_reports_an_unreachable_engine_without_the_error_text(
    runs: Runs,
) -> None:
    coordinator = ProbeCoordinator()
    runs.crash = True
    runs.release.set()

    result = await coordinator.probe(SETTINGS, environment_id=ENVIRONMENT_ID, fingerprint=None)

    assert (result.status, result.failed_stage) == (ProbeStatus.UNREACHABLE, Stage.ENGINE)
    assert "secret" not in result.message
    await coordinator.aclose()


async def test_closing_the_coordinator_cancels_running_tests_and_refuses_new_ones(
    runs: Runs,
) -> None:
    coordinator = ProbeCoordinator()
    running = asyncio.create_task(
        coordinator.probe(SETTINGS, environment_id=ENVIRONMENT_ID, fingerprint=FINGERPRINT)
    )
    await asyncio.sleep(0)

    await coordinator.aclose()

    assert running.cancelled()
    with pytest.raises(RuntimeError, match="closed"):
        await coordinator.probe(SETTINGS, environment_id=ENVIRONMENT_ID, fingerprint=None)


@pytest.fixture
def connector(
    db: Session, clock: Clock, vault: Vault, client_registry: EngineClientRegistry
) -> EnvironmentConnector:
    return EnvironmentConnector(
        tls=EnvironmentTlsRepository(db, vault, clock),
        registry=client_registry,
        probes=ScriptedProbes().as_coordinator(),
    )


async def test_a_stored_environment_whose_key_cannot_be_opened_is_reported_not_raised(
    environments: EnvironmentServices,
    client_registry: EngineClientRegistry,
    db: Session,
    clock: Clock,
) -> None:
    created = await environments.environments.create(ADMIN, remote_spec(valid_material()))
    environment = await EnvironmentRepository(db, clock).get(created.summary.id)
    other_vault = EnvironmentTlsRepository(db, vault_with(SECOND_KEY), clock)
    stored = await other_vault.get(created.summary.id)
    assert environment is not None and stored is not None
    broken = EnvironmentConnector(
        tls=other_vault, registry=client_registry, probes=ScriptedProbes().as_coordinator()
    )

    result = await broken.test_stored(environment, stored)

    assert (result.failed_stage, result.message) == (Stage.TLS_HANDSHAKE, MATERIAL_UNAVAILABLE)


async def test_a_remote_environment_with_no_stored_material_is_reported_not_raised(
    db: Session, clock: Clock, connector: EnvironmentConnector
) -> None:
    remote = await seed_environment(db, clock, "bare", kind="tcp_tls", endpoint=REMOTE_ENDPOINT)

    result = await connector.test_stored(remote, None)

    assert (result.failed_stage, result.message) == (Stage.TLS_HANDSHAKE, MATERIAL_UNAVAILABLE)
