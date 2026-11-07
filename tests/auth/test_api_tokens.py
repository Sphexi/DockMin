"""API tokens: issued once, stored as a hash, resolved to the principal of their owner."""

import json
import re
from datetime import timedelta

import pytest

from app.auth.errors import PermissionDenied, TokenNotFound, ValidationFailed
from app.auth.principal import Principal
from app.auth.services import AuthServices
from app.auth.tokens import LABEL_MAX, TOUCH_INTERVAL, IssuedToken, hash_token
from app.db.engine import Session
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_USERNAME,
    CLIENT_ADDRESS,
    STANDARD_ID,
    STANDARD_USERNAME,
    FakeClock,
    read_events,
)

TOKEN_SHAPE = re.compile(r"dkm_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{43}")
TOKEN_MARK_PREFIX = "dkm_"
DISPLAY_PREFIX_LENGTH = len(TOKEN_MARK_PREFIX) + 8
SECRET_LENGTH = 43
PAGE = 50
LABEL = "ci"
LIFETIME = timedelta(hours=1)
UNKNOWN_TOKEN_ID = 9999


async def issue(
    services: AuthServices,
    actor: Principal,
    label: str = LABEL,
    *,
    lifetime: timedelta | None = None,
    clock: FakeClock | None = None,
) -> IssuedToken:
    expires = None if lifetime is None or clock is None else clock.now + lifetime
    return await services.tokens.create(
        actor, label=label, expires_at=expires, remote_address=CLIENT_ADDRESS
    )


async def test_an_issued_token_has_the_documented_shape(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)

    assert TOKEN_SHAPE.fullmatch(issued.value)


async def test_two_tokens_never_share_a_value_or_a_prefix(
    services: AuthServices, standard_user: Principal
) -> None:
    first = await issue(services, standard_user, "first")
    second = await issue(services, standard_user, "second")

    assert first.value != second.value
    assert first.record.display_prefix != second.record.display_prefix


async def test_the_value_is_kept_out_of_the_issued_tokens_repr(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)

    assert issued.value not in repr(issued)


async def test_the_record_shows_a_prefix_that_identifies_the_token_without_revealing_it(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)

    assert issued.record.display_prefix == issued.value[:DISPLAY_PREFIX_LENGTH]
    assert issued.value[DISPLAY_PREFIX_LENGTH:] not in repr(issued.record)


async def test_only_a_hash_of_the_value_is_stored(
    services: AuthServices, db: Session, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)

    row = await db.fetch_one("SELECT * FROM api_token WHERE user_id = ?", (STANDARD_ID,))

    assert row is not None
    assert row["token_hash"] == hash_token(issued.value)
    assert issued.value not in repr(tuple(row))


async def test_listing_tokens_afterwards_cannot_reproduce_the_value(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)

    page = await services.tokens.list_page(standard_user, limit=PAGE, cursor=None)

    assert [item.id for item in page.items] == [issued.record.id]
    assert issued.value not in repr(page.items)
    assert issued.value[-SECRET_LENGTH:] not in repr(page.items)


async def test_issuing_a_token_records_an_event_without_the_value(
    services: AuthServices, db: Session, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)

    events = await read_events(db)

    created = [event for event in events if event["action"] == "token_created"]
    assert [event["detail"]["prefix"] for event in created] == [
        issued.record.display_prefix.removeprefix(TOKEN_MARK_PREFIX)
    ]
    assert issued.value[-SECRET_LENGTH:] not in json.dumps(events)


async def test_a_token_authenticates_as_the_principal_of_its_owner(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)

    principal = await services.tokens.resolve(issued.value)

    assert principal == Principal(STANDARD_ID, STANDARD_USERNAME, "standard", None)


async def test_a_token_of_an_administrator_carries_the_administrator_role(
    services: AuthServices, administrator: Principal
) -> None:
    issued = await issue(services, administrator)

    principal = await services.tokens.resolve(issued.value)

    assert principal == Principal(ADMIN_ID, ADMIN_USERNAME, "administrator", None)


async def test_a_token_follows_its_owners_role_as_it_changes(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)
    await services.users.set_role(STANDARD_ID, "administrator")

    principal = await services.tokens.resolve(issued.value)

    assert principal is not None
    assert principal.is_administrator


async def test_a_revoked_token_stops_working_at_once(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)
    assert await services.tokens.resolve(issued.value) is not None

    await services.tokens.revoke(standard_user, issued.record.id, remote_address=CLIENT_ADDRESS)

    assert await services.tokens.resolve(issued.value) is None


async def test_revoking_a_token_twice_is_harmless(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)
    await services.tokens.revoke(standard_user, issued.record.id, remote_address=CLIENT_ADDRESS)

    await services.tokens.revoke(standard_user, issued.record.id, remote_address=CLIENT_ADDRESS)

    assert await services.tokens.resolve(issued.value) is None


async def test_a_user_cannot_revoke_a_token_that_belongs_to_someone_else(
    services: AuthServices, administrator: Principal, standard_user: Principal
) -> None:
    issued = await issue(services, administrator)

    with pytest.raises(TokenNotFound):
        await services.tokens.revoke(standard_user, issued.record.id, remote_address=CLIENT_ADDRESS)

    assert await services.tokens.resolve(issued.value) is not None


async def test_revoking_a_token_that_does_not_exist_is_not_found(
    services: AuthServices, standard_user: Principal
) -> None:
    with pytest.raises(TokenNotFound):
        await services.tokens.revoke(standard_user, UNKNOWN_TOKEN_ID, remote_address=CLIENT_ADDRESS)


