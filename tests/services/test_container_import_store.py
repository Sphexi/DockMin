"""The held create form state of a container import: who may take it and how long it lives."""

import pytest

from app.services.container_import_store import (
    CAPACITY,
    LIFETIME_SECONDS,
    PreparedContainer,
    PreparedContainerStore,
)

USER, SESSION, ENVIRONMENT = 1, 10, 100


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def state(
    store: PreparedContainerStore, *, user: int = USER, environment: int = ENVIRONMENT
) -> PreparedContainer:
    return PreparedContainer(
        id=store.new_id(),
        user_id=user,
        session_id=SESSION,
        environment_id=environment,
        source_name="web",
        exported="2026-11-09T12:00:00+00:00",
        values={"image": "nginx:1"},
        not_exported=(),
        networks_created=(),
        volumes_created=(),
        environment_variables=0,
        secret_values=0,
        expires=store.expiry(),
    )


def lookup(
    store: PreparedContainerStore, held: PreparedContainer, **changes: int
) -> PreparedContainer | None:
    keys = {"user_id": USER, "session_id": SESSION, "environment_id": ENVIRONMENT} | changes
    return store.get(held.id, **keys)


@pytest.mark.parametrize(
    "changes",
    [{"user_id": 2}, {"session_id": 11}, {"environment_id": 101}],
    ids=["user", "session", "environment"],
)
def test_state_is_given_only_to_the_user_session_and_environment_that_made_it(
    changes: dict[str, int],
) -> None:
    store = PreparedContainerStore()
    held = state(store)
    store.hold(held)

    assert lookup(store, held) is held
    assert lookup(store, held, **changes) is None


def test_state_expires_after_its_lifetime() -> None:
    clock = Clock()
    store = PreparedContainerStore(clock)
    held = state(store)
    store.hold(held)

    clock.now = LIFETIME_SECONDS - 1
    before = lookup(store, held)
    clock.now = LIFETIME_SECONDS
    after = lookup(store, held)

    assert before is held and after is None


def test_a_newer_state_replaces_the_same_users_for_the_environment_and_discard_drops_it() -> None:
    store = PreparedContainerStore()
    first, second, other = state(store), state(store), state(store, environment=ENVIRONMENT + 1)
    for held in (first, second, other):
        store.hold(held)

    store.discard(second.id, user_id=USER, session_id=SESSION, environment_id=ENVIRONMENT)

    assert lookup(store, first) is None and lookup(store, second) is None
    assert lookup(store, other, environment_id=ENVIRONMENT + 1) is other


def test_the_oldest_state_goes_when_more_than_the_capacity_is_held() -> None:
    store = PreparedContainerStore()
    held = [state(store, user=number) for number in range(CAPACITY + 1)]
    for item in held:
        store.hold(item)

    assert lookup(store, held[0], user_id=0) is None
    assert lookup(store, held[-1], user_id=CAPACITY) is held[-1]
