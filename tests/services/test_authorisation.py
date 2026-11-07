"""The enforcement helpers every service reaches Docker objects through, against a real database."""

from collections.abc import Callable

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.db.repositories.resource_control import _SCAN_PAGE as SCAN_PAGE
from app.db.repositories.resource_control import ResourceControlRepository
from app.services.access import ResourceKey, ResourceRef
from app.services.assembly import AccessServices
from app.services.authorisation import Authoriser
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    MEMBER,
    MEMBER_ID,
    PEER,
    PEER_ID,
    STACK_NAME,
    STANDARD,
    TEAM_NAME,
    FakeInspector,
    InspectorCall,
    ListedObject,
    RecordingControls,
    Scan,
    bulk_controls,
    compose_labels,
    container_id,
    container_key,
    control,
    plant,
    service_id,
    stack_key,
    swarm_labels,
    team_of,
    volume_key,
)
from tests.auth_support import STANDARD_ID, FakeClock

STACK_LABELS = compose_labels(STACK_NAME)
LATE_STACK_FORMAT = "late-stack-%d"
LATE_STACK = "late-stack-1"


@pytest.fixture
def container(inspector: FakeInspector) -> ResourceKey:
    """A container Docker reports, carrying no stack label."""
    key = container_key(1)
    inspector.add(key)
    return key


@pytest.fixture
def stacked_container(inspector: FakeInspector) -> ResourceKey:
    """A container Docker reports as part of the shop stack."""
    key = container_key(1)
    inspector.add(key, STACK_LABELS)
    return key


@pytest.fixture
def recorded(
    db: Session, clock: FakeClock, access: AccessServices, inspector: FakeInspector
) -> tuple[Authoriser, RecordingControls]:
    """An authoriser over a repository that notes every scan it is asked for."""
    repository = RecordingControls(db, clock)
    authoriser = Authoriser(
        controls=repository,
        environments=EnvironmentAccessRepository(db),
        policy=access.policy,
        inspector=inspector,
    )
    return authoriser, repository


async def test_a_standard_user_with_no_grant_gets_not_found_for_a_specific_container(
    access: AccessServices, container: ResourceKey
) -> None:
    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_access(STANDARD, container)


async def test_a_standard_user_with_no_grant_sees_nothing_in_a_listing(
    access: AccessServices, container: ResourceKey
) -> None:
    listing = [ListedObject.of(container)]

    visible = await access.authoriser.filter_visible(STANDARD, listing)

    assert visible == []


async def test_a_refused_container_and_a_missing_one_are_refused_identically(
    access: AccessServices, container: ResourceKey
) -> None:
    with pytest.raises(ResourceNotFound) as refused:
        await access.authoriser.require_access(STANDARD, container)
    with pytest.raises(ResourceNotFound) as missing:
        await access.authoriser.require_access(STANDARD, container_key(2))

    assert str(refused.value) == str(missing.value)


async def test_an_administrator_is_admitted_without_consulting_docker(
    access: AccessServices, inspector: FakeInspector
) -> None:
    await access.authoriser.require_access(ADMIN, container_key(1))

    assert inspector.calls == []


async def test_a_grant_to_a_user_admits_that_user_and_no_other(
    access: AccessServices, container: ResourceKey
) -> None:
    await control(access, container, users=(STANDARD_ID,))

    await access.authoriser.require_access(STANDARD, container)
    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_access(PEER, container)


@pytest.fixture
async def team_granted_container(access: AccessServices, container: ResourceKey) -> int:
    """A container granted to a team of MEMBER and PEER; returns the team id."""
    team_id = await team_of(access, TEAM_NAME, MEMBER, PEER)
    await control(access, container, teams=(team_id,))
    return team_id


async def test_a_team_grant_is_effective_for_every_member_and_no_one_else(
    access: AccessServices, container: ResourceKey, team_granted_container: int
) -> None:
    listing = [ListedObject.of(container)]

    seen = {
        who.username: len(await access.authoriser.filter_visible(who, listing))
        for who in (MEMBER, PEER, STANDARD)
    }

    assert seen == {MEMBER.username: 1, PEER.username: 1, STANDARD.username: 0}


