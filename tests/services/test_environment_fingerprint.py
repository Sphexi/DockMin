"""The connection fingerprint names exactly the inputs that decide which host is reached."""

from dataclasses import replace
from typing import Any

import pytest

from app.crypto.vault import Vault
from app.db.engine import Session
from app.db.repositories.clocked import Clock
from app.db.repositories.environment import Environment, EnvironmentRepository
from app.db.repositories.environment_tls import EnvironmentTlsRepository, StoredTls
from app.docker_engine.registry import EngineClientRegistry
from app.services.assembly import EnvironmentServices
from app.services.environment_connector import (
    EnvironmentConnector,
    connection_fingerprint,
    digest_of,
)
from tests.access_support import ADMIN
from tests.docker_engine.tls_material import valid_material
from tests.environment_support import (
    REMOTE_ENDPOINT,
    SECOND_REMOTE_ENDPOINT,
    ScriptedProbes,
    remote_spec,
)

STAMP = "2026-01-02T03:04:05Z"


def an_environment() -> Environment:
    return Environment(
        id=1,
        name="remote",
        type="standalone",
        connection_kind="tcp_tls",
        socket_path=None,
        url=REMOTE_ENDPOINT,
        environment_group_id=None,
        status="reachable",
        failed_stage=None,
        status_message=None,
        last_checked_at=STAMP,
        last_contacted_at=STAMP,
        engine_version="29.2.1",
        api_version="1.51",
        swarm_state="inactive",
        swarm_control_available=False,
        compose_cli_version=None,
        engine_host_name=None,
        created_at=STAMP,
        updated_at=STAMP,
    )


def stored_tls() -> StoredTls:
    return StoredTls(1, "ca-pem", "certificate-pem", "key-ciphertext", STAMP)


BASELINE = connection_fingerprint(an_environment(), stored_tls())


@pytest.mark.parametrize(
    ("environment_changes", "tls_changes"),
    [
        ({"url": SECOND_REMOTE_ENDPOINT}, {}),
        (
            {"connection_kind": "unix_socket", "url": None, "socket_path": "/run/docker.sock"},
            {},
        ),
        ({}, {"ca_certificate": "another-ca-pem"}),
        ({}, {"client_certificate": "another-certificate-pem"}),
        ({}, {"client_key_ciphertext": "another-key-ciphertext"}),
    ],
    ids=["endpoint", "kind", "CA certificate", "client certificate", "client key"],
)
def test_each_connection_input_changes_the_fingerprint(
    environment_changes: dict[str, Any], tls_changes: dict[str, Any]
) -> None:
    changed = connection_fingerprint(
        replace(an_environment(), **environment_changes), replace(stored_tls(), **tls_changes)
    )

    assert changed != BASELINE


def test_the_fingerprint_of_an_environment_without_stored_material_differs_from_one_with_it() -> (
    None
):
    assert connection_fingerprint(an_environment(), None) != BASELINE


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "renamed"},
        {"status": "unreachable", "failed_stage": "tcp"},
        {"engine_version": "30.0.0", "api_version": "1.52"},
        {"environment_group_id": 7},
        {"swarm_state": "active", "type": "swarm"},
        {"last_checked_at": "2027-01-01T00:00:00Z", "updated_at": "2027-01-01T00:00:00Z"},
        {"compose_cli_version": "2.30.0"},
    ],
    ids=["name", "status", "versions", "group", "swarm", "timestamps", "compose version"],
)
def test_a_field_that_changes_without_the_connection_leaves_the_fingerprint_alone(
    changes: dict[str, Any],
) -> None:
    changed = connection_fingerprint(replace(an_environment(), **changes), stored_tls())

    assert changed == BASELINE


def test_the_fingerprint_does_not_read_the_updated_at_column() -> None:
    older = replace(an_environment(), updated_at="2000-01-01T00:00:00Z")
    newer = replace(an_environment(), updated_at="2099-01-01T00:00:00Z")

    assert connection_fingerprint(older, stored_tls()) == connection_fingerprint(
        newer, stored_tls()
    )


def test_parts_that_would_concatenate_to_the_same_text_still_hash_differently() -> None:
    first = digest_of("tcp_tls", "ab", "c", None, None)
    second = digest_of("tcp_tls", "a", "bc", None, None)

    assert first != second


class CountingTls(EnvironmentTlsRepository):
    """Counts how often the client key is decrypted."""

    reveals = 0

    async def reveal_client_key(self, environment_id: int) -> str | None:
        type(self).reveals += 1
        return await super().reveal_client_key(environment_id)


@pytest.fixture
def counting_tls(db: Session, clock: Clock, vault: Vault) -> CountingTls:
    CountingTls.reveals = 0
    return CountingTls(db, vault, clock)


@pytest.fixture
def connector(
    counting_tls: CountingTls, client_registry: EngineClientRegistry
) -> EnvironmentConnector:
    return EnvironmentConnector(
        tls=counting_tls,
        registry=client_registry,
        probes=ScriptedProbes().as_coordinator(),
    )


@pytest.fixture
async def saved(
    environments: EnvironmentServices, db: Session, clock: Clock, counting_tls: CountingTls
) -> tuple[Environment, StoredTls]:
    created = await environments.environments.create(ADMIN, remote_spec(valid_material()))
    environment = await EnvironmentRepository(db, clock).get(created.summary.id)
    stored = await counting_tls.get(created.summary.id)
    assert environment is not None and stored is not None
    CountingTls.reveals = 0
    return environment, stored


async def test_an_unchanged_environment_reuses_its_client_without_decrypting_the_key_again(
    connector: EnvironmentConnector, saved: tuple[Environment, StoredTls], clients_built: list[Any]
) -> None:
    environment, stored = saved

    first = await connector.client(environment, stored)
    second = await connector.client(environment, stored)

    assert second is first
    assert len(clients_built) == 1
    assert CountingTls.reveals == 1


async def test_an_endpoint_change_with_the_same_certificates_yields_a_new_client(
    connector: EnvironmentConnector, saved: tuple[Environment, StoredTls]
) -> None:
    environment, stored = saved
    first = await connector.client(environment, stored)

    moved = await connector.client(replace(environment, url=SECOND_REMOTE_ENDPOINT), stored)

    assert moved is not first
    assert first.closed


async def test_a_replaced_client_key_with_the_same_endpoint_yields_a_new_client(
    connector: EnvironmentConnector,
    saved: tuple[Environment, StoredTls],
    counting_tls: CountingTls,
) -> None:
    environment, stored = saved
    first = await connector.client(environment, stored)
    replacement = valid_material("ed25519")
    await counting_tls.replace(
        environment.id,
        ca_certificate=replacement.ca,
        client_certificate=replacement.cert,
        client_key=replacement.key,
    )
    replaced = await counting_tls.get(environment.id)
    assert replaced is not None

    rebuilt = await connector.client(environment, replaced)

    assert rebuilt is not first
    assert first.closed
