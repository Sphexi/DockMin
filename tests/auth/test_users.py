"""Tests for app/auth/users.py, run against a real database."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.auth.errors import (
    LastAdministratorError,
    PermissionDenied,
    UserHasActivity,
    ValidationFailed,
)
from app.auth.principal import PasswordCredentials, Principal
from app.auth.services import AuthServices
from app.db.engine import Session
from app.db.repositories.clocked import format_timestamp
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    CLIENT_ADDRESS,
    NEW_PASSWORD,
    STANDARD_ID,
    STANDARD_PASSWORD,
    STANDARD_USERNAME,
    WEAK_PASSWORD,
    FakeClock,
    read_categories,
    read_events,
)

AGENT = "test-agent"
SECOND_ADMIN = "second-admin"
NEW_USERNAME = "new-person"
LAST_ADMIN_MESSAGE = "last administrator"
CREATED_ID = 3
RENAMED = "renamed-person"
ADDRESS_DETAIL = {"address": CLIENT_ADDRESS}

Operation = Callable[[AuthServices, Principal], Awaitable[object]]


async def _create(services: AuthServices, actor: Principal) -> object:
    return await services.user_admin.create(
        actor,
        username=NEW_USERNAME,
        password=NEW_PASSWORD,
        role="standard",
        remote_address=CLIENT_ADDRESS,
    )


async def _promote(services: AuthServices, actor: Principal) -> object:
    return await services.user_admin.update(
        actor,
        STANDARD_ID,
        username=STANDARD_USERNAME,
        role="administrator",
        remote_address=CLIENT_ADDRESS,
    )


async def _rename(services: AuthServices, actor: Principal) -> object:
    return await services.user_admin.update(
        actor, STANDARD_ID, username=RENAMED, role="standard", remote_address=CLIENT_ADDRESS
    )


async def _disable(services: AuthServices, actor: Principal) -> object:
    return await services.user_admin.set_enabled(
        actor, STANDARD_ID, enabled=False, remote_address=CLIENT_ADDRESS
    )


async def _enable(services: AuthServices, actor: Principal) -> object:
    await services.users.disable(STANDARD_ID)
    return await services.user_admin.set_enabled(
        actor, STANDARD_ID, enabled=True, remote_address=CLIENT_ADDRESS
    )


async def _delete(services: AuthServices, actor: Principal) -> None:
    await services.user_admin.delete(
        actor, STANDARD_ID, expected_name=STANDARD_USERNAME, remote_address=CLIENT_ADDRESS
    )


async def _update_profile(services: AuthServices, actor: Principal) -> object:
    return await services.user_admin.update_profile(
        Principal(STANDARD_ID, STANDARD_USERNAME, "standard"),
        username=RENAMED,
        remote_address=CLIENT_ADDRESS,
    )


async def _clear_lockout(services: AuthServices, actor: Principal) -> object:
    return await services.user_admin.clear_lockout(
        actor, STANDARD_ID, remote_address=CLIENT_ADDRESS
    )


AUDITED_OPERATIONS = [
    pytest.param(
        _create,
        ("user_created", ADMIN_ID, str(CREATED_ID), ADDRESS_DETAIL | {"role": "standard"}),
        id="create",
    ),
    pytest.param(
        _promote,
        (
            "user_updated",
            ADMIN_ID,
            str(STANDARD_ID),
            ADDRESS_DETAIL | {"old_role": "standard", "new_role": "administrator"},
        ),
        id="update-with-a-role-change",
    ),
    pytest.param(
        _rename, ("user_updated", ADMIN_ID, str(STANDARD_ID), ADDRESS_DETAIL), id="update-rename"
    ),
    pytest.param(
        _disable, ("user_disabled", ADMIN_ID, str(STANDARD_ID), ADDRESS_DETAIL), id="disable"
    ),
    pytest.param(
        _enable, ("user_enabled", ADMIN_ID, str(STANDARD_ID), ADDRESS_DETAIL), id="enable"
    ),
    pytest.param(
        _delete, ("user_deleted", ADMIN_ID, str(STANDARD_ID), ADDRESS_DETAIL), id="delete"
    ),
    pytest.param(
        _update_profile,
        ("profile_updated", STANDARD_ID, str(STANDARD_ID), ADDRESS_DETAIL),
        id="profile-update",
    ),
    pytest.param(
        _clear_lockout,
        ("lockout_cleared", ADMIN_ID, str(STANDARD_ID), ADDRESS_DETAIL),
        id="lockout-cleared",
    ),
]


async def _second_administrator(services: AuthServices, actor: Principal) -> int:
    created = await services.user_admin.create(
        actor,
        username=SECOND_ADMIN,
        password=NEW_PASSWORD,
        role="administrator",
        remote_address=CLIENT_ADDRESS,
    )
    return created.id


async def _issue(services: AuthServices, user_id: int) -> str:
    return (await services.sessions.issue(user_id, user_agent=AGENT, remote_address=None)).token


async def test_disabling_the_only_administrator_is_refused_with_a_clear_message(
    services: AuthServices, administrator: Principal
) -> None:
    with pytest.raises(LastAdministratorError, match=LAST_ADMIN_MESSAGE):
        await services.user_admin.set_enabled(
            administrator, ADMIN_ID, enabled=False, remote_address=CLIENT_ADDRESS
        )

    user = await services.users.get(ADMIN_ID)
    assert user is not None
    assert user.is_enabled is True


async def test_demoting_the_only_administrator_is_refused_with_a_clear_message(
    services: AuthServices, administrator: Principal
) -> None:
    with pytest.raises(LastAdministratorError, match=LAST_ADMIN_MESSAGE):
        await services.user_admin.update(
            administrator,
            ADMIN_ID,
            username=ADMIN_USERNAME,
            role="standard",
            remote_address=CLIENT_ADDRESS,
        )

    user = await services.users.get(ADMIN_ID)
    assert user is not None
    assert user.role == "administrator"


async def test_deleting_the_only_administrator_is_refused_with_a_clear_message(
    services: AuthServices, administrator: Principal
) -> None:
    with pytest.raises(LastAdministratorError, match=LAST_ADMIN_MESSAGE):
        await services.user_admin.delete(
            administrator, ADMIN_ID, expected_name=ADMIN_USERNAME, remote_address=CLIENT_ADDRESS
        )

    assert await services.users.get(ADMIN_ID) is not None


async def test_a_disabled_administrator_does_not_count_as_one_who_can_sign_in(
    services: AuthServices, administrator: Principal
) -> None:
    second_id = await _second_administrator(services, administrator)
    await services.user_admin.set_enabled(
        administrator, second_id, enabled=False, remote_address=CLIENT_ADDRESS
    )

    with pytest.raises(LastAdministratorError):
        await services.user_admin.set_enabled(
            administrator, ADMIN_ID, enabled=False, remote_address=CLIENT_ADDRESS
        )


async def test_an_administrator_can_be_demoted_while_another_can_still_sign_in(
    services: AuthServices, administrator: Principal
) -> None:
    await _second_administrator(services, administrator)

    updated = await services.user_admin.update(
        administrator,
        ADMIN_ID,
        username=ADMIN_USERNAME,
        role="standard",
        remote_address=CLIENT_ADDRESS,
    )

    assert updated.role == "standard"


async def test_a_user_with_recorded_activity_cannot_be_deleted_only_disabled(
    services: AuthServices, administrator: Principal, db: Session
) -> None:
    await services.login.login(
        "internal",
        PasswordCredentials(STANDARD_USERNAME, STANDARD_PASSWORD),
        remote_address=CLIENT_ADDRESS,
        user_agent=AGENT,
    )

    with pytest.raises(UserHasActivity, match="Disable the account"):
        await services.user_admin.delete(
            administrator,
            STANDARD_ID,
            expected_name=STANDARD_USERNAME,
            remote_address=CLIENT_ADDRESS,
        )

    events = await read_events(db)
    assert await services.users.get(STANDARD_ID) is not None
    assert [(e["action"], e["result"]) for e in events] == [
        ("login", "success"),
        ("user_deleted", "failure"),
    ]
    assert events[1]["detail"]["reason"] == "UserHasActivity"
    assert await read_categories(db) == ["authentication", "user_action"]


async def test_the_service_refuses_an_administrator_operation_from_a_standard_user(
    services: AuthServices, standard_user: Principal
) -> None:
    with pytest.raises(PermissionDenied):
        await services.user_admin.create(
            standard_user,
            username=NEW_USERNAME,
            password=NEW_PASSWORD,
            role="standard",
            remote_address=CLIENT_ADDRESS,
        )


async def test_create_refuses_a_password_below_the_policy_and_creates_nobody(
    services: AuthServices, administrator: Principal
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await services.user_admin.create(
            administrator,
            username=NEW_USERNAME,
            password=WEAK_PASSWORD,
            role="standard",
            remote_address=CLIENT_ADDRESS,
        )

    assert set(raised.value.fields) == {"password"}
    assert await services.users.get_by_username(NEW_USERNAME) is None


async def test_create_refuses_a_password_equal_to_the_username(
    services: AuthServices, administrator: Principal
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await services.user_admin.create(
            administrator,
            username=NEW_USERNAME,
            password=NEW_USERNAME,
            role="standard",
            remote_address=CLIENT_ADDRESS,
        )

    assert "same as the username" in raised.value.fields["password"]


async def test_reset_refuses_a_password_below_the_policy_and_keeps_sessions_and_hash(
    services: AuthServices, administrator: Principal
) -> None:
    token = await _issue(services, STANDARD_ID)
    before = await services.users.get(STANDARD_ID)
    assert before is not None

    with pytest.raises(ValidationFailed) as raised:
        await services.user_admin.reset_password(
            administrator, STANDARD_ID, new_password=WEAK_PASSWORD, remote_address=CLIENT_ADDRESS
        )

    after = await services.users.get(STANDARD_ID)
    assert after is not None
    assert set(raised.value.fields) == {"new_password"}
    assert after.password_hash == before.password_hash
    assert await services.sessions.resolve(token) is not None


async def test_change_refuses_a_new_password_below_the_policy(
    services: AuthServices, administrator: Principal
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await services.user_admin.change_password(
            administrator,
            current_password=ADMIN_PASSWORD,
            new_password=WEAK_PASSWORD,
            new_password_confirm=WEAK_PASSWORD,
            remote_address=CLIENT_ADDRESS,
        )

    assert set(raised.value.fields) == {"new_password"}


async def test_change_refuses_a_wrong_current_password_and_records_the_failure(
    services: AuthServices, administrator: Principal, db: Session
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await services.user_admin.change_password(
            administrator,
            current_password=NEW_PASSWORD,
            new_password=NEW_PASSWORD,
            new_password_confirm=NEW_PASSWORD,
            remote_address=CLIENT_ADDRESS,
        )

    [event] = await read_events(db)
    assert set(raised.value.fields) == {"current_password"}
    assert (event["action"], event["result"]) == ("password_change", "failure")
    assert event["detail"] == {
        "address": CLIENT_ADDRESS,
        "reason": "bad_current_password",
        "message": "The current password is incorrect.",
    }


async def test_disabling_a_user_ends_all_of_their_sessions(
    services: AuthServices, administrator: Principal
) -> None:
    first = await _issue(services, STANDARD_ID)
    second = await _issue(services, STANDARD_ID)

    await services.user_admin.set_enabled(
        administrator, STANDARD_ID, enabled=False, remote_address=CLIENT_ADDRESS
    )

    assert await services.sessions.count_for_user(STANDARD_ID) == 0
    assert await services.sessions.resolve(first) is None
    assert await services.sessions.resolve(second) is None


async def test_an_administrator_reset_ends_the_users_sessions_and_installs_the_new_password(
    services: AuthServices, administrator: Principal, db: Session
) -> None:
    token = await _issue(services, STANDARD_ID)

    await services.user_admin.reset_password(
        administrator, STANDARD_ID, new_password=NEW_PASSWORD, remote_address=CLIENT_ADDRESS
    )

    user = await services.users.get(STANDARD_ID)
    events = await read_events(db)
    assert user is not None
    assert await services.sessions.resolve(token) is None
    assert await services.login.login(
        "internal",
        PasswordCredentials(STANDARD_USERNAME, NEW_PASSWORD),
        remote_address=CLIENT_ADDRESS,
        user_agent=AGENT,
    )
    assert (events[0]["action"], events[0]["actor_user_id"]) == ("password_reset", ADMIN_ID)
    assert events[0]["target_id"] == str(STANDARD_ID)


async def test_a_self_service_change_ends_the_other_sessions_and_spares_the_current_one(
    services: AuthServices,
) -> None:
    current = await services.sessions.issue(STANDARD_ID, user_agent=AGENT, remote_address=None)
    other = await _issue(services, STANDARD_ID)
    actor = Principal(STANDARD_ID, STANDARD_USERNAME, "standard", current.session_id)

    await services.user_admin.change_password(
        actor,
        current_password=STANDARD_PASSWORD,
        new_password=NEW_PASSWORD,
        new_password_confirm=NEW_PASSWORD,
        remote_address=CLIENT_ADDRESS,
    )

    assert await services.sessions.resolve(current.token) is not None
    assert await services.sessions.resolve(other) is None


async def test_revoking_sessions_ends_every_session_the_user_has(
    services: AuthServices, administrator: Principal
) -> None:
    await _issue(services, STANDARD_ID)
    await _issue(services, STANDARD_ID)

    ended = await services.user_admin.revoke_sessions(
        administrator, STANDARD_ID, remote_address=CLIENT_ADDRESS
    )

    assert ended == 2
    assert await services.sessions.count_for_user(STANDARD_ID) == 0


@pytest.mark.parametrize(("operation", "expected"), AUDITED_OPERATIONS)
async def test_every_account_operation_writes_one_audit_row_naming_actor_target_and_address(
    services: AuthServices,
    administrator: Principal,
    db: Session,
    operation: Operation,
    expected: tuple[str, int, str, dict[str, Any]],
) -> None:
    await operation(services, administrator)

    [event] = await read_events(db)
    assert (
        event["action"],
        event["actor_user_id"],
        event["target_id"],
        event["detail"],
    ) == expected
    assert event["result"] == "success"
    assert await read_categories(db) == ["user_action"]


async def test_the_session_count_an_administrator_sees_excludes_revoked_rows(
    services: AuthServices, administrator: Principal, db: Session, clock: FakeClock
) -> None:
    await _issue(services, STANDARD_ID)
    revoked = await services.sessions.issue(STANDARD_ID, user_agent=AGENT, remote_address=None)
    await db.execute(
        "UPDATE session SET revoked_at = ? WHERE id = ?",
        (format_timestamp(clock()), revoked.session_id),
    )

    view = await services.user_admin.get(administrator, STANDARD_ID)

    assert view.session_count == 1