async def test_an_administrator_can_revoke_another_users_token(
    services: AuthServices, administrator: Principal, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)

    await services.tokens.revoke(administrator, issued.record.id, remote_address=CLIENT_ADDRESS)

    assert await services.tokens.resolve(issued.value) is None


async def test_a_token_works_until_the_instant_it_expires(
    services: AuthServices, clock: FakeClock, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user, lifetime=LIFETIME, clock=clock)
    clock.advance(LIFETIME - timedelta(seconds=1))

    assert await services.tokens.resolve(issued.value) is not None


async def test_a_token_stops_working_when_it_expires(
    services: AuthServices, clock: FakeClock, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user, lifetime=LIFETIME, clock=clock)
    clock.advance(LIFETIME)

    assert await services.tokens.resolve(issued.value) is None


async def test_a_token_without_an_expiry_does_not_expire(
    services: AuthServices, clock: FakeClock, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)
    clock.advance(timedelta(days=3650))

    assert await services.tokens.resolve(issued.value) is not None


async def test_an_expiry_that_is_not_in_the_future_is_refused(
    services: AuthServices, clock: FakeClock, standard_user: Principal
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await issue(services, standard_user, lifetime=timedelta(0), clock=clock)

    assert "expires_at" in refused.value.fields


async def test_an_expiry_without_a_time_zone_is_refused(
    services: AuthServices, clock: FakeClock, standard_user: Principal
) -> None:
    naive = (clock.now + LIFETIME).replace(tzinfo=None)

    with pytest.raises(ValidationFailed):
        await services.tokens.create(
            standard_user, label=LABEL, expires_at=naive, remote_address=CLIENT_ADDRESS
        )


async def test_a_token_of_a_disabled_user_stops_working(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)
    await services.users.disable(STANDARD_ID)

    assert await services.tokens.resolve(issued.value) is None


async def test_a_token_whose_secret_is_wrong_is_refused_though_its_prefix_exists(
    services: AuthServices, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)
    forged = issued.value[:-SECRET_LENGTH] + "A" * SECRET_LENGTH

    assert await services.tokens.resolve(forged) is None


@pytest.mark.parametrize(
    "presented", ["", "dkm", "not-a-token", "dkm_short_" + "A" * 43, "dkm_AAAAAAAA_" + "A" * 42]
)
async def test_a_value_that_is_not_shaped_like_a_token_is_refused(
    services: AuthServices, presented: str
) -> None:
    assert await services.tokens.resolve(presented) is None


async def test_a_well_formed_token_nobody_issued_is_refused(services: AuthServices) -> None:
    assert await services.tokens.resolve("dkm_AAAAAAAA_" + "A" * SECRET_LENGTH) is None


async def test_a_label_already_used_by_the_user_is_refused_even_after_revocation(
    services: AuthServices, standard_user: Principal
) -> None:
    first = await issue(services, standard_user)
    await services.tokens.revoke(standard_user, first.record.id, remote_address=CLIENT_ADDRESS)

    with pytest.raises(ValidationFailed) as refused:
        await issue(services, standard_user)

    assert "label" in refused.value.fields


@pytest.mark.parametrize("label", ["", "   ", "x" * (LABEL_MAX + 1), "bad\x00label"])
async def test_a_label_that_is_blank_too_long_or_unprintable_is_refused(
    services: AuthServices, standard_user: Principal, label: str
) -> None:
    with pytest.raises(ValidationFailed):
        await issue(services, standard_user, label)


async def test_a_user_lists_their_own_tokens_newest_first(
    services: AuthServices, standard_user: Principal
) -> None:
    await issue(services, standard_user, "older")
    await issue(services, standard_user, "newer")

    page = await services.tokens.list_page(standard_user, limit=PAGE, cursor=None)

    assert [item.label for item in page.items] == ["newer", "older"]


async def test_a_standard_user_cannot_list_another_users_tokens(
    services: AuthServices, standard_user: Principal
) -> None:
    with pytest.raises(PermissionDenied):
        await services.tokens.list_page(standard_user, user_id=ADMIN_ID, limit=PAGE, cursor=None)


async def test_an_administrator_can_list_another_users_tokens(
    services: AuthServices, administrator: Principal, standard_user: Principal
) -> None:
    await issue(services, standard_user)

    page = await services.tokens.list_page(
        administrator, user_id=STANDARD_ID, limit=PAGE, cursor=None
    )

    assert [item.label for item in page.items] == [LABEL]


async def test_using_a_token_records_when_it_was_last_used(
    services: AuthServices, clock: FakeClock, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)
    clock.advance(timedelta(hours=1))
    used_at = clock.now

    await services.tokens.resolve(issued.value)

    page = await services.tokens.list_page(standard_user, limit=PAGE, cursor=None)
    assert page.items[0].last_used_at == used_at.strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_use_is_not_rewritten_inside_the_touch_interval(
    services: AuthServices, clock: FakeClock, standard_user: Principal
) -> None:
    issued = await issue(services, standard_user)
    await services.tokens.resolve(issued.value)
    first = (await services.tokens.list_page(standard_user, limit=PAGE, cursor=None)).items[0]
    clock.advance(TOUCH_INTERVAL - timedelta(seconds=1))

    await services.tokens.resolve(issued.value)

    page = await services.tokens.list_page(standard_user, limit=PAGE, cursor=None)
    assert page.items[0].last_used_at == first.last_used_at
