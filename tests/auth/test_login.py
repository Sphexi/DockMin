"""Tests for the login flow in app/auth/login.py, run against a real database."""

import io
from collections.abc import Callable
from typing import Any

import pytest

from app.auth.errors import InvalidCredentials, LockedOut
from app.auth.login import PASSWORD_METHOD, LoginResult
from app.auth.passwords import HashingBusy
from app.auth.principal import Credentials, PasswordCredentials, Principal
from app.auth.providers import (
    AuthenticationFailed,
    AuthProvider,
    UnknownProviderError,
    default_registry,
)
from app.auth.services import AuthRuntime, AuthServices, build_services
from app.auth.throttle import username_subject
from app.config import Settings
from app.db.engine import Session
from app.db.repositories.login_throttle import LoginThrottleRepository
from app.services.activity import ActivityRecorder
from tests.auth_support import (
    ADMIN_ID,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    CLIENT_ADDRESS,
    OTHER_ADDRESS,
    STANDARD_ID,
    STANDARD_PASSWORD,
    STANDARD_USERNAME,
    FakeClock,
    read_events,
)
from tests.support import MAX_LOGIN_FAILURES, TRUSTED_PROXY_ENV

AGENT = "test-agent"
UNKNOWN_USERNAME = "nobody-here"
SENTINEL_USERNAME = "sentinel-account-zq9"
RECORD_BASE_FIELDS = frozenset({"timestamp", "level", "logger", "message", "request_id"})
WRONG_PASSWORD = "not-the-password"
INVALID_MESSAGE = "Invalid username or password."
PROBE = "probe"
BUSY = "busy"
INTERNAL = "internal"
FRESH_ADDRESSES = tuple(f"192.0.2.{n}" for n in range(1, MAX_LOGIN_FAILURES + 3))


async def _login(
    services: AuthServices,
    username: str = ADMIN_USERNAME,
    password: str = ADMIN_PASSWORD,
    address: str = CLIENT_ADDRESS,
) -> LoginResult:
    return await services.login.login(
        INTERNAL,
        PasswordCredentials(username, password),
        remote_address=address,
        user_agent=AGENT,
    )


async def _fail(services: AuthServices, username: str, address: str, times: int) -> None:
    for _ in range(times):
        with pytest.raises(InvalidCredentials):
            await _login(services, username, WRONG_PASSWORD, address)


async def test_login_with_correct_credentials_issues_a_session_for_the_user(
    services: AuthServices,
) -> None:
    result = await _login(services)

    resolved = await services.sessions.resolve(result.session.token)
    assert result.principal.user_id == ADMIN_ID
    assert result.principal.session_id == result.session.session_id
    assert resolved == result.principal


async def test_login_with_correct_credentials_records_a_success_event_with_the_address(
    services: AuthServices, db: Session
) -> None:
    await _login(services)

    assert await read_events(db) == [
        {
            "actor_type": "user",
            "actor_user_id": ADMIN_ID,
            "actor_subsystem": None,
            "target_id": str(ADMIN_ID),
            "action": "login",
            "result": "success",
            "detail": {"address": CLIENT_ADDRESS, "method": PASSWORD_METHOD},
        }
    ]


async def test_login_with_correct_credentials_stamps_the_last_login_time(
    services: AuthServices,
) -> None:
    await _login(services)

    user = await services.users.get(ADMIN_ID)
    assert user is not None
    assert user.last_login_at is not None


async def test_a_failed_login_for_an_unknown_username_is_recorded_as_anonymous(
    services: AuthServices, db: Session
) -> None:
    with pytest.raises(InvalidCredentials):
        await _login(services, UNKNOWN_USERNAME, WRONG_PASSWORD)

    assert await read_events(db) == [
        {
            "actor_type": "anonymous",
            "actor_user_id": None,
            "actor_subsystem": None,
            "target_id": None,
            "action": "login",
            "result": "failure",
            "detail": {
                "address": CLIENT_ADDRESS,
                "reason": "unknown_user",
                "username": UNKNOWN_USERNAME,
                "method": PASSWORD_METHOD,
            },
        }
    ]


