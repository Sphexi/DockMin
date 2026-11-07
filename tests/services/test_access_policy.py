"""The instance-wide default for objects that have no resource control."""

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.db.engine import Session
from app.db.repositories.setting import SettingRepository
from app.services.access import ResourceKey
from app.services.access_policy import UNCONTROLLED_DEFAULT_KEY, UncontrolledDefault
from app.services.assembly import AccessServices
from tests.access_support import ADMIN, STANDARD, FakeInspector, ListedObject, container_key
from tests.auth_support import FakeClock


@pytest.fixture
def uncontrolled(inspector: FakeInspector) -> ResourceKey:
    """A container Docker reports that no control governs."""
    key = container_key(1)
    inspector.add(key)
    return key


async def test_the_default_is_administrators_only_until_an_administrator_changes_it(
    access: AccessServices,
) -> None:
    assert await access.policy.uncontrolled_default_setting() == "administrators_only"
    assert await access.policy.uncontrolled_default() is None


async def test_under_the_default_a_standard_user_sees_no_uncontrolled_object(
    access: AccessServices, uncontrolled: ResourceKey
) -> None:
    visible = await access.authoriser.filter_visible(STANDARD, [ListedObject.of(uncontrolled)])

    assert visible == []


@pytest.mark.parametrize("chosen", ["read", "write"])
async def test_a_permissive_default_lets_a_standard_user_see_an_uncontrolled_object(
    access: AccessServices, uncontrolled: ResourceKey, chosen: UncontrolledDefault
) -> None:
    await access.policy.set_uncontrolled_default(ADMIN, chosen)
    listing = [ListedObject.of(uncontrolled)]

    visible = await access.authoriser.filter_visible(STANDARD, listing)

    assert visible == listing


async def test_a_read_default_does_not_permit_a_mutation_of_an_uncontrolled_object(
    access: AccessServices, uncontrolled: ResourceKey
) -> None:
    await access.policy.set_uncontrolled_default(ADMIN, "read")

    with pytest.raises(PermissionDenied):
        await access.authoriser.require_access(STANDARD, uncontrolled, "write")


async def test_a_write_default_permits_a_mutation_of_an_uncontrolled_object(
    access: AccessServices, uncontrolled: ResourceKey
) -> None:
    await access.policy.set_uncontrolled_default(ADMIN, "write")

    await access.authoriser.require_access(STANDARD, uncontrolled, "write")


async def test_returning_to_administrators_only_hides_the_object_again(
    access: AccessServices, uncontrolled: ResourceKey
) -> None:
    await access.policy.set_uncontrolled_default(ADMIN, "read")
    await access.policy.set_uncontrolled_default(ADMIN, "administrators_only")

    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_access(STANDARD, uncontrolled)


@pytest.mark.parametrize("stored", ["WRITE", "everyone", ""])
async def test_an_unrecognised_stored_value_falls_back_to_administrators_only(
    access: AccessServices, db: Session, clock: FakeClock, stored: str
) -> None:
    await SettingRepository(db, clock).put(UNCONTROLLED_DEFAULT_KEY, "string", stored)

    assert await access.policy.uncontrolled_default() is None


async def test_a_standard_user_cannot_change_the_default(access: AccessServices) -> None:
    with pytest.raises(PermissionDenied):
        await access.policy.set_uncontrolled_default(STANDARD, "write")


async def test_an_unknown_choice_is_rejected_and_the_default_is_unchanged(
    access: AccessServices,
) -> None:
    with pytest.raises(ValidationFailed):
        await access.policy.set_uncontrolled_default(ADMIN, "everyone")

    assert await access.policy.uncontrolled_default_setting() == "administrators_only"


@pytest.mark.parametrize("chosen", ["read", "write"])
async def test_a_permissive_default_does_not_open_an_environment_the_user_holds_no_grant_on(
    access: AccessServices, uncontrolled: ResourceKey, no_grants: None, chosen: UncontrolledDefault
) -> None:
    await access.policy.set_uncontrolled_default(ADMIN, chosen)

    with pytest.raises(ResourceNotFound):
        await access.authoriser.require_access(STANDARD, uncontrolled)
    visible = await access.authoriser.filter_visible(STANDARD, [ListedObject.of(uncontrolled)])

    assert visible == []