async def test_a_team_grant_stops_the_moment_membership_is_removed(
    access: AccessServices,
    new_request: Callable[[], AccessServices],
    container: ResourceKey,
    team_granted_container: int,
) -> None:
    await access.teams.remove_member(ADMIN, team_granted_container, user_id=MEMBER_ID)

    with pytest.raises(ResourceNotFound):
        await new_request().authoriser.require_access(MEMBER, container)


async def test_removing_one_member_leaves_the_other_members_access_intact(
    access: AccessServices,
    new_request: Callable[[], AccessServices],
    container: ResourceKey,
    team_granted_container: int,
) -> None:
    await access.teams.remove_member(ADMIN, team_granted_container, user_id=MEMBER_ID)

    await new_request().authoriser.require_access(PEER, container)


async def test_deleting_a_team_revokes_what_it_was_granted(
    access: AccessServices,
    new_request: Callable[[], AccessServices],
    container: ResourceKey,
    team_granted_container: int,
) -> None:
    await access.teams.delete(ADMIN, team_granted_container, expected_name=TEAM_NAME)

    with pytest.raises(ResourceNotFound):
        await new_request().authoriser.require_access(MEMBER, container)


async def test_a_container_carrying_a_controlled_stacks_label_is_visible_to_its_grantees(
    access: AccessServices, stacked_container: ResourceKey
) -> None:
    await control(access, stack_key(STACK_NAME), users=(STANDARD_ID,))
    listing = [ListedObject.of(stacked_container, STACK_LABELS)]

    visible = await access.authoriser.filter_visible(STANDARD, listing)

    assert visible == listing


async def test_a_single_container_of_a_controlled_stack_is_reachable_by_its_grantees(
    access: AccessServices, stacked_container: ResourceKey
) -> None:
    await control(access, stack_key(STACK_NAME), users=(STANDARD_ID,))

    await access.authoriser.require_access(STANDARD, stacked_container)


async def test_a_container_of_a_controlled_stack_is_hidden_from_users_the_stack_does_not_grant(
    access: AccessServices, stacked_container: ResourceKey
) -> None:
    await control(access, stack_key(STACK_NAME), users=(STANDARD_ID,))

    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_access(PEER, stacked_container)


async def test_a_service_in_a_swarm_namespace_inherits_the_stacks_control(
    access: AccessServices, inspector: FakeInspector
) -> None:
    service = ResourceKey(ENV_A, "service", service_id(1))
    inspector.add(service, swarm_labels(STACK_NAME))
    await control(access, stack_key(STACK_NAME), users=(STANDARD_ID,))

    await access.authoriser.require_access(STANDARD, service)


async def test_a_container_with_its_own_control_uses_it_instead_of_its_stacks(
    access: AccessServices,
    new_request: Callable[[], AccessServices],
    stacked_container: ResourceKey,
) -> None:
    await control(access, stack_key(STACK_NAME), users=(STANDARD_ID,))
    await control(access, stacked_container, users=(PEER_ID,))

    await new_request().authoriser.require_access(PEER, stacked_container)
    with pytest.raises(ResourceNotFound):
        await new_request().authoriser.require_access(STANDARD, stacked_container)


@pytest.mark.parametrize(
    ("control_name", "labels"),
    [
        ("WebApp", compose_labels("webapp")),
        ("mystack", swarm_labels("MyStack")),
    ],
    ids=["control cased, project lower", "control lower, namespace cased"],
)
async def test_a_stack_control_governs_its_members_whatever_the_case_of_either_name(
    access: AccessServices,
    controls: ResourceControlRepository,
    inspector: FakeInspector,
    container: ResourceKey,
    control_name: str,
    labels: dict[str, str],
) -> None:
    await plant(controls, stack_key(control_name), public=True)
    inspector.add(container, labels)

    await access.authoriser.require_access(STANDARD, container)


