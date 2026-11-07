"""Tests for app/auth/throttle.py."""

from datetime import timedelta

import pytest

from app.auth.throttle import KeyedLocks, LoginThrottleService
from app.db.engine import Session
from app.db.repositories.login_throttle import LoginThrottleRepository
from tests.auth_support import CLIENT_ADDRESS, OTHER_ADDRESS, FakeClock
from tests.support import MAX_LOGIN_FAILURES

WINDOW = timedelta(minutes=15)
USERNAME = "operator"
OTHER_USERNAME = "someone-else"
ONE_SECOND = timedelta(seconds=1)
UNRELATED_ADDRESS = "192.0.2.1"
FRESH_ADDRESS = "192.0.2.10"
ADDRESSES = ("192.0.2.1", "192.0.2.2", "192.0.2.3")


@pytest.fixture
def throttle(db: Session, clock: FakeClock) -> LoginThrottleService:
    return _throttle(db, clock, enforce_address=True)


def _throttle(db: Session, clock: FakeClock, *, enforce_address: bool) -> LoginThrottleService:
    return LoginThrottleService(
        LoginThrottleRepository(db, clock),
        clock,
        max_failures=MAX_LOGIN_FAILURES,
        window=WINDOW,
        enforce_address=enforce_address,
        locks=KeyedLocks(),
    )


async def _fail(throttle: LoginThrottleService, times: int, username: str = USERNAME) -> None:
    for _ in range(times):
        await throttle.record_failure(username, CLIENT_ADDRESS)


async def test_retry_after_is_zero_for_a_subject_with_no_failures(
    throttle: LoginThrottleService,
) -> None:
    assert await throttle.retry_after(USERNAME, CLIENT_ADDRESS) == 0


async def test_retry_after_is_zero_while_failures_stay_below_the_limit(
    throttle: LoginThrottleService,
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES - 1)

    assert await throttle.retry_after(USERNAME, CLIENT_ADDRESS) == 0


async def test_reaching_the_limit_locks_the_subject_for_the_whole_window(
    throttle: LoginThrottleService,
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES)

    assert await throttle.retry_after(USERNAME, CLIENT_ADDRESS) == WINDOW.total_seconds()


async def test_the_lock_counts_down_as_time_passes(
    throttle: LoginThrottleService, clock: FakeClock
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES)
    clock.advance(timedelta(minutes=5))

    remaining = await throttle.retry_after(USERNAME, CLIENT_ADDRESS)

    assert remaining == timedelta(minutes=10).total_seconds()


async def test_the_lock_lifts_when_the_window_has_passed(
    throttle: LoginThrottleService, clock: FakeClock
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES)
    clock.advance(WINDOW + ONE_SECOND)

    assert await throttle.retry_after(USERNAME, CLIENT_ADDRESS) == 0


async def test_a_failure_after_a_lock_has_lifted_starts_a_fresh_count(
    throttle: LoginThrottleService, clock: FakeClock
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES)
    clock.advance(WINDOW + ONE_SECOND)

    await _fail(throttle, 1)

    assert await throttle.retry_after(USERNAME, CLIENT_ADDRESS) == 0


async def test_failures_spread_over_more_than_a_window_do_not_add_up(
    throttle: LoginThrottleService, clock: FakeClock
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES - 1)
    clock.advance(WINDOW + ONE_SECOND)

    await _fail(throttle, 1)

    assert await throttle.retry_after(USERNAME, CLIENT_ADDRESS) == 0


@pytest.mark.parametrize("variant", ["OPERATOR", "  operator  ", "Operator"])
async def test_a_username_is_counted_without_regard_to_case_or_padding(
    throttle: LoginThrottleService, variant: str
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES - 1)
    await throttle.record_failure(variant, OTHER_ADDRESS)

    assert await throttle.retry_after(USERNAME, UNRELATED_ADDRESS) > 0


