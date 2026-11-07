"""The environment list: filters, paging, snapshot freshness and what each role may see."""

from datetime import timedelta

import pytest

from app.auth.errors import ValidationFailed
from app.db.engine import Session
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.environment import MAX_NAME_FILTER, MAX_TAG_FILTERS, EnvironmentStatus
from app.db.repositories.environment_snapshot import EnvironmentSnapshotRepository, SnapshotCounts
from app.db.repositories.pagination import Page
from app.docker_engine.registry import EngineClientRegistry
from app.services.assembly import EnvironmentServices
from app.services.environments import DashboardFilters, EnvironmentSummary
from tests.access_support import ADMIN, ENV_A, ENV_B, MEMBER, STANDARD
from tests.auth_support import FakeClock
from tests.db.helpers import walk_pages
from tests.environment_support import STALE_AFTER, ScriptedProbes, seed_environment

SEEDED_NAMES = ["environment-a", "environment-b"]
CONTAINERS = 7
IMAGES = 3
SNAPSHOT_ERROR = "Too many objects to count."


@pytest.fixture
async def fleet(environments: EnvironmentServices, db: Session, clock: FakeClock) -> dict[str, int]:
    """Four more environments: two grouped and tagged, one failing, one with LIKE characters."""
    ids: dict[str, int] = {}
    statuses: tuple[tuple[str, EnvironmentStatus], ...] = (
        ("alpha", "reachable"),
        ("beta", "unreachable"),
        ("gamma", "reachable"),
        ("sale%", "reachable"),
        ("saleX", "reachable"),
        ("a_b", "reachable"),
        ("aXb", "reachable"),
    )
    for name, status in statuses:
        created = await seed_environment(
            db, clock, name, endpoint=f"/run/{ord(name[0])}-{len(ids)}.sock", status=status
        )
        ids[name] = created.id
    group = await environments.groups.create(ADMIN, name="production")
    ids["group"] = group.id
    blue = await environments.tags.create(ADMIN, name="blue")
    green = await environments.tags.create(ADMIN, name="green")
    ids["blue"], ids["green"] = blue.id, green.id
    for name in ("alpha", "beta"):
        await environments.environments.assign_group(ADMIN, ids[name], group.id)
    await environments.environments.set_tags(ADMIN, ids["alpha"], [blue.id, green.id])
    await environments.environments.set_tags(ADMIN, ids["beta"], [blue.id])
    return ids


def names(page: Page[EnvironmentSummary]) -> list[str]:
    return [item.name for item in page.items]


async def test_an_administrator_sees_every_environment_ordered_by_name(
    environments: EnvironmentServices, fleet: dict[str, int]
) -> None:
    page = await environments.environments.dashboard(ADMIN, limit=100)

    assert names(page) == sorted(names(page), key=str.casefold)
    assert {"alpha", "beta", "gamma", *SEEDED_NAMES} <= set(names(page))


async def test_walking_the_pages_lists_each_environment_once_in_order(
    environments: EnvironmentServices, fleet: dict[str, int]
) -> None:
    everything = names(await environments.environments.dashboard(ADMIN, limit=100))

    walk = await walk_pages(
        lambda limit, cursor: environments.environments.dashboard(
            ADMIN, limit=limit, cursor=cursor
        ),
        limit=3,
        key=lambda item: item.name,
    )

    assert walk.keys == everything
    assert max(walk.page_sizes) == 3
    assert walk.final_cursor is None


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (DashboardFilters(status="unreachable"), ["beta"]),
        (DashboardFilters(q="alp"), ["alpha"]),
        (DashboardFilters(q="ALPHA"), ["alpha"]),
        (DashboardFilters(q="%"), ["sale%"]),
        (DashboardFilters(q="a_b"), ["a_b"]),
    ],
    ids=[
        "status",
        "name substring",
        "name ignores case",
        "percent is literal",
        "underscore is literal",
    ],
)
async def test_each_filter_narrows_the_list(
    environments: EnvironmentServices,
    fleet: dict[str, int],
    filters: DashboardFilters,
    expected: list[str],
) -> None:
    page = await environments.environments.dashboard(ADMIN, filters, limit=100)

    assert names(page) == expected