async def test_a_stack_redeployed_with_entirely_new_container_ids_keeps_its_access(
    access: AccessServices,
    new_request: Callable[[], AccessServices],
    inspector: FakeInspector,
) -> None:
    before = [container_key(1), container_key(2)]
    after = [container_key(3), container_key(4)]
    for key in before:
        inspector.add(key, STACK_LABELS)
    await control(access, stack_key(STACK_NAME), users=(STANDARD_ID,))
    listed_before = [ListedObject.of(key, STACK_LABELS) for key in before]
    assert await access.authoriser.filter_visible(STANDARD, listed_before) == listed_before
    for key in before:
        inspector.remove(key)
    for key in after:
        inspector.add(key, STACK_LABELS)
    listed_after = [ListedObject.of(key, STACK_LABELS) for key in after]

    visible = await new_request().authoriser.filter_visible(STANDARD, listed_after)

    assert visible == listed_after


async def test_a_redeployed_stacks_new_container_is_reachable_by_id(
    access: AccessServices,
    new_request: Callable[[], AccessServices],
    inspector: FakeInspector,
) -> None:
    await control(access, stack_key(STACK_NAME), users=(STANDARD_ID,))
    redeployed = container_key(9)
    inspector.add(redeployed, STACK_LABELS)

    await new_request().authoriser.require_access(STANDARD, redeployed)


async def test_a_stale_sub_resource_list_cannot_grant_access(
    access: AccessServices,
    new_request: Callable[[], AccessServices],
    inspector: FakeInspector,
    stacked_container: ResourceKey,
) -> None:
    stack = stack_key(STACK_NAME)
    await control(access, stack, users=(STANDARD_ID,))
    member = ResourceRef(ENV_A, "container", stacked_container.resource_id, STACK_LABELS)
    await access.controls.refresh_sub_resources(stack, [member])
    inspector.add(stacked_container, {})

    with pytest.raises(ResourceNotFound):
        await new_request().authoriser.require_access(STANDARD, stacked_container)


async def test_labels_supplied_by_the_caller_do_not_place_a_container_in_a_stack(
    access: AccessServices, container: ResourceKey
) -> None:
    await control(access, stack_key(STACK_NAME), users=(STANDARD_ID,))
    forged = ResourceRef(ENV_A, "container", container.resource_id, STACK_LABELS)

    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_access(STANDARD, forged)  # type: ignore[arg-type]


async def test_a_mutation_naming_forged_labels_is_refused_before_its_body_runs(
    access: AccessServices, container: ResourceKey
) -> None:
    await control(access, stack_key(STACK_NAME), level="write", users=(STANDARD_ID,))
    forged = ResourceRef(ENV_A, "container", container.resource_id, STACK_LABELS)
    reached: list[bool] = []

    with pytest.raises(ResourceNotFound):
        async with access.authoriser.mutation(STANDARD, forged):  # type: ignore[arg-type]
            reached.append(True)

    assert reached == []


async def test_the_labels_a_check_uses_are_the_ones_the_inspector_reports(
    access: AccessServices, inspector: FakeInspector, stacked_container: ResourceKey
) -> None:
    await control(access, stack_key(STACK_NAME), users=(STANDARD_ID,))

    await access.authoriser.require_access(STANDARD, stacked_container)

    assert inspector.calls == [InspectorCall(ENV_A, "container", stacked_container.resource_id)]


async def test_a_stack_is_checked_without_consulting_docker(
    access: AccessServices, inspector: FakeInspector
) -> None:
    stack = stack_key(STACK_NAME)
    await control(access, stack, users=(STANDARD_ID,))

    await access.authoriser.require_access(STANDARD, stack)

    assert inspector.calls == []


async def test_access_below_the_required_level_is_forbidden(
    access: AccessServices, container: ResourceKey
) -> None:
    await control(access, container, level="read", users=(STANDARD_ID,))

    with pytest.raises(PermissionDenied):
        await access.authoriser.require_access(STANDARD, container, "write")


async def test_access_at_the_required_level_is_admitted(
    access: AccessServices, container: ResourceKey
) -> None:
    await control(access, container, level="write", users=(STANDARD_ID,))

    await access.authoriser.require_access(STANDARD, container, "write")


