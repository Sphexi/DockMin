"""Creating, changing and removing the control over one object."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

import pytest

from app.auth.errors import PermissionDenied, ResourceControlNotFound, ValidationFailed
from app.db.engine import Session
from app.db.repositories.errors import DuplicateError
from app.db.repositories.resource_control import MAX_GRANTS_PER_KIND
from app.services.access import ResourceKey, ResourceRef
from app.services.assembly import AccessServices
from app.services.resource_controls import (
    CHANGED_REASON,
    RESOURCE_ID_MAX,
    ControlChanged,
    ControlSpec,
    ControlView,
    control_digest,
)
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    PEER_ID,
    STACK_NAME,
    STANDARD,
    UNKNOWN_TEAM_ID,
    UNKNOWN_USER_ID,
    compose_labels,
    container_id,
    container_key,
    control,
    digest_of,
    service_id,
    stack_key,
    swarm_labels,
    volume_key,
)
from tests.auth_support import STANDARD_ID
from tests.object_support import rows_of
from tests.registry_support import make_team

READ_SPEC = ControlSpec(access_level="read")
PEER_SPEC = ControlSpec(access_level="write", user_ids=(PEER_ID,))
KEY = container_key(1)
NO_CONTROL = ""
STALE_DIGEST = "0" * 64

Operation = Callable[[AccessServices], Awaitable[object]]


@pytest.mark.parametrize(
    "operation",
    [
        lambda access: access.controls.get(STANDARD, KEY),
        lambda access: access.controls.create(STANDARD, KEY, READ_SPEC, expected_digest=NO_CONTROL),
        lambda access: access.controls.update(STANDARD, KEY, READ_SPEC, expected_digest=NO_CONTROL),
        lambda access: access.controls.delete(STANDARD, KEY, expected_digest=NO_CONTROL),
    ],
    ids=["get", "create", "update", "delete"],
)
async def test_only_an_administrator_may_manage_controls(
    access: AccessServices, operation: Operation
) -> None:
    with pytest.raises(PermissionDenied):
        await operation(access)


async def test_creating_a_control_returns_it_as_stored(access: AccessServices) -> None:
    spec = ControlSpec(access_level="write", owner_user_id=PEER_ID, user_ids=(STANDARD_ID,))

    view = await access.controls.create(ADMIN, KEY, spec, expected_digest=NO_CONTROL)

    assert view.key == KEY
    assert (view.access_level, view.owner_user_id, view.user_ids) == (
        "write",
        PEER_ID,
        (STANDARD_ID,),
    )
    assert not view.is_public
    assert not view.is_administrators_only


async def test_a_stack_control_is_stored_under_the_canonical_form_of_its_name(
    access: AccessServices,
) -> None:
    view = await access.controls.create(
        ADMIN, stack_key("WebApp"), READ_SPEC, expected_digest=NO_CONTROL
    )

    assert view.key.resource_id == "webapp"


async def test_getting_the_control_of_a_stack_works_under_any_case(
    access: AccessServices,
) -> None:
    await control(access, stack_key("WebApp"))

    view = await access.controls.get(ADMIN, stack_key("WEBAPP"))

    assert view is not None


async def test_an_object_without_a_control_has_none_to_get(access: AccessServices) -> None:
    assert await access.controls.get(ADMIN, KEY) is None


async def test_a_container_id_is_stored_trimmed(access: AccessServices) -> None:
    padded = ResourceKey(ENV_A, "container", f"  {container_id(1)}  ")

    view = await access.controls.create(ADMIN, padded, READ_SPEC, expected_digest=NO_CONTROL)

    assert view.key.resource_id == container_id(1)


async def test_creating_a_second_control_over_one_object_is_refused(
    access: AccessServices,
) -> None:
    await control(access, KEY)

    with pytest.raises(ValidationFailed) as refused:
        await access.controls.create(
            ADMIN, KEY, READ_SPEC, expected_digest=await digest_of(access, KEY)
        )

    assert "resource" in refused.value.fields


async def test_two_spellings_of_one_stack_name_cannot_both_be_controlled(
    access: AccessServices,
) -> None:
    await control(access, stack_key("WebApp"))

    with pytest.raises(ValidationFailed):
        await access.controls.create(
            ADMIN,
            stack_key("webapp"),
            READ_SPEC,
            expected_digest=await digest_of(access, stack_key("webapp")),
        )


@pytest.mark.parametrize(
    "granted", [{"user_ids": (UNKNOWN_USER_ID,)}, {"team_ids": (UNKNOWN_TEAM_ID,)}]
)
async def test_a_grant_to_a_user_or_team_that_does_not_exist_is_refused(
    access: AccessServices, granted: dict[str, Any]
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await access.controls.create(
            ADMIN,
            KEY,
            ControlSpec(access_level="read", **granted),
            expected_digest=NO_CONTROL,
        )

    assert "grants" in refused.value.fields


async def test_a_refused_creation_leaves_no_control_behind(access: AccessServices) -> None:
    spec = ControlSpec(access_level="read", user_ids=(UNKNOWN_USER_ID,))
    with pytest.raises(ValidationFailed):
        await access.controls.create(ADMIN, KEY, spec, expected_digest=NO_CONTROL)

    assert await access.controls.get(ADMIN, KEY) is None


@pytest.mark.parametrize(
    ("key", "field"),
    [
        (ResourceKey(ENV_A, "container", container_id(1)[:12]), "resource_id"),
        (ResourceKey(ENV_A, "container", "web"), "resource_id"),
        (ResourceKey(ENV_A, "container", f"  {container_id(1)[:12]}  "), "resource_id"),
        (ResourceKey(ENV_A, "container", "A" * 64), "resource_id"),
        (ResourceKey(ENV_A, "service", container_id(1)), "resource_id"),
        (ResourceKey(ENV_A, "service", "web_frontend"), "resource_id"),
        (ResourceKey(ENV_A, "volume", ""), "resource_id"),
        (ResourceKey(ENV_A, "volume", "   "), "resource_id"),
        (ResourceKey(ENV_A, "volume", "a" * (RESOURCE_ID_MAX + 1)), "resource_id"),
        (ResourceKey(ENV_A, "volume", "bad\x00name"), "resource_id"),
        (ResourceKey(ENV_A, "stack", "Straße"), "resource_id"),
        (ResourceKey(ENV_A, "pod", "anything"), "resource_type"),  # type: ignore[arg-type]
    ],
)
async def test_a_key_that_would_govern_nothing_is_refused(
    access: AccessServices, key: ResourceKey, field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await access.controls.create(ADMIN, key, READ_SPEC, expected_digest=NO_CONTROL)

    assert field in refused.value.fields


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        (ControlSpec(access_level="admin"), "access_level"),
        (ControlSpec(access_level=""), "access_level"),
        (
            ControlSpec(access_level="read", is_public=True, is_administrators_only=True),
            "is_public",
        ),
        (ControlSpec(access_level="read", is_public=True, user_ids=(PEER_ID,)), "grants"),
        (
            ControlSpec(access_level="read", is_administrators_only=True, team_ids=(1,)),
            "grants",
        ),
        (
            ControlSpec(access_level="read", user_ids=tuple(range(MAX_GRANTS_PER_KIND + 1))),
            "grants",
        ),
        (
            ControlSpec(access_level="read", team_ids=tuple(range(MAX_GRANTS_PER_KIND + 1))),
            "grants",
        ),
    ],
)
async def test_an_inconsistent_or_oversized_spec_is_refused(
    access: AccessServices, spec: ControlSpec, field: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await access.controls.create(ADMIN, KEY, spec, expected_digest=NO_CONTROL)

    assert field in refused.value.fields


@pytest.mark.parametrize(
    "key",
    [
        container_key(1),
        ResourceKey(ENV_A, "service", service_id(1)),
        volume_key("Data"),
        stack_key("MyStack"),
    ],
)
async def test_a_well_formed_key_of_each_type_is_accepted(
    access: AccessServices, key: ResourceKey
) -> None:
    view = await access.controls.create(ADMIN, key, READ_SPEC, expected_digest=NO_CONTROL)

    assert view.key.resource_type == key.resource_type


async def test_updating_replaces_the_flags_level_owner_and_grants(access: AccessServices) -> None:
    await control(access, KEY, users=(STANDARD_ID,))

    view = await access.controls.update(
        ADMIN, KEY, PEER_SPEC, expected_digest=await digest_of(access, KEY)
    )

    assert (view.access_level, view.user_ids) == ("write", (PEER_ID,))


async def test_updating_an_object_without_a_control_is_refused(access: AccessServices) -> None:
    with pytest.raises(ResourceControlNotFound):
        await access.controls.update(ADMIN, KEY, READ_SPEC, expected_digest=NO_CONTROL)


async def test_a_refused_update_leaves_the_control_unchanged(access: AccessServices) -> None:
    await control(access, KEY, users=(STANDARD_ID,))
    broken = ControlSpec(access_level="read", user_ids=(UNKNOWN_USER_ID,))
    with pytest.raises(ValidationFailed):
        await access.controls.update(
            ADMIN, KEY, broken, expected_digest=await digest_of(access, KEY)
        )

    view = await access.controls.get(ADMIN, KEY)

    assert view is not None
    assert view.user_ids == (STANDARD_ID,)


async def test_deleting_a_control_returns_the_object_to_the_uncontrolled_default(
    access: AccessServices,
) -> None:
    await control(access, KEY)

    await access.controls.delete(ADMIN, KEY, expected_digest=await digest_of(access, KEY))

    assert await access.controls.get(ADMIN, KEY) is None


async def test_deleting_the_control_of_a_stack_works_under_any_case(
    access: AccessServices,
) -> None:
    await control(access, stack_key("WebApp"))

    await access.controls.delete(
        ADMIN, stack_key("WEBAPP"), expected_digest=await digest_of(access, stack_key("webapp"))
    )

    assert await access.controls.get(ADMIN, stack_key("webapp")) is None


async def test_deleting_an_object_without_a_control_is_refused(access: AccessServices) -> None:
    with pytest.raises(ResourceControlNotFound):
        await access.controls.delete(ADMIN, KEY, expected_digest=NO_CONTROL)


async def test_releasing_a_removed_objects_control_drops_it_once(access: AccessServices) -> None:
    await control(access, stack_key("WebApp"))

    first = await access.controls.release(stack_key("WEBAPP"))
    second = await access.controls.release(stack_key("WEBAPP"))

    assert (first, second) == (True, False)


async def test_each_change_is_recorded_with_the_object_it_concerned(
    access: AccessServices, db: Session
) -> None:
    await control(access, KEY)
    await access.controls.update(
        ADMIN, KEY, PEER_SPEC, expected_digest=await digest_of(access, KEY)
    )
    await access.controls.delete(ADMIN, KEY, expected_digest=await digest_of(access, KEY))

    rows = await db.fetch_limited(
        "SELECT action, target_type, target_id, environment_id FROM activity_event "
        "WHERE category = 'user_action' ORDER BY id",
        (),
        limit=10,
    )

    assert [tuple(row) for row in rows] == [
        ("resource_control_created", "container", KEY.resource_id, ENV_A),
        ("resource_control_updated", "container", KEY.resource_id, ENV_A),
        ("resource_control_deleted", "container", KEY.resource_id, ENV_A),
    ]


async def test_refreshing_keeps_only_members_whose_own_labels_name_the_stack(
    access: AccessServices,
) -> None:
    stack = stack_key("Shop")
    await control(access, stack, public=True)
    members = [
        ResourceRef(ENV_A, "container", container_id(1), compose_labels("SHOP")),
        ResourceRef(ENV_A, "container", container_id(2), compose_labels("other")),
        ResourceRef(ENV_B, "container", container_id(3), compose_labels(STACK_NAME)),
        ResourceRef(ENV_A, "stack", "child", compose_labels(STACK_NAME)),
        ResourceRef(ENV_A, "volume", "data", swarm_labels(STACK_NAME)),
        ResourceRef(ENV_A, "network", "unlabelled", {}),
    ]

    changed = await access.controls.refresh_sub_resources(stack, members)

    view = await access.controls.get(ADMIN, stack)
    assert changed
    assert view is not None
    assert view.sub_resource_ids == (f"container:{container_id(1)}", "volume:data")


async def test_refreshing_an_uncontrolled_stack_changes_nothing(access: AccessServices) -> None:
    member = ResourceRef(ENV_A, "container", container_id(1), compose_labels(STACK_NAME))

    assert not await access.controls.refresh_sub_resources(stack_key(STACK_NAME), [member])


async def test_refreshing_the_members_of_something_that_is_not_a_stack_is_a_programming_error(
    access: AccessServices,
) -> None:
    with pytest.raises(ValueError, match="stack"):
        await access.controls.refresh_sub_resources(KEY, [])


NEW_KEY = container_key(2)


async def test_claiming_a_new_object_gives_its_creator_a_private_write_control(
    access: AccessServices,
) -> None:
    claimed = await access.controls.claim(KEY, STANDARD_ID)

    view = await access.controls.get(ADMIN, KEY)
    assert claimed is True and view is not None
    assert (view.owner_user_id, view.access_level) == (STANDARD_ID, "write")
    assert (view.is_public, view.is_administrators_only) == (False, False)
    assert (view.user_ids, view.team_ids) == ((), ())


async def test_claiming_for_administrators_only_leaves_the_object_without_an_owner(
    access: AccessServices,
) -> None:
    await access.controls.claim(KEY, None, administrators_only=True)

    view = await access.controls.get(ADMIN, KEY)
    assert view is not None
    assert (view.owner_user_id, view.is_administrators_only, view.is_public) == (None, True, False)


async def test_claiming_an_object_that_already_has_a_control_changes_nothing(
    access: AccessServices,
) -> None:
    await control(access, KEY, public=True, level="read")

    claimed = await access.controls.claim(KEY, STANDARD_ID)

    view = await access.controls.get(ADMIN, KEY)
    assert claimed is False and view is not None
    assert (view.is_public, view.access_level, view.owner_user_id) == (True, "read", None)


async def test_claiming_with_a_team_grants_it_access_at_the_chosen_level_and_never_public(
    access: AccessServices, db: Session
) -> None:
    team = await make_team(db, "platform", PEER_ID)

    claimed = await access.controls.claim(KEY, STANDARD_ID, team_ids=(team,), access_level="read")

    view = await access.controls.get(ADMIN, KEY)
    assert claimed is True and view is not None
    assert (view.owner_user_id, view.access_level, view.team_ids) == (STANDARD_ID, "read", (team,))
    assert (view.is_public, view.is_administrators_only) == (False, False)


async def test_claiming_for_a_team_that_does_not_exist_is_refused_and_leaves_no_control(
    access: AccessServices, db: Session
) -> None:
    with pytest.raises(ValidationFailed):
        async with db.transaction():
            await access.controls.claim(KEY, None, team_ids=(UNKNOWN_TEAM_ID,))

    assert await access.controls.get(ADMIN, KEY) is None


async def test_creating_with_detail_adds_it_to_the_activity_row(
    access: AccessServices, db: Session
) -> None:
    await access.controls.create(
        ADMIN, KEY, READ_SPEC, expected_digest=NO_CONTROL, detail={"onboarding": True}
    )

    [row] = [r for r in await rows_of(db) if r["action"] == "resource_control_created"]
    assert row["detail"]["onboarding"] is True


async def test_claiming_an_object_with_an_invalid_id_is_refused(access: AccessServices) -> None:
    with pytest.raises(ValidationFailed):
        await access.controls.claim(ResourceKey(ENV_A, "container", ""), STANDARD_ID)


async def test_transfer_moves_the_owner_and_grants_to_the_new_id_and_leaves_none_behind(
    access: AccessServices,
) -> None:
    await control(access, KEY, level="write", owner=STANDARD_ID, users=(PEER_ID,))

    moved = await access.controls.transfer(KEY, NEW_KEY)

    view = await access.controls.get(ADMIN, NEW_KEY)
    assert moved is True and view is not None
    assert (view.owner_user_id, view.access_level, view.user_ids) == (
        STANDARD_ID,
        "write",
        (PEER_ID,),
    )
    assert await access.controls.get(ADMIN, KEY) is None


async def test_transfer_of_an_uncontrolled_object_leaves_the_new_one_uncontrolled(
    access: AccessServices,
) -> None:
    moved = await access.controls.transfer(KEY, NEW_KEY)

    assert moved is False
    assert await access.controls.get(ADMIN, NEW_KEY) is None


async def test_transfer_onto_an_object_that_already_has_a_control_is_refused(
    access: AccessServices,
) -> None:
    await control(access, KEY, level="read", public=True)
    await control(access, NEW_KEY, level="read", public=True)

    with pytest.raises(DuplicateError):
        await access.controls.transfer(KEY, NEW_KEY)


async def test_copy_gives_the_new_id_the_same_control_and_keeps_the_old_one(
    access: AccessServices,
) -> None:
    await control(access, KEY, level="write", owner=STANDARD_ID, users=(PEER_ID,))

    copied = await access.controls.copy(KEY, NEW_KEY)

    old, new = await access.controls.get(ADMIN, KEY), await access.controls.get(ADMIN, NEW_KEY)
    assert copied is True and old is not None and new is not None
    assert (new.owner_user_id, new.access_level, new.user_ids) == (
        old.owner_user_id,
        old.access_level,
        old.user_ids,
    )


async def test_copy_of_an_uncontrolled_object_does_nothing(access: AccessServices) -> None:
    assert await access.controls.copy(KEY, NEW_KEY) is False
    assert await access.controls.get(ADMIN, NEW_KEY) is None


@pytest.mark.parametrize("hook", ["transfer", "copy"])
@pytest.mark.parametrize(
    "new",
    [
        pytest.param(container_key(2, ENV_B), id="other-environment"),
        pytest.param(volume_key("data"), id="other-type"),
        pytest.param(ResourceKey(ENV_A, "container", "web"), id="not-an-id"),
    ],
)
async def test_a_control_moves_only_between_valid_ids_of_one_type_in_one_environment(
    access: AccessServices, hook: str, new: ResourceKey
) -> None:
    with pytest.raises(ValueError, match=r"control moves only|not a valid id"):
        await getattr(access.controls, hook)(KEY, new)


SHA256_HEX_LENGTH = 64
CHANGED_MESSAGE = (
    "The access settings changed since this page was loaded. Review them and save again."
)
BASE_VIEW = ControlView(
    id=1,
    key=KEY,
    is_public=False,
    is_administrators_only=False,
    access_level="read",
    owner_user_id=None,
    user_ids=(1, 2),
    team_ids=(3, 4),
    sub_resource_ids=(),
    updated_at="2026-01-02T03:04:05Z",
)


async def failure_rows(db: Session, action: str) -> list[dict[str, Any]]:
    return [row for row in await rows_of(db, action) if row["result"] == "failure"]


def test_the_digest_of_no_control_is_empty() -> None:
    assert control_digest(None) == ""


def test_the_digest_of_a_control_is_a_sha256_hex_string() -> None:
    digest = control_digest(BASE_VIEW)

    assert len(digest) == SHA256_HEX_LENGTH
    assert int(digest, 16) >= 0


@pytest.mark.parametrize(
    "same",
    [
        replace(BASE_VIEW, user_ids=(2, 1)),
        replace(BASE_VIEW, team_ids=(4, 3)),
        replace(BASE_VIEW, id=99, updated_at="2027-01-01T00:00:00Z"),
        replace(BASE_VIEW, sub_resource_ids=("container:abc",)),
    ],
    ids=["user-order", "team-order", "row-identity-and-time", "display-cache"],
)
def test_the_digest_ignores_the_order_of_ids_and_what_a_form_does_not_show(
    same: ControlView,
) -> None:
    assert control_digest(same) == control_digest(BASE_VIEW)


@pytest.mark.parametrize(
    "different",
    [
        replace(BASE_VIEW, is_public=True),
        replace(BASE_VIEW, is_administrators_only=True),
        replace(BASE_VIEW, access_level="write"),
        replace(BASE_VIEW, owner_user_id=1),
        replace(BASE_VIEW, user_ids=(1,)),
        replace(BASE_VIEW, team_ids=(3,)),
        replace(BASE_VIEW, user_ids=(3, 4), team_ids=(1, 2)),
    ],
    ids=[
        "public",
        "administrators-only",
        "level",
        "owner",
        "users",
        "teams",
        "users-and-teams-swapped",
    ],
)
def test_the_digest_changes_with_every_field_a_form_shows(different: ControlView) -> None:
    assert control_digest(different) != control_digest(BASE_VIEW)


@pytest.mark.parametrize(
    ("operation", "action"),
    [
        (
            lambda access: access.controls.create(
                ADMIN, KEY, READ_SPEC, expected_digest=STALE_DIGEST
            ),
            "resource_control_created",
        ),
        (
            lambda access: access.controls.update(
                ADMIN, KEY, PEER_SPEC, expected_digest=STALE_DIGEST
            ),
            "resource_control_updated",
        ),
        (
            lambda access: access.controls.delete(ADMIN, KEY, expected_digest=STALE_DIGEST),
            "resource_control_deleted",
        ),
    ],
    ids=["create", "update", "delete"],
)
async def test_a_stale_digest_changes_nothing_and_leaves_a_failure_row_with_its_reason(
    access: AccessServices, db: Session, operation: Operation, action: str
) -> None:
    before = await control(access, KEY, users=(STANDARD_ID,))

    with pytest.raises(ControlChanged) as refused:
        await operation(access)

    after = await access.controls.get(ADMIN, KEY)
    [failure] = await failure_rows(db, action)
    assert refused.value.fields == {"resource": CHANGED_MESSAGE}
    assert after == before
    assert (failure["result"], failure["detail"]["reason"]) == ("failure", CHANGED_REASON)


async def test_creating_with_a_digest_taken_before_the_control_was_removed_is_refused(
    access: AccessServices,
) -> None:
    await control(access, KEY)
    reviewed = await digest_of(access, KEY)
    await access.controls.delete(ADMIN, KEY, expected_digest=reviewed)

    with pytest.raises(ControlChanged):
        await access.controls.create(ADMIN, KEY, READ_SPEC, expected_digest=reviewed)

    assert await access.controls.get(ADMIN, KEY) is None


async def test_a_stale_digest_is_refused_before_a_bad_access_level_is_looked_at_on_create(
    access: AccessServices,
) -> None:
    await control(access, KEY)
    invalid = ControlSpec(access_level="admin")

    with pytest.raises(ControlChanged):
        await access.controls.create(ADMIN, KEY, invalid, expected_digest=STALE_DIGEST)


async def test_a_stale_digest_is_refused_before_a_bad_access_level_is_looked_at_on_update(
    access: AccessServices,
) -> None:
    await control(access, KEY)
    invalid = ControlSpec(access_level="admin")

    with pytest.raises(ControlChanged):
        await access.controls.update(ADMIN, KEY, invalid, expected_digest=STALE_DIGEST)


async def test_a_current_digest_lets_a_bad_access_level_through_to_field_validation(
    access: AccessServices,
) -> None:
    await control(access, KEY)
    invalid = ControlSpec(access_level="admin")

    with pytest.raises(ValidationFailed) as refused:
        await access.controls.update(
            ADMIN, KEY, invalid, expected_digest=await digest_of(access, KEY)
        )

    assert not isinstance(refused.value, ControlChanged)
    assert "access_level" in refused.value.fields
