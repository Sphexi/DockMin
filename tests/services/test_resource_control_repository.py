"""The control queries: standing scans, stack-name folding and the SQL visibility filter."""

import pytest

from app.db.engine import Session
from app.db.repositories.errors import DuplicateError
from app.db.repositories.resource_control import _SCAN_PAGE as SCAN_PAGE
from app.db.repositories.resource_control import AccessLevel, ResourceControlRepository
from app.db.repositories.visibility_filter import SqlFragment
from app.services.access import ResourceKey, ResourceType
from app.services.assembly import AccessServices
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    MEMBER,
    MEMBER_ID,
    PEER_ID,
    STANDARD,
    TEAM_NAME,
    bulk_controls,
    container_id,
    container_key,
    control,
    plant,
    stack_key,
    team_of,
    volume_key,
)
from tests.auth_support import STANDARD_ID

# Qualified, because an unqualified name shared with resource_control binds to the inner table.
ITEM_COLUMN = "item.name"
ENVIRONMENT_COLUMN = "item.host"
CONTAINER_FORMAT = "%064x"


async def stock(db: Session, *rows: tuple[int, str]) -> None:
    """Create a table of (environment, name) rows for a filter to be applied to."""
    await db.execute("CREATE TABLE item (host INTEGER NOT NULL, name TEXT NOT NULL)")
    for environment_id, name in rows:
        await db.execute("INSERT INTO item VALUES (?, ?)", (environment_id, name))


async def admitted(db: Session, fragment: SqlFragment) -> set[tuple[int, str]]:
    """Return the (environment, name) rows a filter lets through."""
    rows = await db.fetch_limited(
        f"SELECT host, name FROM item WHERE {fragment.clause}",
        fragment.params,
        limit=1000,
    )
    return {(int(row["host"]), str(row["name"])) for row in rows}


async def filter_for(
    access: AccessServices,
    environment_id: int,
    resource_type: ResourceType,
    required: AccessLevel = "read",
) -> SqlFragment:
    """Build STANDARD's filter for one environment and type through the authoriser."""
    visibility = await access.authoriser.resolve_visibility(STANDARD, environment_id, resource_type)
    return visibility.sql_filter(ITEM_COLUMN, ENVIRONMENT_COLUMN, required)


async def test_a_stack_control_is_found_under_any_case_of_its_name(
    controls: ResourceControlRepository,
) -> None:
    await plant(controls, stack_key("WebApp"))

    found = await controls.get_for(ENV_A, "stack", "WEBAPP")

    assert found is not None
    assert found.resource_id == "WebApp"


async def test_a_stack_control_is_deleted_under_any_case_of_its_name(
    controls: ResourceControlRepository,
) -> None:
    await plant(controls, stack_key("WebApp"))

    assert await controls.delete_for(ENV_A, "stack", "webapp")
    assert await controls.get_for(ENV_A, "stack", "WebApp") is None


async def test_other_object_types_are_matched_case_sensitively(
    controls: ResourceControlRepository,
) -> None:
    await plant(controls, volume_key("Data"), public=True)

    assert await controls.get_for(ENV_A, "volume", "data") is None


async def test_two_stack_names_that_differ_only_in_case_cannot_both_be_controlled(
    controls: ResourceControlRepository,
) -> None:
    await plant(controls, stack_key("WebApp"))

    with pytest.raises(DuplicateError):
        await plant(controls, stack_key("webapp"))


async def test_the_same_stack_name_can_be_controlled_in_two_environments(
    controls: ResourceControlRepository,
) -> None:
    await plant(controls, stack_key("webapp", ENV_A))

    assert await plant(controls, stack_key("webapp", ENV_B)) > 0


async def test_a_scan_reports_the_reason_the_user_holds_each_control(
    access: AccessServices, controls: ResourceControlRepository
) -> None:
    team_id = await team_of(access, TEAM_NAME, STANDARD)
    owned, granted, via_team, public, refused = (container_key(n) for n in range(1, 6))
    await control(access, owned, owner=STANDARD_ID)
    await control(access, granted, users=(STANDARD_ID,))
    await control(access, via_team, teams=(team_id,))
    await control(access, public, public=True)
    await control(access, refused, administrators_only=True)

    scan = {
        row.resource_id: row
        for row in await controls.list_standing(STANDARD_ID, ENV_A, "container")
    }

    assert scan[owned.resource_id].is_owner
    assert scan[granted.resource_id].user_granted
    assert scan[via_team.resource_id].team_granted
    assert scan[public.resource_id].is_public
    assert scan[refused.resource_id].is_administrators_only
    assert not scan[granted.resource_id].team_granted
    assert not scan[via_team.resource_id].user_granted


