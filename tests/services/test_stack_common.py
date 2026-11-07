"""Tests for app/services/stack_common.py: concurrent task groups and the labelled-name reads."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.docker_engine.errors import EngineError, EnvironmentUnreachable, ResponseTooLarge
from app.services.containers import ContainerListTooLarge
from app.services.host_objects import NetworkListTooLarge, VolumeListTooLarge
from app.services.object_inspector import EnvironmentUnavailable
from app.services.stack_common import concurrent, read_labelled_names

ENVIRONMENT = 1
DEADLINE_SECONDS = 5
PROJECT_LABEL = "com.docker.compose.project"
NAMESPACE_LABEL = "com.docker.stack.namespace"
SOURCE_LABEL = "com.example.stack"


class Boom(Exception):
    pass


class Other(Exception):
    pass


async def never() -> None:
    await asyncio.Event().wait()


class Cancellable:
    """A task body that waits forever and records that it was cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self) -> None:
        self.started.set()
        try:
            await never()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def test_concurrent_lets_every_task_finish_and_returns_their_results() -> None:
    async def double(value: int) -> int:
        await asyncio.sleep(0)
        return value * 2

    async with concurrent() as group:
        first = group.create_task(double(1))
        second = group.create_task(double(2))

    assert (first.result(), second.result()) == (2, 4)


async def test_concurrent_raises_the_first_failure_as_itself_and_cancels_the_siblings() -> None:
    sibling = Cancellable()

    async def fail_once_the_sibling_runs() -> None:
        await sibling.started.wait()
        raise Boom

    with pytest.raises(Boom) as raised:
        async with asyncio.timeout(DEADLINE_SECONDS):
            async with concurrent() as group:
                group.create_task(sibling.run())
                group.create_task(fail_once_the_sibling_runs())

    assert type(raised.value) is Boom
    assert sibling.cancelled


async def test_concurrent_raises_the_first_created_failure_when_a_later_task_fails_first() -> None:
    async def fail_when_cancelled() -> None:
        try:
            await never()
        except asyncio.CancelledError:
            raise Boom from None

    async def fail_at_once() -> None:
        raise Other

    with pytest.raises(Boom) as raised:
        async with asyncio.timeout(DEADLINE_SECONDS):
            async with concurrent() as group:
                group.create_task(fail_when_cancelled())
                group.create_task(fail_at_once())

    assert type(raised.value) is Boom


async def test_concurrent_raises_the_first_created_failure_when_the_first_task_fails_first() -> (
    None
):
    async def fail_at_once() -> None:
        raise Boom

    async def fail_when_cancelled() -> None:
        try:
            await never()
        except asyncio.CancelledError:
            raise Other from None

    with pytest.raises(Boom) as raised:
        async with asyncio.timeout(DEADLINE_SECONDS):
            async with concurrent() as group:
                group.create_task(fail_at_once())
                group.create_task(fail_when_cancelled())

    assert type(raised.value) is Boom


async def test_concurrent_propagates_cancellation_when_a_child_raises_while_being_cancelled() -> (
    None
):
    started = asyncio.Event()

    async def fail_when_cancelled() -> None:
        started.set()
        try:
            await never()
        except asyncio.CancelledError:
            raise Boom from None

    async def caller() -> None:
        async with concurrent() as group:
            group.create_task(fail_when_cancelled())
            await never()

    task = asyncio.ensure_future(caller())
    await asyncio.wait_for(started.wait(), DEADLINE_SECONDS)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, DEADLINE_SECONDS)
    assert task.cancelled()


async def test_concurrent_raises_a_failure_of_its_own_body_as_itself_and_cancels_the_tasks() -> (
    None
):
    running = Cancellable()

    with pytest.raises(Boom) as raised:
        async with asyncio.timeout(DEADLINE_SECONDS):
            async with concurrent() as group:
                group.create_task(running.run())
                await running.started.wait()
                raise Boom

    assert type(raised.value) is Boom
    assert running.cancelled


def stack(label: str, value: str) -> SimpleNamespace:
    return SimpleNamespace(labels={label: value})


