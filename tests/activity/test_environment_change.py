"""What an audited mutation tells the rest of the process about the environment it changed."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest

import app.services.activity as activity_module
from app.db.engine import Session
from app.db.repositories.user import UserRepository
from app.services.activity import (
    ActivityRecorder,
    UserActor,
    install_environment_change_hook,
    remove_environment_change_hook,
    request_changed_environment,
    track_request_changes,
)
from tests.auth_support import FakeClock
from tests.db.helpers import PASSWORD_HASH
from tests.environment_support import seed_environment
from tests.support import present

ACTION = "probe_changed"
TARGET = "probe"
INSERT_PROBE = "INSERT INTO probe (value) VALUES (?)"
HOOK_FAILED = "environment_change_hook_failed"


@pytest.fixture(autouse=True)
def hook_restored() -> Iterator[None]:
    held = activity_module._environment_change_hook
    yield
    install_environment_change_hook(held)


@pytest.fixture
async def probe(db: Session) -> None:
    await db.execute("CREATE TABLE probe (value TEXT NOT NULL)")


@pytest.fixture
async def environment_id(db: Session, clock: FakeClock) -> int:
    return (await seed_environment(db, clock, "edge")).id


@pytest.fixture
async def actor(db: Session) -> UserActor:
    created = await UserRepository(db).create("scope-user", PASSWORD_HASH, "standard")
    return UserActor(created.id)


@pytest.fixture
def told() -> list[int]:
    heard: list[int] = []
    install_environment_change_hook(heard.append)
    return heard


async def mutate(
    recorder: ActivityRecorder,
    db: Session,
    actor: UserActor,
    environment_id: int | None,
    *,
    fail: bool = False,
) -> None:
    async with (
        recorder.audit(actor, action=ACTION, target_type=TARGET, environment_id=environment_id),
        db.transaction(),
    ):
        await db.execute(INSERT_PROBE, ("value",))
        if fail:
            raise RuntimeError("abort")


async def test_a_committed_audited_mutation_tells_the_hook_which_environment_changed_once(
    recorder: ActivityRecorder,
    db: Session,
    actor: UserActor,
    environment_id: int,
    probe: None,
    told: list[int],
) -> None:
    await mutate(recorder, db, actor, environment_id)

    assert told == [environment_id]


async def test_a_block_that_only_talks_to_docker_still_tells_the_hook(
    recorder: ActivityRecorder,
    db: Session,
    actor: UserActor,
    environment_id: int,
    told: list[int],
) -> None:
    async with recorder.audit(
        actor, action=ACTION, target_type=TARGET, environment_id=environment_id
    ):
        pass

    assert told == [environment_id]


async def test_a_failed_mutation_tells_the_hook_nothing(
    recorder: ActivityRecorder,
    db: Session,
    actor: UserActor,
    environment_id: int,
    probe: None,
    told: list[int],
) -> None:
    with pytest.raises(RuntimeError):
        await mutate(recorder, db, actor, environment_id, fail=True)

    assert told == []


async def test_a_mutation_that_belongs_to_no_environment_tells_the_hook_nothing(
    recorder: ActivityRecorder, db: Session, actor: UserActor, probe: None, told: list[int]
) -> None:
    await mutate(recorder, db, actor, None)

    assert told == []


async def test_a_hook_that_raises_does_not_fail_the_mutation_and_is_logged(
    recorder: ActivityRecorder,
    db: Session,
    actor: UserActor,
    environment_id: int,
    probe: None,
    logged: Callable[[str], list[dict[str, Any]]],
) -> None:
    def broken(environment: int) -> None:
        raise RuntimeError("unreadable")

    install_environment_change_hook(broken)

    await mutate(recorder, db, actor, environment_id)

    [record] = logged(HOOK_FAILED)
    assert (record["environment_id"], record["error"]) == (environment_id, "RuntimeError")
    assert (present(await db.fetch_one("SELECT COUNT(*) AS n FROM probe")))["n"] == 1


class TestHookRemoval:
    def test_removing_the_installed_hook_uninstalls_it(self) -> None:
        def hook(environment: int) -> None: ...

        install_environment_change_hook(hook)

        remove_environment_change_hook(hook)

        assert activity_module._environment_change_hook is None

    def test_removing_a_hook_that_is_not_installed_leaves_the_installed_one(self) -> None:
        def installed(environment: int) -> None: ...

        def other(environment: int) -> None: ...

        install_environment_change_hook(installed)

        remove_environment_change_hook(other)

        assert activity_module._environment_change_hook is installed

    def test_removing_a_hook_when_none_is_installed_does_nothing(self) -> None:
        def hook(environment: int) -> None: ...

        install_environment_change_hook(None)

        remove_environment_change_hook(hook)

        assert activity_module._environment_change_hook is None


class TestRequestRecord:
    async def test_a_committed_environment_mutation_marks_the_request_that_tracks_it(
        self,
        recorder: ActivityRecorder,
        db: Session,
        actor: UserActor,
        environment_id: int,
        probe: None,
    ) -> None:
        scope = object()
        track_request_changes(scope)

        await mutate(recorder, db, actor, environment_id)

        assert request_changed_environment(scope) is True

    @pytest.mark.parametrize("outcome", ["failed", "no-environment"])
    async def test_a_failed_or_environmentless_mutation_leaves_the_record_unset(
        self,
        recorder: ActivityRecorder,
        db: Session,
        actor: UserActor,
        environment_id: int,
        probe: None,
        outcome: str,
    ) -> None:
        scope = object()
        track_request_changes(scope)

        if outcome == "failed":
            with pytest.raises(RuntimeError):
                await mutate(recorder, db, actor, environment_id, fail=True)
        else:
            await mutate(recorder, db, actor, None)

        assert request_changed_environment(scope) is False

    async def test_the_record_belongs_to_the_scope_that_started_it(
        self,
        recorder: ActivityRecorder,
        db: Session,
        actor: UserActor,
        environment_id: int,
        probe: None,
    ) -> None:
        track_request_changes(object())

        await mutate(recorder, db, actor, environment_id)

        assert request_changed_environment(object()) is False

    async def test_a_mutation_outside_any_tracked_request_marks_nothing(
        self,
        recorder: ActivityRecorder,
        db: Session,
        actor: UserActor,
        environment_id: int,
        probe: None,
    ) -> None:
        scope = object()

        await mutate(recorder, db, actor, environment_id)

        assert request_changed_environment(scope) is False