async def test_a_failed_login_for_a_known_username_is_recorded_anonymously_with_that_user_as_target(
    services: AuthServices, db: Session
) -> None:
    with pytest.raises(InvalidCredentials):
        await _login(services, ADMIN_USERNAME, WRONG_PASSWORD)

    assert await read_events(db) == [
        {
            "actor_type": "anonymous",
            "actor_user_id": None,
            "actor_subsystem": None,
            "target_id": str(ADMIN_ID),
            "action": "login",
            "result": "failure",
            "detail": {
                "address": CLIENT_ADDRESS,
                "reason": "bad_password",
                "username": ADMIN_USERNAME,
                "method": PASSWORD_METHOD,
            },
        }
    ]


async def test_a_failed_guess_at_a_known_username_does_not_make_the_account_undeletable(
    services: AuthServices, administrator: Principal
) -> None:
    with pytest.raises(InvalidCredentials):
        await _login(services, STANDARD_USERNAME, WRONG_PASSWORD)

    await services.user_admin.delete(
        administrator, STANDARD_ID, expected_name=STANDARD_USERNAME, remote_address=CLIENT_ADDRESS
    )

    assert await services.users.get(STANDARD_ID) is None


@pytest.mark.parametrize(
    ("username", "password"),
    [
        pytest.param(UNKNOWN_USERNAME, WRONG_PASSWORD, id="unknown-username"),
        pytest.param(ADMIN_USERNAME, WRONG_PASSWORD, id="wrong-password"),
    ],
)
async def test_a_failed_login_gives_the_same_message_whichever_half_was_wrong(
    services: AuthServices, username: str, password: str
) -> None:
    with pytest.raises(InvalidCredentials) as raised:
        await _login(services, username, password)

    assert str(raised.value) == INVALID_MESSAGE


async def test_a_disabled_account_cannot_sign_in_and_the_message_does_not_say_why(
    services: AuthServices, db: Session
) -> None:
    await services.users.disable(STANDARD_ID)

    with pytest.raises(InvalidCredentials) as raised:
        await _login(services, STANDARD_USERNAME, STANDARD_PASSWORD)

    [event] = await read_events(db)
    assert str(raised.value) == INVALID_MESSAGE
    assert event["detail"]["reason"] == "account_disabled"


async def test_login_through_an_unregistered_provider_is_refused(services: AuthServices) -> None:
    with pytest.raises(UnknownProviderError):
        await services.login.login(
            "ldap",
            PasswordCredentials(ADMIN_USERNAME, ADMIN_PASSWORD),
            remote_address=CLIENT_ADDRESS,
            user_agent=AGENT,
        )


async def test_repeated_failures_lock_out_even_the_correct_password(
    services: AuthServices,
) -> None:
    await _fail(services, ADMIN_USERNAME, CLIENT_ADDRESS, MAX_LOGIN_FAILURES)

    with pytest.raises(LockedOut) as raised:
        await _login(services)

    assert raised.value.retry_after > 0
    assert await services.sessions.count_for_user(ADMIN_ID) == 0


async def test_every_attempt_including_a_refused_one_is_recorded_with_the_address(
    services: AuthServices, db: Session
) -> None:
    await _fail(services, ADMIN_USERNAME, CLIENT_ADDRESS, MAX_LOGIN_FAILURES)
    with pytest.raises(LockedOut):
        await _login(services)

    events = await read_events(db)

    assert len(events) == MAX_LOGIN_FAILURES + 1
    assert {event["detail"]["address"] for event in events} == {CLIENT_ADDRESS}
    assert events[-1]["detail"]["reason"] == "locked_out"
    assert {event["result"] for event in events} == {"failure"}


async def test_failures_against_a_nonexistent_username_lock_that_username_from_any_address(
    services: AuthServices,
) -> None:
    for address in FRESH_ADDRESSES[:MAX_LOGIN_FAILURES]:
        await _fail(services, UNKNOWN_USERNAME, address, 1)

    with pytest.raises(LockedOut):
        await _login(services, UNKNOWN_USERNAME, WRONG_PASSWORD, FRESH_ADDRESSES[-1])


async def test_failures_from_one_address_do_not_refuse_a_correct_login_when_no_proxy_is_trusted(
    services: AuthServices,
) -> None:
    for number in range(MAX_LOGIN_FAILURES):
        await _fail(services, f"ghost-{number}", CLIENT_ADDRESS, 1)

    result = await _login(services)

    assert result.principal.user_id == ADMIN_ID