async def test_a_group_filter_lists_only_that_groups_environments(
    environments: EnvironmentServices, fleet: dict[str, int]
) -> None:
    page = await environments.environments.dashboard(
        ADMIN, DashboardFilters(group_id=fleet["group"]), limit=100
    )

    assert names(page) == ["alpha", "beta"]


async def test_the_ungrouped_filter_excludes_every_grouped_environment(
    environments: EnvironmentServices, fleet: dict[str, int]
) -> None:
    page = await environments.environments.dashboard(
        ADMIN, DashboardFilters(ungrouped=True), limit=100
    )

    assert not {"alpha", "beta"} & set(names(page))
    assert "gamma" in names(page)


async def test_several_tags_are_matched_as_all_of(
    environments: EnvironmentServices, fleet: dict[str, int]
) -> None:
    both = await environments.environments.dashboard(
        ADMIN, DashboardFilters(tag_ids=(fleet["blue"], fleet["green"])), limit=100
    )
    blue = await environments.environments.dashboard(
        ADMIN, DashboardFilters(tag_ids=(fleet["blue"],)), limit=100
    )

    assert names(both) == ["alpha"]
    assert names(blue) == ["alpha", "beta"]


@pytest.mark.parametrize(
    ("filters", "field"),
    [
        (DashboardFilters(status="up"), "status"),
        (DashboardFilters(q="x" * (MAX_NAME_FILTER + 1)), "q"),
        (DashboardFilters(group_id=1, ungrouped=True), "group"),
        (DashboardFilters(tag_ids=tuple(range(1, MAX_TAG_FILTERS + 2))), "tag"),
    ],
    ids=["unknown status", "long query", "group and ungrouped", "too many tags"],
)
async def test_a_filter_the_list_cannot_honour_is_refused_by_field(
    environments: EnvironmentServices, filters: DashboardFilters, field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await environments.environments.dashboard(ADMIN, filters)

    assert list(refused.value.fields) == [field]


async def test_listing_and_opening_environments_makes_no_engine_call(
    environments: EnvironmentServices,
    fleet: dict[str, int],
    probes: ScriptedProbes,
    client_registry: EngineClientRegistry,
    clients_built: list[object],
) -> None:
    await environments.environments.dashboard(ADMIN, limit=100)
    await environments.environments.get(ADMIN, fleet["alpha"])
    await environments.environments.dashboard(STANDARD, limit=100)

    assert probes.calls == []
    assert clients_built == []


async def test_an_administrator_sees_endpoint_and_counts_and_a_missing_snapshot_is_a_state(
    environments: EnvironmentServices,
    fleet: dict[str, int],
    db: Session,
    clock: FakeClock,
) -> None:
    await EnvironmentSnapshotRepository(db).record_counts(
        fleet["alpha"],
        format_timestamp(clock.now),
        SnapshotCounts(container_count=CONTAINERS, image_count=IMAGES),
    )

    page = await environments.environments.dashboard(ADMIN, DashboardFilters(q="a"), limit=100)

    by_name = {item.name: item for item in page.items}
    alpha, gamma = by_name["alpha"], by_name["gamma"]
    assert alpha.connection is not None and alpha.connection.endpoint.startswith("unix://")
    assert alpha.snapshot is not None and alpha.snapshot.counts is not None
    assert (alpha.snapshot.counts.container_count, alpha.snapshot.counts.image_count) == (
        CONTAINERS,
        IMAGES,
    )
    assert gamma.snapshot is not None
    assert (gamma.snapshot.captured_at, gamma.snapshot.counts, gamma.snapshot.stale) == (
        None,
        None,
        False,
    )


async def test_a_failed_snapshot_shows_its_error_with_no_counts(
    environments: EnvironmentServices, fleet: dict[str, int], db: Session, clock: FakeClock
) -> None:
    await EnvironmentSnapshotRepository(db).record_error(
        fleet["beta"], format_timestamp(clock.now), SNAPSHOT_ERROR
    )

    detail = await environments.environments.get(ADMIN, fleet["beta"])

    assert detail.summary.snapshot is not None
    assert (detail.summary.snapshot.error, detail.summary.snapshot.counts) == (
        SNAPSHOT_ERROR,
        None,
    )


@pytest.mark.parametrize(
    ("age", "stale"),
    [
        (STALE_AFTER - timedelta(seconds=1), False),
        (STALE_AFTER, False),
        (STALE_AFTER + timedelta(seconds=1), True),
    ],
    ids=["inside", "at the limit", "past the limit"],
)
async def test_a_snapshot_older_than_three_intervals_is_marked_stale(
    environments: EnvironmentServices,
    fleet: dict[str, int],
    db: Session,
    clock: FakeClock,
    age: timedelta,
    stale: bool,
) -> None:
    await EnvironmentSnapshotRepository(db).record_counts(
        fleet["alpha"], format_timestamp(clock.now), SnapshotCounts(container_count=1)
    )
    clock.advance(age)

    detail = await environments.environments.get(ADMIN, fleet["alpha"])

    assert detail.summary.snapshot is not None
    assert detail.summary.snapshot.stale is stale


async def test_a_standard_user_sees_only_granted_environments_without_connection_data(
    environments: EnvironmentServices, fleet: dict[str, int], db: Session, clock: FakeClock
) -> None:
    await EnvironmentSnapshotRepository(db).record_counts(
        ENV_A, format_timestamp(clock.now), SnapshotCounts(container_count=CONTAINERS)
    )

    page = await environments.environments.dashboard(STANDARD, limit=100)

    assert names(page) == SEEDED_NAMES
    first = page.items[0]
    assert (first.connection, first.snapshot) == (None, None)
    assert (first.failed_stage, first.status_message) == (None, None)
    assert [item.is_local for item in page.items] == [False, True]


async def test_a_group_grant_shows_the_groups_environments_and_their_group_and_tag_names(
    environments: EnvironmentServices, fleet: dict[str, int]
) -> None:
    await environments.groups.grant(ADMIN, fleet["group"], user_id=MEMBER.user_id)

    page = await environments.environments.dashboard(MEMBER, limit=100)

    assert names(page) == ["alpha", "beta", *SEEDED_NAMES]
    alpha = page.items[0]
    assert (alpha.group.name if alpha.group else None, [t.name for t in alpha.tags]) == (
        "production",
        ["blue", "green"],
    )


async def test_a_standard_user_filtering_by_a_group_they_cannot_reach_gets_an_empty_list(
    environments: EnvironmentServices, fleet: dict[str, int]
) -> None:
    page = await environments.environments.dashboard(
        STANDARD, DashboardFilters(group_id=fleet["group"]), limit=100
    )

    assert page.items == []


async def test_the_filter_options_of_a_standard_user_are_limited_to_what_they_can_see(
    environments: EnvironmentServices, fleet: dict[str, int]
) -> None:
    administrator = await environments.environments.filter_options(ADMIN)
    standard = await environments.environments.filter_options(STANDARD)

    assert [tag.name for tag in administrator.tags] == ["blue", "green"]
    assert [group.name for group in administrator.groups] == ["production"]
    assert (standard.tags, standard.groups) == ((), ())


async def test_a_standard_user_opening_a_granted_environment_sees_no_endpoint(
    environments: EnvironmentServices,
) -> None:
    detail = await environments.environments.get(STANDARD, ENV_B)

    assert detail.summary.connection is None
    assert detail.can_manage is False
    assert detail.created_at is None