async def test_no_access_to_an_object_is_not_found_even_when_another_is_readable(
    access: AccessServices, inspector: FakeInspector, container: ResourceKey
) -> None:
    readable = container_key(2)
    inspector.add(readable)
    await control(access, readable, users=(STANDARD_ID,))

    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_access(STANDARD, container, "write")


async def test_a_listing_omits_what_the_caller_may_not_see_and_never_refuses(
    access: AccessServices, inspector: FakeInspector, container: ResourceKey
) -> None:
    readable = container_key(2)
    inspector.add(readable)
    await control(access, readable, users=(STANDARD_ID,))
    listing = [ListedObject.of(container), ListedObject.of(readable)]

    visible = await access.authoriser.filter_visible(STANDARD, listing)

    assert visible == [listing[1]]


async def test_a_listing_at_a_higher_level_omits_what_is_only_readable_and_never_forbids(
    access: AccessServices, container: ResourceKey
) -> None:
    await control(access, container, level="read", users=(STANDARD_ID,))

    visible = await access.authoriser.filter_visible(
        STANDARD, [ListedObject.of(container)], "write"
    )

    assert visible == []


async def test_a_listing_keeps_the_original_order_across_environments_and_types(
    access: AccessServices, inspector: FakeInspector
) -> None:
    keys = [volume_key("zeta"), container_key(1), volume_key("alpha", 2)]
    for key in keys:
        await control(access, key, public=True)
    listing = [ListedObject.of(key) for key in keys]

    visible = await access.authoriser.filter_visible(STANDARD, listing)

    assert visible == listing


async def test_an_administrators_listing_is_returned_whole(
    access: AccessServices, container: ResourceKey
) -> None:
    listing = [ListedObject.of(container)]

    assert await access.authoriser.filter_visible(ADMIN, listing) == listing


async def test_a_refused_mutation_never_reaches_its_body(
    access: AccessServices, container: ResourceKey
) -> None:
    reached: list[bool] = []

    with pytest.raises(ResourceNotFound):
        async with access.authoriser.mutation(STANDARD, container):
            reached.append(True)

    assert reached == []


async def test_a_mutation_by_a_holder_of_read_only_is_forbidden_before_its_body_runs(
    access: AccessServices, container: ResourceKey
) -> None:
    await control(access, container, level="read", users=(STANDARD_ID,))
    reached: list[bool] = []

    with pytest.raises(PermissionDenied):
        async with access.authoriser.mutation(STANDARD, container):
            reached.append(True)

    assert reached == []


async def test_a_mutation_by_a_holder_of_write_runs_its_body(
    access: AccessServices, container: ResourceKey
) -> None:
    await control(access, container, level="write", users=(STANDARD_ID,))
    reached: list[bool] = []

    async with access.authoriser.mutation(STANDARD, container):
        reached.append(True)

    assert reached == [True]


@pytest.mark.parametrize(
    "malformed",
    [container_id(1)[:12], "web", f" {container_id(1)} ", f"{container_id(1)}\n"],
)
async def test_a_container_keyed_by_a_short_id_a_name_or_padding_is_never_admitted(
    access: AccessServices,
    controls: ResourceControlRepository,
    inspector: FakeInspector,
    malformed: str,
) -> None:
    key = ResourceKey(ENV_A, "container", malformed)
    inspector.add(key)
    await controls.create(
        ENV_A,
        "container",
        malformed,
        is_public=True,
        is_administrators_only=False,
        access_level="write",
        owner_user_id=None,
    )

    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_access(STANDARD, key)

    assert inspector.calls == []


async def test_a_single_object_check_reads_only_that_objects_controls(
    recorded: tuple[Authoriser, RecordingControls], container: ResourceKey
) -> None:
    authoriser, repository = recorded

    with pytest.raises(ResourceNotFound):
        await authoriser.require_access(STANDARD, container)

    assert Scan("container", frozenset({container.resource_id})) in repository.scans