class TestBehindATrustedProxy:
    @pytest.fixture
    def env(self, env: dict[str, str]) -> dict[str, str]:
        return env | TRUSTED_PROXY_ENV

    async def test_failures_from_one_address_against_many_usernames_lock_the_address(
        self, services: AuthServices
    ) -> None:
        for number in range(MAX_LOGIN_FAILURES):
            await _fail(services, f"ghost-{number}", CLIENT_ADDRESS, 1)

        with pytest.raises(LockedOut):
            await _login(services)


async def test_a_successful_login_clears_the_username_counter(
    services: AuthServices,
) -> None:
    below_limit = MAX_LOGIN_FAILURES - 1
    await _fail(services, ADMIN_USERNAME, FRESH_ADDRESSES[0], below_limit)
    await _login(services, address=FRESH_ADDRESSES[1])
    await _fail(services, ADMIN_USERNAME, FRESH_ADDRESSES[2], below_limit)

    result = await _login(services, address=FRESH_ADDRESSES[3])

    assert result.principal.user_id == ADMIN_ID


async def test_logout_ends_the_session_and_records_the_event(
    services: AuthServices, db: Session
) -> None:
    result = await _login(services)

    await services.login.logout(result.principal, remote_address=OTHER_ADDRESS)

    events = await read_events(db)
    assert await services.sessions.resolve(result.session.token) is None
    assert events[-1]["action"] == "logout"
    assert events[-1]["detail"] == {"address": OTHER_ADDRESS}


def _fields(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key not in RECORD_BASE_FIELDS}


async def test_a_successful_login_logs_the_username_beside_the_user_id(
    services: AuthServices, logged: Callable[[str], list[dict[str, Any]]]
) -> None:
    await _login(services)

    [record] = logged("login_succeeded")
    assert _fields(record) == {
        "user_id": ADMIN_ID,
        "username": ADMIN_USERNAME,
        "address": CLIENT_ADDRESS,
        "method": PASSWORD_METHOD,
    }


async def test_a_failed_login_for_an_existing_account_logs_the_stored_username(
    services: AuthServices, logged: Callable[[str], list[dict[str, Any]]]
) -> None:
    with pytest.raises(InvalidCredentials):
        await _login(services, ADMIN_USERNAME.upper(), WRONG_PASSWORD)

    [record] = logged("login_failed")
    assert _fields(record) == {
        "user_id": ADMIN_ID,
        "username": ADMIN_USERNAME,
        "reason": "bad_password",
        "address": CLIENT_ADDRESS,
        "method": PASSWORD_METHOD,
    }


