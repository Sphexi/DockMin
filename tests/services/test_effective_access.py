"""The effective-access view: what a user can reach, and the reason for each answer."""

import pytest

from app.auth.errors import PermissionDenied, UserNotFound
from app.services.access import Reason, ResourceKey
from app.services.assembly import AccessServices
from app.services.effective_access import EffectiveAccess
from tests.access_support import (
    ADMIN,
    ENV_A,
    PEER_ID,
    STACK_NAME,
    STANDARD,
    TEAM_NAME,
    UNKNOWN_USER_ID,
    FakeInspector,
    ListedObject,
    compose_labels,
    container_key,
    control,
    stack_key,
    team_of,
)
from tests.auth_support import ADMIN_ID, STANDARD_ID, STANDARD_USERNAME

Explained = tuple[str | None, tuple[Reason, ...], tuple[str, ...], str | None]

USER_GRANTED = container_key(1)
TEAM_GRANTED = container_key(2)
PUBLIC = container_key(3)
OWNED = container_key(4)
INHERITED = container_key(5)
ADMINISTRATORS_ONLY = container_key(6)
NOT_GRANTED = container_key(7)
UNCONTROLLED = container_key(8)
STACK_LABELS = compose_labels(STACK_NAME)

EXPECTED: dict[ResourceKey, Explained] = {
    USER_GRANTED: ("read", (Reason.USER_GRANT,), (), None),
    TEAM_GRANTED: ("read", (Reason.TEAM_GRANT,), (TEAM_NAME,), None),
    PUBLIC: ("read", (Reason.PUBLIC,), (), None),
    OWNED: ("read", (Reason.OWNER,), (), None),
    INHERITED: ("read", (Reason.USER_GRANT,), (), STACK_NAME),
    ADMINISTRATORS_ONLY: (None, (Reason.ADMINISTRATORS_ONLY,), (), None),
    NOT_GRANTED: (None, (Reason.NOT_GRANTED,), (), None),
    UNCONTROLLED: (None, (Reason.UNCONTROLLED_DENIED,), (), None),
}


@pytest.fixture
async def listing(access: AccessServices, inspector: FakeInspector) -> list[ListedObject]:
    """One container per way of being reachable or refused by STANDARD."""
    team_id = await team_of(access, TEAM_NAME, STANDARD)
    await control(access, USER_GRANTED, users=(STANDARD_ID,))
    await control(access, TEAM_GRANTED, teams=(team_id,))
    await control(access, PUBLIC, public=True)
    await control(access, OWNED, owner=STANDARD_ID)
    await control(access, stack_key(STACK_NAME), users=(STANDARD_ID,))
    await control(access, ADMINISTRATORS_ONLY, administrators_only=True)
    await control(access, NOT_GRANTED, users=(PEER_ID,))
    labelled = {INHERITED: STACK_LABELS}
    for key in EXPECTED:
        inspector.add(key, labelled.get(key))
    return [ListedObject.of(key, labelled.get(key)) for key in EXPECTED]


def reasons_by_object(result: EffectiveAccess) -> dict[str, Explained]:
    return {
        entry.resource_id: (entry.level, entry.reasons, entry.via_teams, entry.inherited_from)
        for entry in result.entries
    }


async def test_each_object_is_explained_by_the_reason_the_user_can_or_cannot_reach_it(
    access: AccessServices, listing: list[ListedObject]
) -> None:
    result = await access.effective.explain(ADMIN, STANDARD_ID, ENV_A, "container", listing)

    assert reasons_by_object(result) == {key.resource_id: row for key, row in EXPECTED.items()}


async def test_the_explanation_names_the_account_and_the_default_in_force(
    access: AccessServices, listing: list[ListedObject]
) -> None:
    result = await access.effective.explain(ADMIN, STANDARD_ID, ENV_A, "container", listing)

    assert (result.username, result.role, result.is_enabled, result.uncontrolled_default) == (
        STANDARD_USERNAME,
        "standard",
        True,
        "administrators_only",
    )


async def test_the_explanation_names_a_permissive_default_as_the_reason_for_an_uncontrolled_object(
    access: AccessServices, listing: list[ListedObject]
) -> None:
    await access.policy.set_uncontrolled_default(ADMIN, "read")

    result = await access.effective.explain(
        ADMIN, STANDARD_ID, ENV_A, "container", [ListedObject.of(UNCONTROLLED)]
    )

    assert reasons_by_object(result) == {
        UNCONTROLLED.resource_id: ("read", (Reason.UNCONTROLLED_DEFAULT,), (), None)
    }


async def test_the_explanation_agrees_with_what_enforcement_lets_the_user_see(
    access: AccessServices, listing: list[ListedObject]
) -> None:
    result = await access.effective.explain(ADMIN, STANDARD_ID, ENV_A, "container", listing)

    visible = await access.authoriser.filter_visible(STANDARD, listing)

    explained_visible = [entry.resource_id for entry in result.entries if entry.level is not None]
    assert explained_visible == [item.resource_id for item in visible]


async def test_an_administrator_subject_is_explained_by_the_role_alone(
    access: AccessServices, listing: list[ListedObject]
) -> None:
    result = await access.effective.explain(ADMIN, ADMIN_ID, ENV_A, "container", listing[:1])

    assert reasons_by_object(result) == {
        USER_GRANTED.resource_id: ("write", (Reason.ADMINISTRATOR,), (), None)
    }


async def test_a_standard_user_cannot_ask_for_the_explanation_even_about_themselves(
    access: AccessServices, listing: list[ListedObject]
) -> None:
    with pytest.raises(PermissionDenied):
        await access.effective.explain(STANDARD, STANDARD_ID, ENV_A, "container", listing)


async def test_explaining_a_user_who_does_not_exist_is_not_found(
    access: AccessServices, listing: list[ListedObject]
) -> None:
    with pytest.raises(UserNotFound):
        await access.effective.explain(ADMIN, UNKNOWN_USER_ID, ENV_A, "container", listing)