async def test_stack_controls_are_read_in_full_whatever_the_narrowing(
    recorded: tuple[Authoriser, RecordingControls], container: ResourceKey
) -> None:
    authoriser, repository = recorded

    with pytest.raises(ResourceNotFound):
        await authoriser.require_access(STANDARD, container)

    assert Scan("stack", None) in repository.scans


async def test_a_repeat_check_on_the_same_object_does_not_rescan(
    recorded: tuple[Authoriser, RecordingControls], container: ResourceKey
) -> None:
    authoriser, repository = recorded
    with pytest.raises(ResourceNotFound):
        await authoriser.require_access(STANDARD, container)
    scans_after_first = list(repository.scans)

    with pytest.raises(ResourceNotFound):
        await authoriser.require_access(STANDARD, container)

    assert repository.scans == scans_after_first


async def test_a_second_object_is_judged_by_its_own_control_rather_than_as_uncontrolled(
    recorded: tuple[Authoriser, RecordingControls],
    access: AccessServices,
    inspector: FakeInspector,
    container: ResourceKey,
) -> None:
    authoriser, _ = recorded
    readable = container_key(2)
    inspector.add(readable)
    await access.policy.set_uncontrolled_default(ADMIN, "read")
    await control(access, container)
    await control(access, readable, users=(STANDARD_ID,))
    await authoriser.require_access(STANDARD, readable)

    with pytest.raises(ResourceNotFound):
        await authoriser.require_access(STANDARD, container)


async def test_a_stack_control_beyond_the_first_scan_page_still_hides_its_containers(
    access: AccessServices, db: Session, inspector: FakeInspector
) -> None:
    await bulk_controls(db, ENV_A, "stack", "stack-%d", SCAN_PAGE)
    await bulk_controls(db, ENV_A, "stack", LATE_STACK_FORMAT, 1)
    late = container_key(1)
    inspector.add(late, compose_labels(LATE_STACK))
    await access.policy.set_uncontrolled_default(ADMIN, "read")

    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_access(STANDARD, late)


UNKNOWN_ENVIRONMENT = 999


@pytest.mark.parametrize(
    ("who", "environment_id", "expected"),
    [
        pytest.param(ADMIN, ENV_A, True, id="administrator-remote"),
        pytest.param(ADMIN, ENV_B, True, id="administrator-local"),
        pytest.param(ADMIN, UNKNOWN_ENVIRONMENT, True, id="administrator-anywhere"),
        pytest.param(STANDARD, ENV_A, True, id="standard-remote-with-a-grant"),
        pytest.param(STANDARD, ENV_B, False, id="standard-local-socket"),
        pytest.param(STANDARD, UNKNOWN_ENVIRONMENT, False, id="standard-unknown-environment"),
    ],
)
async def test_may_create_reports_who_may_create_where(
    access: AccessServices, who: Principal, environment_id: int, expected: bool
) -> None:
    assert await access.authoriser.may_create(who, environment_id) is expected


@pytest.mark.parametrize(
    ("who", "environment_id"),
    [(ADMIN, ENV_A), (ADMIN, ENV_B), (ADMIN, UNKNOWN_ENVIRONMENT), (STANDARD, ENV_A)],
)
async def test_require_create_passes_for_an_administrator_and_for_a_granted_remote_environment(
    access: AccessServices, who: Principal, environment_id: int
) -> None:
    await access.authoriser.require_create(who, environment_id)


@pytest.mark.parametrize(
    ("environment_id", "error"),
    [(ENV_B, PermissionDenied), (UNKNOWN_ENVIRONMENT, ResourceNotFound)],
    ids=["local-socket-is-forbidden", "unknown-environment-is-not-found"],
)
async def test_require_create_refuses_a_standard_user_with_the_error_that_fits(
    access: AccessServices, environment_id: int, error: type[Exception]
) -> None:
    with pytest.raises(error):
        await access.authoriser.require_create(STANDARD, environment_id)


@pytest.mark.usefixtures("no_grants")
async def test_without_a_grant_on_the_environment_creating_is_not_found_and_not_offered(
    access: AccessServices,
) -> None:
    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_create(STANDARD, ENV_A)

    assert await access.authoriser.may_create(STANDARD, ENV_A) is False
