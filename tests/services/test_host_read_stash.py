"""Tests for HostReadStash: a result usable only by whoever read it, and only for a while."""

from app.services.host_read_stash import HostReadStash
from app.services.stack_read_result import HostReadResult

USER, OTHER_USER = 1, 2
ENVIRONMENT, OTHER_ENVIRONMENT = 7, 8
NAME = "shop"
RESULT = HostReadResult("services: {}\n", "read")
LIFETIME = 900.0
CAPACITY = 32
PER_USER = 8


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_a_result_is_returned_only_for_the_user_environment_kind_and_name_that_made_it() -> None:
    stash = HostReadStash(Clock())
    token = stash.put(USER, ENVIRONMENT, "compose", NAME, RESULT)

    assert stash.get(token, USER, ENVIRONMENT, "compose", NAME) is RESULT
    assert stash.get(token, OTHER_USER, ENVIRONMENT, "compose", NAME) is None
    assert stash.get(token, USER, OTHER_ENVIRONMENT, "compose", NAME) is None
    assert stash.get(token, USER, ENVIRONMENT, "swarm", NAME) is None
    assert stash.get(token, USER, ENVIRONMENT, "compose", "other") is None
    assert stash.get("A" * 22, USER, ENVIRONMENT, "compose", NAME) is None


def test_a_result_expires_and_a_discarded_one_is_gone() -> None:
    clock = Clock()
    stash = HostReadStash(clock)
    expiring = stash.put(USER, ENVIRONMENT, "compose", NAME, RESULT)
    discarded = stash.put(USER, ENVIRONMENT, "compose", NAME, RESULT)

    stash.discard(discarded)
    clock.now = LIFETIME

    assert stash.get(discarded, USER, ENVIRONMENT, "compose", NAME) is None
    assert stash.get(expiring, USER, ENVIRONMENT, "compose", NAME) is None


def test_the_oldest_result_is_dropped_when_more_than_the_capacity_are_held() -> None:
    stash = HostReadStash(Clock())
    first = stash.put(USER, ENVIRONMENT, "compose", NAME, RESULT)
    later = [stash.put(USER, ENVIRONMENT, "compose", NAME, RESULT) for _ in range(CAPACITY)]

    assert stash.get(first, USER, ENVIRONMENT, "compose", NAME) is None
    assert stash.get(later[-1], USER, ENVIRONMENT, "compose", NAME) is RESULT


def test_one_user_holds_at_most_eight_reads_and_cannot_evict_another_users() -> None:
    stash = HostReadStash(Clock())
    other = stash.put(OTHER_USER, ENVIRONMENT, "compose", NAME, RESULT)
    mine = [stash.put(USER, ENVIRONMENT, "compose", NAME, RESULT) for _ in range(PER_USER + 3)]

    assert stash.get(other, OTHER_USER, ENVIRONMENT, "compose", NAME) is RESULT
    assert [stash.get(t, USER, ENVIRONMENT, "compose", NAME) is RESULT for t in mine] == (
        [False] * 3 + [True] * PER_USER
    )
