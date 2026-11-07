"""Tests for app/services/reveal_access.py: opening a session's reveal window."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta

import pytest

from app.auth.errors import PermissionDenied, ValidationFailed
from app.auth.jwt_identity import Assertion
from app.auth.platform_signin import PlatformIdentity
from app.auth.principal import Principal
from app.auth.services import AuthRuntime, AuthServices, build_services
from app.config import Settings
from app.db.engine import Session
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.user import UserRepository
from app.db.repositories.user_session import (
    REVEAL_LIMIT,
    REVEAL_WINDOW,
    UserSessionRepository,
)
from app.services.activity import ActivityRecorder
from app.services.reveal_access import RevealAccess
from tests.access_support import MEMBER_ID, MEMBER_USERNAME
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    CLIENT_ADDRESS,
    NEW_PASSWORD,
    STANDARD_ID,
    STANDARD_PASSWORD,
    STANDARD_USERNAME,
    FakeClock,
    read_categories,
    read_events,
)
from tests.jwt_support import ec_signer, verifier
from tests.reveal_support import open_session, reveal_access
from tests.support import present

WRONG_PASSWORD = "not-the-password-at-all"
PLATFORM_HEADER = "X-Platform-Token"
PLATFORM_IDENTITY = "pat@example.test"
PLATFORM_USERNAME = "pat.example"
PLATFORM_DIGEST = "d" * 64
TOO_MANY = "Too many failed attempts. Try again later."
INCORRECT = "The current password is incorrect."


@pytest.fixture
def services(db: Session, settings: Settings, runtime: AuthRuntime) -> AuthServices:
    return build_services(db, settings, runtime, ActivityRecorder(db, runtime.clock))


@pytest.fixture
def access(db: Session, runtime: AuthRuntime, services: AuthServices) -> RevealAccess:
    return reveal_access(db, runtime, services)


@pytest.fixture
async def admin(db: Session, runtime: AuthRuntime) -> Principal:
    return await open_session(
        db, runtime, ADMIN_ID, ADMIN_USERNAME, "administrator", token="admin-token"
    )


async def _open(access: RevealAccess, actor: Principal, password: str = ADMIN_PASSWORD) -> None:
    await access.open(actor, password=password, remote_address=CLIENT_ADDRESS)


async def _window_end(db: Session, runtime: AuthRuntime, actor: Principal) -> str | None:
    window = await UserSessionRepository(db, runtime.clock).reveal_window(
        present(actor.session_id), runtime.clock()
    )
    return None if window is None else window.ends_at


async def test_the_correct_password_opens_a_five_minute_window_and_records_how_it_was_opened(
    db: Session, runtime: AuthRuntime, access: RevealAccess, admin: Principal, clock: FakeClock
) -> None:
    await _open(access, admin)

    ends = format_timestamp(clock() + REVEAL_WINDOW)
    window = await UserSessionRepository(db, clock).reveal_window(
        present(admin.session_id), clock()
    )
    [event] = await read_events(db)
    assert timedelta(seconds=300) == REVEAL_WINDOW
    assert window is not None and (window.ends_at, window.reveals_left) == (ends, REVEAL_LIMIT)
    assert (event["action"], event["result"], event["actor_user_id"]) == (
        "reveal_window_opened",
        "success",
        ADMIN_ID,
    )
    assert event["target_id"] == str(ADMIN_ID)
    assert event["detail"] == {"address": CLIENT_ADDRESS, "method": "password", "expires_at": ends}
    assert await read_categories(db) == ["authentication"]


async def test_a_wrong_password_records_a_failure_and_leaves_the_window_closed(
    db: Session, runtime: AuthRuntime, access: RevealAccess, admin: Principal
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        await _open(access, admin, WRONG_PASSWORD)

    [event] = await read_events(db)
    assert refused.value.fields == {"password": INCORRECT}
    assert (event["action"], event["result"]) == ("reveal_window_opened", "failure")
    assert event["detail"]["reason"] == "bad_current_password"
    assert "expires_at" not in event["detail"]
    assert await _window_end(db, runtime, admin) is None


async def test_wrong_guesses_count_toward_the_username_lock_and_a_locked_account_is_not_verified(
    db: Session,
    runtime: AuthRuntime,
    access: RevealAccess,
    admin: Principal,
    settings: Settings,
) -> None:
    for _ in range(settings.login_max_failures):
        with pytest.raises(ValidationFailed):
            await _open(access, admin, WRONG_PASSWORD)

    with pytest.raises(ValidationFailed) as locked:
        await _open(access, admin, ADMIN_PASSWORD)

    last = (await read_events(db))[-1]
    assert locked.value.fields == {"password": TOO_MANY}
    assert last["detail"]["reason"] == "locked_out"
    assert await _window_end(db, runtime, admin) is None


async def test_an_account_whose_password_was_generated_at_platform_sign_in_cannot_open_a_window(
    db: Session, runtime: AuthRuntime, settings: Settings
) -> None:
    runtime.platform = PlatformIdentity(
        header=PLATFORM_HEADER, create_users=True, verifier=verifier(ec_signer())
    )
    platform = present(
        build_services(db, settings, runtime, ActivityRecorder(db, runtime.clock)).platform
    )
    created = present(
        await platform.exchange(
            Assertion(PLATFORM_IDENTITY, PLATFORM_USERNAME, PLATFORM_DIGEST),
            remote_address=CLIENT_ADDRESS,
            user_agent=None,
        )
    )
    await UserRepository(db, runtime.clock).set_role(created.principal.user_id, "administrator")
    promoted = replace(created.principal, role="administrator")
    access = reveal_access(
        db, runtime, build_services(db, settings, runtime, ActivityRecorder(db, runtime.clock))
    )

    with pytest.raises(ValidationFailed) as refused:
        await _open(access, promoted, PLATFORM_USERNAME)

    assert refused.value.fields == {"password": INCORRECT}
    assert await _window_end(db, runtime, promoted) is None


async def test_a_token_a_standard_user_and_an_ended_session_cannot_open_a_window(
    db: Session, runtime: AuthRuntime, access: RevealAccess, admin: Principal
) -> None:
    standard = await open_session(
        db, runtime, STANDARD_ID, STANDARD_USERNAME, "standard", token="standard-token"
    )
    token = Principal(ADMIN_ID, ADMIN_USERNAME, "administrator")
    await UserSessionRepository(db, runtime.clock).delete(present(admin.session_id))
    before = await read_events(db)

    for actor, password in (
        (standard, STANDARD_PASSWORD),
        (token, ADMIN_PASSWORD),
        (admin, ADMIN_PASSWORD),
    ):
        with pytest.raises(PermissionDenied):
            await _open(access, actor, password)

    assert await read_events(db) == before
    assert await _window_end(db, runtime, standard) is None


async def _change_own_password(services: AuthServices, admin: Principal, other: Principal) -> None:
    await services.user_admin.change_password(
        admin,
        current_password=ADMIN_PASSWORD,
        new_password=NEW_PASSWORD,
        new_password_confirm=NEW_PASSWORD,
        remote_address=CLIENT_ADDRESS,
    )


async def _reset_password(services: AuthServices, admin: Principal, other: Principal) -> None:
    await services.user_admin.reset_password(
        other, ADMIN_ID, new_password=NEW_PASSWORD, remote_address=CLIENT_ADDRESS
    )


async def _demote(services: AuthServices, admin: Principal, other: Principal) -> object:
    return await services.user_admin.update(
        other, ADMIN_ID, username=ADMIN_USERNAME, role="standard", remote_address=CLIENT_ADDRESS
    )


async def _disable(services: AuthServices, admin: Principal, other: Principal) -> object:
    return await services.user_admin.set_enabled(
        other, ADMIN_ID, enabled=False, remote_address=CLIENT_ADDRESS
    )


async def _sign_out(services: AuthServices, admin: Principal, other: Principal) -> object:
    return await services.sessions.end(present(admin.session_id))


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(_change_own_password, id="password-change-including-the-kept-session"),
        pytest.param(_reset_password, id="password-reset"),
        pytest.param(_demote, id="role-change"),
        pytest.param(_disable, id="disable"),
        pytest.param(_sign_out, id="sign-out"),
    ],
)
async def test_the_window_ends_when_the_account_changes_or_the_session_does(
    db: Session,
    runtime: AuthRuntime,
    services: AuthServices,
    access: RevealAccess,
    admin: Principal,
    operation: Callable[[AuthServices, Principal, Principal], Awaitable[object]],
) -> None:
    await UserRepository(db, runtime.clock).set_role(MEMBER_ID, "administrator")
    other = await open_session(
        db, runtime, MEMBER_ID, MEMBER_USERNAME, "administrator", token="other-token"
    )
    await _open(access, admin)
    assert await _window_end(db, runtime, admin) is not None

    await operation(services, admin, other)

    assert await _window_end(db, runtime, admin) is None


async def test_a_password_change_keeps_the_session_but_not_its_window(
    db: Session,
    runtime: AuthRuntime,
    services: AuthServices,
    access: RevealAccess,
    admin: Principal,
) -> None:
    await _open(access, admin)

    await _change_own_password(services, admin, admin)

    kept = await UserSessionRepository(db, runtime.clock).get(present(admin.session_id))
    assert kept is not None
    assert await _window_end(db, runtime, admin) is None


async def test_the_window_is_closed_once_the_clock_passes_its_end(
    db: Session, runtime: AuthRuntime, access: RevealAccess, admin: Principal, clock: FakeClock
) -> None:
    await _open(access, admin)
    before = await _window_end(db, runtime, admin)

    clock.advance(REVEAL_WINDOW)

    assert before is not None
    assert await _window_end(db, runtime, admin) is None