async def test_failures_from_one_address_lock_it_whatever_usernames_were_tried(
    throttle: LoginThrottleService,
) -> None:
    await throttle.record_failure("first-name", CLIENT_ADDRESS)
    await throttle.record_failure("second-name", CLIENT_ADDRESS)
    await throttle.record_failure("third-name", CLIENT_ADDRESS)

    assert await throttle.retry_after(OTHER_USERNAME, CLIENT_ADDRESS) > 0


async def test_failures_against_one_username_lock_it_from_any_address(
    throttle: LoginThrottleService,
) -> None:
    for address in ADDRESSES:
        await throttle.record_failure(USERNAME, address)

    assert await throttle.retry_after(USERNAME, FRESH_ADDRESS) > 0
    assert await throttle.retry_after(OTHER_USERNAME, FRESH_ADDRESS) == 0


async def test_clearing_a_username_forgets_its_failures(
    throttle: LoginThrottleService,
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES - 1)

    await throttle.clear_username(USERNAME)
    await throttle.record_failure(USERNAME, OTHER_ADDRESS)

    assert await throttle.retry_after(USERNAME, UNRELATED_ADDRESS) == 0


async def test_clearing_a_username_leaves_the_addresss_failures_standing(
    throttle: LoginThrottleService,
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES - 1)

    await throttle.clear_username(USERNAME)
    await throttle.record_failure(OTHER_USERNAME, CLIENT_ADDRESS)

    assert await throttle.retry_after(OTHER_USERNAME, CLIENT_ADDRESS) > 0


async def test_an_attempt_without_a_username_counts_against_the_address_only(
    throttle: LoginThrottleService,
) -> None:
    for _ in range(MAX_LOGIN_FAILURES):
        await throttle.record_failure(None, CLIENT_ADDRESS)

    assert await throttle.retry_after(None, CLIENT_ADDRESS) > 0
    assert await throttle.retry_after(USERNAME, OTHER_ADDRESS) == 0


async def test_an_address_is_counted_but_not_enforced_when_no_proxy_is_trusted(
    db: Session, clock: FakeClock
) -> None:
    throttle = _throttle(db, clock, enforce_address=False)
    for name in ("first-name", "second-name", "third-name"):
        await throttle.record_failure(name, CLIENT_ADDRESS)

    counted = await LoginThrottleRepository(db, clock).get("address", CLIENT_ADDRESS)

    assert await throttle.retry_after(OTHER_USERNAME, CLIENT_ADDRESS) == 0
    assert counted is not None
    assert counted.failure_count == MAX_LOGIN_FAILURES


async def test_a_username_lock_is_still_enforced_when_no_proxy_is_trusted(
    db: Session, clock: FakeClock
) -> None:
    throttle = _throttle(db, clock, enforce_address=False)
    for address in ADDRESSES:
        await throttle.record_failure(USERNAME, address)

    assert await throttle.retry_after(USERNAME, FRESH_ADDRESS) > 0


async def test_a_refund_takes_back_the_attempt_and_lifts_a_lock_it_no_longer_justifies(
    throttle: LoginThrottleService,
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES)

    await throttle.refund(USERNAME, CLIENT_ADDRESS)

    assert await throttle.retry_after(USERNAME, CLIENT_ADDRESS) == 0


async def test_settling_a_success_forgets_the_username_and_takes_back_one_address_count(
    db: Session, clock: FakeClock, throttle: LoginThrottleService
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES - 1)

    await throttle.settle_success(USERNAME, CLIENT_ADDRESS)

    repository = LoginThrottleRepository(db, clock)
    address = await repository.get("address", CLIENT_ADDRESS)
    assert await repository.get("username", USERNAME) is None
    assert address is not None
    assert address.failure_count == MAX_LOGIN_FAILURES - 2


async def test_clearing_a_subject_lifts_a_username_lock_and_an_address_lock(
    throttle: LoginThrottleService,
) -> None:
    await _fail(throttle, MAX_LOGIN_FAILURES)

    removed = await throttle.clear(USERNAME) + await throttle.clear(CLIENT_ADDRESS)

    assert removed == 2
    assert await throttle.retry_after(USERNAME, CLIENT_ADDRESS) == 0
