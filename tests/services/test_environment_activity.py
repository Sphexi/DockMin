"""Every environment, group, tag and grant mutation leaves one event naming the acting user."""

from collections.abc import Awaitable, Callable

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.db.engine import Session
from app.services.assembly import EnvironmentServices
from tests.access_support import ADMIN, ENV_A, STANDARD
from tests.auth_support import ADMIN_ID, STANDARD_ID
from tests.docker_engine.tls_material import valid_material
from tests.environment_support import (
    activity_rows,
    local_spec,
    remote_spec,
)

MUTATING_ACTIONS = [
    "environment_group_created",
    "tag_created",
    "environment_created",
    "environment_updated",
    "environment_connection_tested",
    "environment_tags_set",
    "environment_group_assigned",
    "environment_access_granted",
    "environment_access_revoked",
    "environment_group_updated",
    "environment_group_access_granted",
    "environment_group_access_revoked",
    "tag_deleted",
    "environment_group_deleted",
    "environment_deleted",
]
ABSENT_ID = 9999


async def test_each_mutation_writes_exactly_one_success_event_with_the_acting_user(
    environments: EnvironmentServices, db: Session
) -> None:
    group = await environments.groups.create(ADMIN, name="production")
    tag = await environments.tags.create(ADMIN, name="blue")
    created = await environments.environments.create(ADMIN, local_spec(name="edge"))
    host = created.summary.id
    await environments.environments.update(ADMIN, host, local_spec(name="edge-renamed"))
    await environments.environments.test_connection(ADMIN, host)
    await environments.environments.set_tags(ADMIN, host, [tag.id])
    await environments.environments.assign_group(ADMIN, host, group.id)
    await environments.environments.grant(ADMIN, host, user_id=STANDARD_ID)
    await environments.environments.revoke(ADMIN, host, user_id=STANDARD_ID)
    await environments.groups.update(ADMIN, group.id, name="live", description=None)
    await environments.groups.grant(ADMIN, group.id, user_id=STANDARD_ID)
    await environments.groups.revoke(ADMIN, group.id, user_id=STANDARD_ID)
    await environments.tags.delete(ADMIN, tag.id, expected_name="blue")
    await environments.groups.delete(ADMIN, group.id, expected_name="live")
    await environments.environments.delete(ADMIN, host, expected_name="edge-renamed")

    rows = await activity_rows(db)

    assert [row["action"] for row in rows] == MUTATING_ACTIONS
    assert {row["result"] for row in rows} == {"success"}
    assert {row["actor_user_id"] for row in rows} == {ADMIN_ID}
    assert {row["actor_type"] for row in rows} == {"user"}


Attempt = Callable[[EnvironmentServices], Awaitable[object]]
ATTEMPTS: dict[str, Attempt] = {
    "create": lambda s: s.environments.create(STANDARD, local_spec()),
    "update": lambda s: s.environments.update(STANDARD, ENV_A, local_spec()),
    "test": lambda s: s.environments.test_connection(STANDARD, ENV_A),
    "delete": lambda s: s.environments.delete(STANDARD, ENV_A, expected_name="environment-a"),
    "set tags": lambda s: s.environments.set_tags(STANDARD, ENV_A, []),
    "assign group": lambda s: s.environments.assign_group(STANDARD, ENV_A, None),
    "grant": lambda s: s.environments.grant(STANDARD, ENV_A, user_id=STANDARD_ID),
    "revoke": lambda s: s.environments.revoke(STANDARD, ENV_A, user_id=STANDARD_ID),
    "group create": lambda s: s.groups.create(STANDARD, name="x"),
    "group update": lambda s: s.groups.update(STANDARD, 1, name="x", description=None),
    "group delete": lambda s: s.groups.delete(STANDARD, 1, expected_name="x"),
    "group grant": lambda s: s.groups.grant(STANDARD, 1, user_id=STANDARD_ID),
    "group revoke": lambda s: s.groups.revoke(STANDARD, 1, user_id=STANDARD_ID),
    "tag create": lambda s: s.tags.create(STANDARD, name="x"),
    "tag delete": lambda s: s.tags.delete(STANDARD, 1, expected_name="x"),
}


@pytest.mark.parametrize("attempt", ATTEMPTS.values(), ids=ATTEMPTS.keys())
async def test_a_refusal_for_lack_of_the_administrator_role_writes_no_event(
    environments: EnvironmentServices, db: Session, attempt: Attempt
) -> None:
    with pytest.raises(PermissionDenied):
        await attempt(environments)

    assert await activity_rows(db) == []


async def test_an_operation_on_an_environment_the_caller_cannot_see_writes_no_event(
    environments: EnvironmentServices, db: Session
) -> None:
    with pytest.raises(ResourceNotFound):
        await environments.environments.test_connection(STANDARD, ABSENT_ID)
    with pytest.raises(ResourceNotFound):
        await environments.environments.delete(ADMIN, ABSENT_ID, expected_name="x")

    assert await activity_rows(db) == []


async def test_no_event_detail_carries_certificate_or_key_text(
    environments: EnvironmentServices, db: Session
) -> None:
    first, second = valid_material(), valid_material("ed25519")
    created = await environments.environments.create(ADMIN, remote_spec(first))
    await environments.environments.update(
        ADMIN, created.summary.id, remote_spec(second, host="other.example.test")
    )
    await environments.environments.test_connection(ADMIN, created.summary.id)
    await environments.environments.delete(ADMIN, created.summary.id, expected_name="edge")

    details = " ".join(str(row["detail"]) for row in await activity_rows(db))

    assert len(await activity_rows(db)) == 4
    assert "BEGIN" not in details
    uploaded = [*first.key.splitlines()[1:-1], *first.cert.splitlines()[1:-1]]
    uploaded += [*second.key.splitlines()[1:-1], *second.cert.splitlines()[1:-1]]
    assert not any(line in details for line in uploaded)