async def test_a_scan_stops_reporting_a_team_grant_once_membership_is_removed(
    access: AccessServices, controls: ResourceControlRepository
) -> None:
    team_id = await team_of(access, TEAM_NAME, MEMBER)
    key = container_key(1)
    await control(access, key, teams=(team_id,))
    await access.teams.remove_member(ADMIN, team_id, user_id=MEMBER_ID)

    scan = await controls.list_standing(MEMBER_ID, ENV_A, "container")

    assert [row.team_granted for row in scan] == [False]


async def test_a_scan_ignores_controls_in_other_environments(
    access: AccessServices, controls: ResourceControlRepository
) -> None:
    await control(access, container_key(1, ENV_B), public=True)

    assert await controls.list_standing(STANDARD_ID, ENV_A, "container") == []


async def test_a_narrowed_scan_returns_only_the_requested_objects(
    access: AccessServices, controls: ResourceControlRepository
) -> None:
    for number in (1, 2, 3):
        await control(access, container_key(number), public=True)

    scan = await controls.list_standing(STANDARD_ID, ENV_A, "container", {container_id(2)})

    assert [row.resource_id for row in scan] == [container_id(2)]


async def test_a_narrowed_scan_omits_a_requested_object_that_has_no_control(
    access: AccessServices, controls: ResourceControlRepository
) -> None:
    await control(access, container_key(1), public=True)

    scan = await controls.list_standing(
        STANDARD_ID, ENV_A, "container", {container_id(1), container_id(2)}
    )

    assert [row.resource_id for row in scan] == [container_id(1)]


async def test_a_scan_of_stacks_ignores_narrowing_and_returns_every_stack(
    access: AccessServices, controls: ResourceControlRepository
) -> None:
    for name in ("alpha", "beta"):
        await control(access, stack_key(name), public=True)

    scan = await controls.list_standing(STANDARD_ID, ENV_A, "stack", {"alpha"})

    assert sorted(row.resource_id for row in scan) == ["alpha", "beta"]


async def test_a_scan_returns_every_control_when_there_are_more_than_one_page(
    db: Session, controls: ResourceControlRepository
) -> None:
    await bulk_controls(db, ENV_A, "container", CONTAINER_FORMAT, SCAN_PAGE + 1)

    scan = await controls.list_standing(STANDARD_ID, ENV_A, "container")

    assert len(scan) == SCAN_PAGE + 1
    assert scan[-1].resource_id == CONTAINER_FORMAT % (SCAN_PAGE + 1)


async def test_a_scan_of_exactly_one_page_returns_that_page_and_ends(
    db: Session, controls: ResourceControlRepository
) -> None:
    await bulk_controls(db, ENV_A, "container", CONTAINER_FORMAT, SCAN_PAGE)

    scan = await controls.list_standing(STANDARD_ID, ENV_A, "container")

    assert len(scan) == SCAN_PAGE


async def test_a_narrowed_scan_of_more_than_one_page_of_objects_returns_them_all(
    db: Session, controls: ResourceControlRepository
) -> None:
    await bulk_controls(db, ENV_A, "container", CONTAINER_FORMAT, SCAN_PAGE + 1)
    wanted = {CONTAINER_FORMAT % number for number in range(1, SCAN_PAGE + 2)}

    scan = await controls.list_standing(STANDARD_ID, ENV_A, "container", wanted)

    assert {row.resource_id for row in scan} == wanted


@pytest.mark.parametrize(
    ("environment_id", "expected"),
    [
        (ENV_A, {(ENV_A, "alpha")}),
        (ENV_B, {(ENV_B, "alpha"), (ENV_B, "beta")}),
    ],
)
async def test_a_filter_built_for_one_environment_admits_nothing_from_another(
    access: AccessServices,
    controls: ResourceControlRepository,
    db: Session,
    environment_id: int,
    expected: set[tuple[int, str]],
) -> None:
    for key in (volume_key("alpha", ENV_A), volume_key("alpha", ENV_B), volume_key("beta", ENV_B)):
        await plant(controls, key, public=True)
    await stock(db, (ENV_A, "alpha"), (ENV_B, "alpha"), (ENV_A, "beta"), (ENV_B, "beta"))
    fragment = await filter_for(access, environment_id, "volume")

    assert await admitted(db, fragment) == expected


async def test_a_control_in_another_environment_does_not_stand_in_for_a_missing_one(
    access: AccessServices, controls: ResourceControlRepository, db: Session
) -> None:
    await plant(controls, volume_key("beta", ENV_B), public=True)
    await stock(db, (ENV_A, "beta"))
    fragment = await filter_for(access, ENV_A, "volume")

    assert await admitted(db, fragment) == set()


