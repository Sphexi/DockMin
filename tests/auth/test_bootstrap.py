"""Tests for app/auth/bootstrap.py, run against a database with no users."""

import io
import json

import pytest

from app.auth.bootstrap import announce_setup_token, issue_setup_token
from app.auth.errors import LockedOut, SetupClosed, SetupTokenRejected, ValidationFailed
from app.auth.services import AuthRuntime, AuthServices
from app.db.engine import Session
from app.logging import REDACTED
from tests.auth_support import ADMIN_PASSWORD, ADMIN_USERNAME, CLIENT_ADDRESS, read_events
from tests.support import MAX_LOGIN_FAILURES, TRUSTED_PROXY_ENV

WRONG_TOKEN = "not-the-setup-token"
WEAK_PASSWORD = "short"
MISMATCHED_PASSWORD = "a-different-password-1234"


@pytest.fixture
def token(runtime: AuthRuntime) -> str:
    return issue_setup_token(runtime.setup)


async def _complete(
    services: AuthServices,
    setup_token: str,
    *,
    username: str = ADMIN_USERNAME,
    password: str = ADMIN_PASSWORD,
    confirm: str = ADMIN_PASSWORD,
) -> None:
    await services.bootstrap.complete(
        setup_token=setup_token,
        username=username,
        password=password,
        password_confirm=confirm,
        remote_address=CLIENT_ADDRESS,
    )


async def test_a_database_without_an_administrator_has_the_setup_window_open(
    empty_services: AuthServices,
) -> None:
    assert await empty_services.bootstrap.is_open() is True


async def test_complete_with_the_right_token_creates_the_first_administrator(
    empty_services: AuthServices, token: str
) -> None:
    await _complete(empty_services, token)

    user = await empty_services.users.get_by_username(ADMIN_USERNAME)
    assert user is not None
    assert user.role == "administrator"
    assert user.is_enabled is True
    assert user.password_hash != ADMIN_PASSWORD


async def test_complete_records_a_success_event_with_the_address(
    empty_services: AuthServices, empty_db: Session, token: str
) -> None:
    await _complete(empty_services, token)

    [event] = await read_events(empty_db)
    assert (event["action"], event["result"]) == ("bootstrap", "success")
    assert event["detail"] == {"address": CLIENT_ADDRESS}


async def test_completing_setup_closes_it_and_discards_the_token(
    empty_services: AuthServices, runtime: AuthRuntime, token: str
) -> None:
    await _complete(empty_services, token)

    assert await empty_services.bootstrap.is_open() is False
    assert runtime.setup.token is None
    assert runtime.setup.complete is True


async def test_complete_after_setup_closed_refuses_even_with_the_old_token(
    empty_services: AuthServices, token: str
) -> None:
    await _complete(empty_services, token)

    with pytest.raises(SetupClosed):
        await _complete(empty_services, token, username="second-admin")


async def test_complete_with_a_wrong_token_is_refused_and_creates_nobody(
    empty_services: AuthServices, token: str
) -> None:
    with pytest.raises(SetupTokenRejected):
        await _complete(empty_services, WRONG_TOKEN)

    assert await empty_services.users.count_administrators() == 0
    assert await empty_services.bootstrap.is_open() is True


async def test_complete_with_a_wrong_token_records_a_failure_with_the_address(
    empty_services: AuthServices, empty_db: Session, token: str
) -> None:
    with pytest.raises(SetupTokenRejected):
        await _complete(empty_services, WRONG_TOKEN)

    [event] = await read_events(empty_db)
    assert event["result"] == "failure"
    assert event["detail"] == {"address": CLIENT_ADDRESS, "reason": "bad_setup_token"}
    assert WRONG_TOKEN not in json.dumps(event)


async def test_repeated_wrong_tokens_do_not_lock_the_address_when_no_proxy_is_trusted(
    empty_services: AuthServices, token: str
) -> None:
    for _ in range(MAX_LOGIN_FAILURES):
        with pytest.raises(SetupTokenRejected):
            await _complete(empty_services, WRONG_TOKEN)

    await _complete(empty_services, token)

    assert await empty_services.users.count_administrators() == 1


class TestBehindATrustedProxy:
    @pytest.fixture
    def env(self, env: dict[str, str]) -> dict[str, str]:
        return env | TRUSTED_PROXY_ENV

    async def test_repeated_wrong_tokens_lock_the_address_out_even_for_the_right_one(
        self, empty_services: AuthServices, token: str
    ) -> None:
        for _ in range(MAX_LOGIN_FAILURES):
            with pytest.raises(SetupTokenRejected):
                await _complete(empty_services, WRONG_TOKEN)

        with pytest.raises(LockedOut):
            await _complete(empty_services, token)


async def test_complete_applies_the_password_policy_and_keeps_the_token_usable(
    empty_services: AuthServices, token: str
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await _complete(empty_services, token, password=WEAK_PASSWORD, confirm=WEAK_PASSWORD)

    assert set(raised.value.fields) == {"password"}
    assert await empty_services.users.count_administrators() == 0
    await _complete(empty_services, token)


async def test_a_correct_token_followed_by_a_validation_failure_is_recorded(
    empty_services: AuthServices, empty_db: Session, token: str
) -> None:
    with pytest.raises(ValidationFailed):
        await _complete(empty_services, token, password=WEAK_PASSWORD, confirm=WEAK_PASSWORD)

    [event] = await read_events(empty_db)
    assert (event["action"], event["result"], event["actor_type"]) == (
        "bootstrap",
        "failure",
        "anonymous",
    )
    assert event["detail"] == {
        "address": CLIENT_ADDRESS,
        "reason": "validation_failed",
        "fields": "password",
    }


async def test_complete_refuses_passwords_that_do_not_match(
    empty_services: AuthServices, token: str
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await _complete(empty_services, token, confirm=MISMATCHED_PASSWORD)

    assert set(raised.value.fields) == {"password_confirm"}


async def test_complete_refuses_a_username_outside_the_allowed_pattern(
    empty_services: AuthServices, token: str
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        await _complete(empty_services, token, username="no spaces allowed")

    assert set(raised.value.fields) == {"username"}


def test_the_announced_setup_token_reaches_the_log_unredacted(
    log_stream: io.StringIO, runtime: AuthRuntime
) -> None:
    announced = issue_setup_token(runtime.setup)

    announce_setup_token(announced)

    [record] = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    assert record["message"].endswith(announced)
    assert REDACTED not in record["message"]
