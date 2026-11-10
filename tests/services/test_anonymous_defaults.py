"""The default registry entries: their mark, and their grant to each environment at create."""

from typing import Any

from pydantic import SecretStr

from app.db.engine import Session
from app.db.repositories.registry_access import RegistryAccessRepository
from app.services.assembly import EnvironmentServices
from app.services.environments import ConnectionInput
from app.services.registry_input import RegistrySpec
from tests.access_support import ADMIN, ENV_A
from tests.environment_support import activity_rows, local_spec
from tests.registry_support import RegistryRig, raw_row, seed_default_entries
from tests.services.test_registries import new_spec

HUB_ADDRESS = "docker.io"
OTHER_ADDRESS = "mirror.example.com"
SECOND_PATH = "/run/second-docker.sock"


async def granted_to(db: Session, environment_id: int) -> list[int]:
    rows = await db.fetch_limited(
        "SELECT registry_credential_id FROM registry_environment_access "
        "WHERE environment_id = ? ORDER BY registry_credential_id",
        (environment_id,),
        limit=100,
    )
    return [int(row["registry_credential_id"]) for row in rows]


async def test_each_environment_created_is_granted_every_default_entry_and_names_them(
    environments: EnvironmentServices, db: Session
) -> None:
    entries = await seed_default_entries(db)

    first = await environments.environments.create(ADMIN, local_spec(name="first"))
    second = await environments.environments.create(
        ADMIN,
        local_spec(
            name="second",
            connection=ConnectionInput("unix_socket", SECOND_PATH),
            reviewed_endpoint=f"unix://{SECOND_PATH}",
        ),
    )

    assert await granted_to(db, first.summary.id) == entries
    assert await granted_to(db, second.summary.id) == entries
    created = [r for r in await activity_rows(db) if r["action"] == "environment_created"]
    assert [r["detail"]["registry_grants"] for r in created] == [",".join(map(str, entries))] * 2


async def test_a_removed_default_is_not_granted_to_the_next_environment(
    environments: EnvironmentServices, db: Session
) -> None:
    hub, ghcr = await seed_default_entries(db)
    await db.execute("DELETE FROM registry_credential WHERE id = ?", (hub,))

    created = await environments.environments.create(ADMIN, local_spec())

    assert await granted_to(db, created.summary.id) == [ghcr]


async def test_an_environment_created_with_no_default_entries_records_no_grant_detail(
    environments: EnvironmentServices, db: Session
) -> None:
    created = await environments.environments.create(ADMIN, local_spec())

    [row] = [r for r in await activity_rows(db) if r["action"] == "environment_created"]
    assert await granted_to(db, created.summary.id) == []
    assert "registry_grants" not in row["detail"]


async def test_an_entry_without_the_mark_is_never_granted_at_create(
    environments: EnvironmentServices, db: Session
) -> None:
    hub, _ = await seed_default_entries(db)
    await db.execute("UPDATE registry_credential SET seeded_default = 0 WHERE id = ?", (hub,))

    created = await environments.environments.create(ADMIN, local_spec())

    assert hub not in await granted_to(db, created.summary.id)


async def test_a_default_turned_to_authentication_on_loses_its_mark_and_is_never_granted(
    registry_rig: RegistryRig,
) -> None:
    hub, ghcr = await seed_default_entries(registry_rig.db)
    access = RegistryAccessRepository(registry_rig.db)

    await registry_rig.services.credentials.update(
        ADMIN,
        hub,
        new_spec(
            "Docker Hub (anonymous)",
            provider="dockerhub",
            address=HUB_ADDRESS,
            username="account",
            secret=SecretStr("token"),
        ),
    )

    assert (await raw_row(registry_rig.db, hub))["seeded_default"] == 0
    assert (await raw_row(registry_rig.db, ghcr))["seeded_default"] == 1
    assert await access.grant_default_entries(ENV_A) == [ghcr]


def anonymous_hub_spec(**overrides: Any) -> RegistrySpec:
    values: dict[str, Any] = {
        "provider": "dockerhub",
        "address": HUB_ADDRESS,
        "authentication_enabled": False,
        "username": "",
        "secret": SecretStr(""),
        "name": "Docker Hub (anonymous)",
    }
    return new_spec(**{**values, **overrides})


async def test_the_mark_survives_a_rename(registry_rig: RegistryRig) -> None:
    hub, _ = await seed_default_entries(registry_rig.db)

    await registry_rig.services.credentials.update(ADMIN, hub, anonymous_hub_spec(name="Hub"))

    assert (await raw_row(registry_rig.db, hub))["seeded_default"] == 1


async def test_a_new_provider_and_address_remove_the_mark(registry_rig: RegistryRig) -> None:
    hub, _ = await seed_default_entries(registry_rig.db)

    detail = await registry_rig.services.credentials.update(
        ADMIN, hub, anonymous_hub_spec(provider="custom", address=OTHER_ADDRESS)
    )

    assert detail.seeded_default is False
    assert (await raw_row(registry_rig.db, hub))["seeded_default"] == 0


async def test_the_list_and_the_detail_report_the_mark(registry_rig: RegistryRig) -> None:
    hub, _ = await seed_default_entries(registry_rig.db)
    await registry_rig.services.credentials.create(ADMIN, new_spec("mine", address=OTHER_ADDRESS))

    page = await registry_rig.services.credentials.page(ADMIN)
    detail = await registry_rig.services.credentials.detail(ADMIN, hub)

    assert {row.name: row.seeded_default for row in page.items} == {
        "Docker Hub (anonymous)": True,
        "GitHub Container Registry (anonymous)": True,
        "mine": False,
    }
    assert detail.seeded_default is True