async def test_the_uncontrolled_default_is_decided_within_the_rows_own_environment(
    access: AccessServices, controls: ResourceControlRepository, db: Session
) -> None:
    await access.policy.set_uncontrolled_default(ADMIN, "read")
    await plant(controls, stack_key("delta", ENV_B), administrators_only=True)
    await plant(controls, stack_key("epsilon", ENV_A), administrators_only=True)
    await stock(db, (ENV_A, "delta"), (ENV_A, "epsilon"), (ENV_A, "zeta"), (ENV_B, "zeta"))
    fragment = await filter_for(access, ENV_A, "stack")

    assert await admitted(db, fragment) == {(ENV_A, "delta"), (ENV_A, "zeta")}


async def test_a_restrictive_default_admits_no_uncontrolled_row(
    access: AccessServices, db: Session
) -> None:
    await stock(db, (ENV_A, "zeta"))
    fragment = await filter_for(access, ENV_A, "stack")

    assert await admitted(db, fragment) == set()


async def test_inherited_access_and_the_default_are_not_applied_to_a_type_that_inherits(
    access: AccessServices, controls: ResourceControlRepository, db: Session
) -> None:
    await access.policy.set_uncontrolled_default(ADMIN, "read")
    controlled, uncontrolled = container_id(1), container_id(2)
    await plant(controls, ResourceKey(ENV_A, "container", controlled), public=True)
    await stock(db, (ENV_A, controlled), (ENV_A, uncontrolled))
    fragment = await filter_for(access, ENV_A, "container")

    assert await admitted(db, fragment) == {(ENV_A, controlled)}


@pytest.mark.parametrize(
    ("required", "expected"),
    [("read", {"readable", "writable"}), ("write", {"writable"})],
)
async def test_a_filter_admits_only_controls_that_give_the_required_level(
    access: AccessServices,
    controls: ResourceControlRepository,
    db: Session,
    required: AccessLevel,
    expected: set[str],
) -> None:
    await plant(controls, volume_key("readable"), public=True, level="read")
    await plant(controls, volume_key("writable"), public=True, level="write")
    await stock(db, (ENV_A, "readable"), (ENV_A, "writable"))
    fragment = await filter_for(access, ENV_A, "volume", required)

    assert {name for _, name in await admitted(db, fragment)} == expected


@pytest.fixture
async def volumes_by_reason(access: AccessServices, db: Session) -> int:
    """Volumes the filter must admit or refuse for different reasons; returns the team id."""
    team_id = await team_of(access, TEAM_NAME, STANDARD)
    await control(access, volume_key("owned"), owner=STANDARD_ID)
    await control(access, volume_key("granted"), users=(STANDARD_ID,))
    await control(access, volume_key("via-team"), teams=(team_id,))
    await control(access, volume_key("granted-to-peer"), users=(PEER_ID,))
    await control(
        access, volume_key("refused-but-owned"), administrators_only=True, owner=STANDARD_ID
    )
    await control(access, volume_key("nobody"))
    await stock(
        db,
        *(
            (ENV_A, name)
            for name in (
                "owned",
                "granted",
                "via-team",
                "granted-to-peer",
                "refused-but-owned",
                "nobody",
            )
        ),
    )
    return team_id


async def test_a_filter_admits_what_the_user_owns_or_is_granted_directly_or_through_a_team(
    access: AccessServices, db: Session, volumes_by_reason: int
) -> None:
    fragment = await filter_for(access, ENV_A, "volume")

    assert {name for _, name in await admitted(db, fragment)} == {"owned", "granted", "via-team"}


async def test_a_filter_built_after_membership_ends_no_longer_admits_the_team_grant(
    access: AccessServices, db: Session, volumes_by_reason: int
) -> None:
    await access.teams.remove_member(ADMIN, volumes_by_reason, user_id=STANDARD_ID)
    fragment = await filter_for(access, ENV_A, "volume")

    assert {name for _, name in await admitted(db, fragment)} == {"owned", "granted"}


@pytest.mark.parametrize(
    ("control_name", "row_name"),
    [("WebApp", "webapp"), ("mystack", "MyStack"), ("MIXED", "mixed")],
)
async def test_a_filter_matches_a_stack_whatever_the_case_on_either_side(
    access: AccessServices,
    controls: ResourceControlRepository,
    db: Session,
    control_name: str,
    row_name: str,
) -> None:
    await plant(controls, stack_key(control_name), public=True)
    await stock(db, (ENV_A, row_name))
    fragment = await filter_for(access, ENV_A, "stack")

    assert await admitted(db, fragment) == {(ENV_A, row_name)}


@pytest.mark.parametrize(
    ("column", "environment_column"),
    [("name", "item.environment_id"), ("item.name", "environment_id"), ("name", "environment_id")],
    ids=["unqualified-object", "unqualified-environment", "both-unqualified"],
)
async def test_sql_filter_refuses_a_column_that_does_not_name_its_table(
    access: AccessServices, column: str, environment_column: str
) -> None:
    visibility = await access.authoriser.resolve_visibility(STANDARD, ENV_A, "volume")

    with pytest.raises(ValueError, match="name its table"):
        visibility.sql_filter(column, environment_column)