async def test_a_failed_login_for_an_unknown_username_logs_nothing_it_did_not_log_before(
    services: AuthServices,
    db: Session,
    log_stream: io.StringIO,
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    with pytest.raises(InvalidCredentials):
        await _login(services, SENTINEL_USERNAME, WRONG_PASSWORD)

    [event] = await read_events(db)
    [record] = logged("login_failed")
    assert event["detail"]["username"] == SENTINEL_USERNAME
    assert SENTINEL_USERNAME not in log_stream.getvalue()
    assert _fields(record) == {
        "user_id": None,
        "reason": "unknown_user",
        "address": CLIENT_ADDRESS,
        "method": PASSWORD_METHOD,
    }


async def test_a_refused_attempt_on_an_existing_account_logs_the_user_id_and_stored_username(
    services: AuthServices, logged: Callable[[str], list[dict[str, Any]]]
) -> None:
    await _fail(services, ADMIN_USERNAME, CLIENT_ADDRESS, MAX_LOGIN_FAILURES)
    with pytest.raises(LockedOut):
        await _login(services)

    [record] = logged("login_refused")
    assert _fields(record) == {
        "reason": "locked_out",
        "address": CLIENT_ADDRESS,
        "method": PASSWORD_METHOD,
        "user_id": ADMIN_ID,
        "username": ADMIN_USERNAME,
    }


async def test_a_refused_attempt_on_an_unknown_username_logs_neither_an_id_nor_the_name(
    services: AuthServices,
    log_stream: io.StringIO,
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    await _fail(services, SENTINEL_USERNAME, CLIENT_ADDRESS, MAX_LOGIN_FAILURES)
    with pytest.raises(LockedOut):
        await _login(services, SENTINEL_USERNAME, WRONG_PASSWORD)

    [record] = logged("login_refused")
    assert SENTINEL_USERNAME not in log_stream.getvalue()
    assert _fields(record) == {
        "reason": "locked_out",
        "address": CLIENT_ADDRESS,
        "method": PASSWORD_METHOD,
    }


async def test_logout_logs_the_user_id_username_and_address(
    services: AuthServices, logged: Callable[[str], list[dict[str, Any]]]
) -> None:
    result = await _login(services)

    await services.login.logout(result.principal, remote_address=OTHER_ADDRESS)

    [record] = logged("logout_succeeded")
    assert _fields(record) == {
        "user_id": ADMIN_ID,
        "username": ADMIN_USERNAME,
        "address": OTHER_ADDRESS,
        "method": PASSWORD_METHOD,
    }


class _CountingProvider:
    """Records the username's failure count at the moment of verification, then decides."""

    def __init__(
        self, repository: LoginThrottleRepository, seen: list[int], *, accept: bool
    ) -> None:
        self._repository = repository
        self._seen = seen
        self._accept = accept

    async def authenticate(self, credentials: Credentials) -> Principal:
        row = await self._repository.get("username", username_subject(credentials.username))
        self._seen.append(0 if row is None else row.failure_count)
        if not self._accept:
            raise AuthenticationFailed("bad_password", ADMIN_ID)
        return Principal(ADMIN_ID, ADMIN_USERNAME, "administrator")


class _SaturatedProvider:
    async def authenticate(self, credentials: Credentials) -> Principal:
        raise HashingBusy


def _with_provider(
    db: Session, settings: Settings, runtime: AuthRuntime, name: str, provider: AuthProvider
) -> AuthServices:
    registry = default_registry()
    registry.register(name, lambda _context: provider)
    runtime.registry = registry
    return build_services(db, settings, runtime, ActivityRecorder(db, runtime.clock))


async def test_the_failure_is_already_counted_while_the_password_is_being_checked(
    db: Session, settings: Settings, runtime: AuthRuntime, clock: FakeClock
) -> None:
    seen: list[int] = []
    provider = _CountingProvider(LoginThrottleRepository(db, clock), seen, accept=False)
    services = _with_provider(db, settings, runtime, PROBE, provider)

    with pytest.raises(InvalidCredentials):
        await services.login.login(
            PROBE,
            PasswordCredentials(ADMIN_USERNAME, WRONG_PASSWORD),
            remote_address=CLIENT_ADDRESS,
            user_agent=AGENT,
        )

    assert seen == [1]


async def test_a_successful_login_takes_back_the_count_made_before_the_check(
    db: Session, settings: Settings, runtime: AuthRuntime, clock: FakeClock
) -> None:
    seen: list[int] = []
    provider = _CountingProvider(LoginThrottleRepository(db, clock), seen, accept=True)
    services = _with_provider(db, settings, runtime, PROBE, provider)

    await services.login.login(
        PROBE,
        PasswordCredentials(ADMIN_USERNAME, ADMIN_PASSWORD),
        remote_address=CLIENT_ADDRESS,
        user_agent=AGENT,
    )

    repository = LoginThrottleRepository(db, clock)
    address = await repository.get("address", CLIENT_ADDRESS)
    assert seen == [1]
    assert await repository.get("username", ADMIN_USERNAME) is None
    assert address is not None
    assert address.failure_count == 0


async def test_attempts_refused_for_lack_of_hashing_capacity_are_recorded_and_never_lock_anyone(
    db: Session, settings: Settings, runtime: AuthRuntime
) -> None:
    services = _with_provider(db, settings, runtime, BUSY, _SaturatedProvider())
    for _ in range(MAX_LOGIN_FAILURES + 1):
        with pytest.raises(HashingBusy):
            await services.login.login(
                BUSY,
                PasswordCredentials(ADMIN_USERNAME, WRONG_PASSWORD),
                remote_address=CLIENT_ADDRESS,
                user_agent=AGENT,
            )

    events = await read_events(db)
    result = await _login(services)

    assert [event["detail"]["reason"] for event in events] == ["server_busy"] * (
        MAX_LOGIN_FAILURES + 1
    )
    assert result.principal.user_id == ADMIN_ID
