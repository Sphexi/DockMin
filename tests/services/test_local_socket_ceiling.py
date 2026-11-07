"""A standard user's access on the local-socket environment never exceeds read."""

import pytest

from app.auth.errors import PermissionDenied
from app.db.engine import Session
from app.services.access import Reason, ResourceKey
from app.services.assembly import AccessServices
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    STANDARD,
    FakeInspector,
    ListedObject,
    container_key,
    control,
    volume_key,
)
from tests.auth_support import STANDARD_ID

ITEM_COLUMN = "item.name"
ENVIRONMENT_COLUMN = "item.host"
VOLUME = "data"


async def public_write(
    access: AccessServices, inspector: FakeInspector, environment_id: int
) -> ResourceKey:
    key = container_key(1, environment_id)
    inspector.add(key)
    await control(access, key, public=True, level="write")
    return key


async def test_a_write_grant_on_a_local_socket_container_is_held_to_read_with_the_reason(
    access: AccessServices, inspector: FakeInspector
) -> None:
    key = await public_write(access, inspector, ENV_B)
    visibility = await access.authoriser.resolve_visibility(
        STANDARD, ENV_B, "container", {key.resource_id}
    )

    decision = visibility.decide(key.resource_id, {})

    assert decision.level == "read"
    assert Reason.LOCAL_SOCKET_READ_ONLY in decision.reasons
    assert visibility.permits(key.resource_id, {}, "read")
    assert not visibility.permits(key.resource_id, {}, "write")


async def test_the_same_grant_on_a_remote_environment_keeps_write_without_the_reason(
    access: AccessServices, inspector: FakeInspector
) -> None:
    key = await public_write(access, inspector, ENV_A)
    visibility = await access.authoriser.resolve_visibility(
        STANDARD, ENV_A, "container", {key.resource_id}
    )

    decision = visibility.decide(key.resource_id, {})

    assert decision.level == "write"
    assert Reason.LOCAL_SOCKET_READ_ONLY not in decision.reasons


async def test_requiring_write_on_a_local_socket_container_is_forbidden_not_hidden(
    access: AccessServices, inspector: FakeInspector
) -> None:
    key = await public_write(access, inspector, ENV_B)

    with pytest.raises(PermissionDenied):
        await access.authoriser.require_access(STANDARD, key, "write")


async def test_an_administrator_keeps_write_on_the_local_socket(
    access: AccessServices, inspector: FakeInspector
) -> None:
    key = await public_write(access, inspector, ENV_B)

    await access.authoriser.require_access(ADMIN, key, "write")


async def test_filtering_for_write_on_the_local_socket_admits_nothing_and_for_read_admits_all(
    access: AccessServices, inspector: FakeInspector
) -> None:
    key = await public_write(access, inspector, ENV_B)
    listed = [ListedObject.of(key)]

    writable = await access.authoriser.filter_visible(STANDARD, listed, "write")
    readable = await access.authoriser.filter_visible(STANDARD, listed, "read")

    assert (writable, readable) == ([], listed)


@pytest.mark.parametrize(
    ("environment_id", "expected"), [(ENV_A, {VOLUME}), (ENV_B, set())], ids=["remote", "local"]
)
async def test_the_sql_filter_for_write_admits_a_write_control_only_on_a_remote_environment(
    access: AccessServices, db: Session, environment_id: int, expected: set[str]
) -> None:
    await control(access, volume_key(VOLUME, environment_id), public=True, level="write")
    await db.execute("CREATE TABLE item (host INTEGER NOT NULL, name TEXT NOT NULL)")
    await db.execute("INSERT INTO item VALUES (?, ?)", (environment_id, VOLUME))
    visibility = await access.authoriser.resolve_visibility(STANDARD, environment_id, "volume")
    fragment = visibility.sql_filter(ITEM_COLUMN, ENVIRONMENT_COLUMN, "write")

    rows = await db.fetch_limited(
        f"SELECT name FROM item WHERE {fragment.clause}", fragment.params, limit=10
    )

    assert {row["name"] for row in rows} == expected


async def test_effective_access_reports_the_ceiling_as_the_reason_for_read_only(
    access: AccessServices, inspector: FakeInspector
) -> None:
    key = await public_write(access, inspector, ENV_B)

    result = await access.effective.explain(
        ADMIN, STANDARD_ID, ENV_B, "container", [ListedObject.of(key)]
    )

    entry = result.entries[0]
    assert entry.level == "read"
    assert Reason.LOCAL_SOCKET_READ_ONLY in entry.reasons