class FakeHost:
    """An Engine client whose list calls answer from tables, meet at a barrier or fail.

    When failing names a kind, the other kinds wait forever.
    """

    def __init__(
        self,
        *,
        barrier: asyncio.Barrier | None = None,
        failing: tuple[str, EngineError] | None = None,
    ) -> None:
        self.containers_by_label: dict[str, list[Any]] = {}
        self.volumes_by_label: dict[str, list[Any]] = {}
        self.networks_by_label: dict[str, list[Any]] = {}
        self._barrier = barrier
        self._failing = failing
        self.containers = SimpleNamespace(list=self._list("containers", self.containers_by_label))
        self.volumes = SimpleNamespace(list=self._list("volumes", self.volumes_by_label))
        self.networks = SimpleNamespace(list=self._list("networks", self.networks_by_label))

    def _list(self, kind: str, table: dict[str, list[Any]]) -> Any:
        async def list_(**kwargs: Any) -> list[Any]:
            [label] = kwargs["filters"]["label"]
            if self._barrier is not None:
                await self._barrier.wait()
            if self._failing is not None:
                failing_kind, error = self._failing
                if kind == failing_kind:
                    raise error
                await never()
            return table.get(label, [])

        return list_


async def test_the_names_are_the_case_folded_union_over_containers_volumes_and_networks() -> None:
    host = FakeHost()
    host.containers_by_label[PROJECT_LABEL] = [stack(PROJECT_LABEL, "Shop")]
    host.volumes_by_label[PROJECT_LABEL] = [stack(PROJECT_LABEL, "DATA")]
    host.containers_by_label[NAMESPACE_LABEL] = [stack(NAMESPACE_LABEL, "Swarm")]
    host.networks_by_label[SOURCE_LABEL] = [stack(SOURCE_LABEL, "Edge")]
    host.networks_by_label[PROJECT_LABEL] = [stack(PROJECT_LABEL, "ignored")]

    names = await read_labelled_names(
        host,  # type: ignore[arg-type]
        [PROJECT_LABEL, NAMESPACE_LABEL],
        network_labels=[SOURCE_LABEL],
    )

    assert names == frozenset({"shop", "data", "swarm", "edge"})


async def test_an_object_without_the_label_or_with_an_empty_value_adds_no_name() -> None:
    host = FakeHost()
    host.containers_by_label[PROJECT_LABEL] = [
        stack("other", "x"),
        stack(PROJECT_LABEL, ""),
        stack(PROJECT_LABEL, "kept"),
    ]

    names = await read_labelled_names(host, [PROJECT_LABEL])  # type: ignore[arg-type]

    assert names == frozenset({"kept"})


async def test_every_read_is_in_flight_at_once() -> None:
    labels = [PROJECT_LABEL, NAMESPACE_LABEL]
    reads = 2 * len(labels) + 1
    host = FakeHost(barrier=asyncio.Barrier(reads))

    async with asyncio.timeout(DEADLINE_SECONDS):
        names = await read_labelled_names(
            host,  # type: ignore[arg-type]
            labels,
            network_labels=[SOURCE_LABEL],
        )

    assert names == frozenset()


READ_FAILURES = [
    pytest.param("containers", ResponseTooLarge(ENVIRONMENT, "big"), ContainerListTooLarge),
    pytest.param("volumes", ResponseTooLarge(ENVIRONMENT, "big"), VolumeListTooLarge),
    pytest.param("networks", ResponseTooLarge(ENVIRONMENT, "big"), NetworkListTooLarge),
    pytest.param("containers", EnvironmentUnreachable(ENVIRONMENT, "down"), EnvironmentUnavailable),
    pytest.param(
        "volumes", EngineError(ENVIRONMENT, "broken", status_code=500), EnvironmentUnavailable
    ),
    pytest.param("networks", EnvironmentUnreachable(ENVIRONMENT, "down"), EnvironmentUnavailable),
]


@pytest.mark.parametrize(("kind", "failure", "expected"), READ_FAILURES)
async def test_one_failing_read_fails_the_whole_answer_and_cancels_the_others(
    kind: str, failure: EngineError, expected: type[Exception]
) -> None:
    host = FakeHost(failing=(kind, failure))

    with pytest.raises(expected) as raised:
        async with asyncio.timeout(DEADLINE_SECONDS):
            await read_labelled_names(
                host,  # type: ignore[arg-type]
                [PROJECT_LABEL],
                network_labels=[SOURCE_LABEL],
            )

    assert type(raised.value) is expected
