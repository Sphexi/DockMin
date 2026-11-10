"""The Instance service: when a restart is refused, what is recorded first, and how it is sent."""

import asyncio
import dataclasses
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.auth.errors import PermissionDenied
from app.docker_engine.errors import EngineError, EnvironmentUnreachable, RequestOutcomeUnknown
from app.self_identity import NOT_CONTAINERISED, SelfIdentity
from app.services.instance import (
    DEFAULT_STOP_TIMEOUT,
    InstanceService,
    ProcessBoot,
    RestartDispatcher,
    RestartRefused,
    RestartRequest,
    UpdateSupport,
    newer_tags,
    update_refusal,
)
from app.services.restore_gate import RestoreGate
from app.services.self_update import UpdateDispatcher, reserve_update
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD
from tests.docker_engine.container_engine import ContainerEngine
from tests.environment_support import activity_rows
from tests.self_support import SELF_ID, SELF_NAME, Held, home_identity
from tests.services.conftest import ContainerRig

CUSTOM_STOP_TIMEOUT = 12
STARTED_AT = datetime(2030, 1, 1, tzinfo=UTC)
RESTART_ACTION = "instance_restart_requested"
DISPATCH_DELAY_SECONDS = 0.5
SENT = "instance_restart_sent"
UNCONFIRMED = "instance_restart_unconfirmed"
FAILED = "instance_restart_failed"
DROPPED = EnvironmentUnreachable(ENV_B, "dropped")
UNKNOWN = RequestOutcomeUnknown(ENV_B, "no answer")
REFUSED = EngineError(ENV_B, "refused")


@pytest.fixture
def held() -> Held:
    return Held(home_identity(ENV_B, kind="unix_socket"))


@pytest.fixture
def gate() -> RestoreGate:
    return RestoreGate()


@pytest.fixture
def instance(
    rig: ContainerRig, host: ContainerEngine, held: Held, gate: RestoreGate
) -> InstanceService:
    host.add(SELF_ID, SELF_NAME, state="running", stop_timeout=CUSTOM_STOP_TIMEOUT)
    return InstanceService(
        db=rig.db,
        activity=rig.recorder,
        inspector=rig.inspector,
        identity=held,
        gate=gate,
        boot=ProcessBoot.at(STARTED_AT),
    )


async def restart_rows(rig: ContainerRig) -> list[dict[str, Any]]:
    return [row for row in await activity_rows(rig.db) if row["action"] == RESTART_ACTION]


class TestRestartRefusals:
    @pytest.mark.parametrize(
        ("identity", "reason"),
        [
            pytest.param(NOT_CONTAINERISED, "not_containerised", id="no-container"),
            pytest.param(home_identity(ENV_A, kind="tcp_tls"), "not_restartable", id="remote-only"),
        ],
    )
    async def test_a_restart_is_refused_without_a_container_or_a_local_route_to_it(
        self,
        rig: ContainerRig,
        instance: InstanceService,
        held: Held,
        host: ContainerEngine,
        identity: Any,
        reason: str,
    ) -> None:
        held.identity = identity

        with pytest.raises(RestartRefused) as refused:
            await instance.request_restart(ADMIN)

        [row] = await restart_rows(rig)
        assert refused.value.reason == reason
        assert (row["result"], row["detail"]["reason"]) == ("failure", reason)
        assert host.mutations() == []

    @pytest.mark.parametrize("phase", ["engaged", "swapping", "failed"])
    async def test_a_restart_is_refused_while_a_restore_holds_the_instance(
        self,
        rig: ContainerRig,
        instance: InstanceService,
        gate: RestoreGate,
        host: ContainerEngine,
        phase: Any,
    ) -> None:
        gate.phase = phase

        with pytest.raises(RestartRefused) as refused:
            await instance.request_restart(ADMIN)

        [row] = await restart_rows(rig)
        assert refused.value.reason == "restore_in_progress"
        assert row["detail"]["reason"] == "restore_in_progress"
        assert host.mutations() == []

    async def test_a_restart_is_refused_when_the_container_is_gone_from_the_host(
        self, rig: ContainerRig, instance: InstanceService, host: ContainerEngine
    ) -> None:
        del host.boxes[SELF_ID]

        with pytest.raises(RestartRefused) as refused:
            await instance.request_restart(ADMIN)

        assert refused.value.reason == "container_missing"
        assert [r["detail"]["reason"] for r in await restart_rows(rig)] == ["container_missing"]

    async def test_a_standard_user_may_neither_restart_nor_view(
        self, instance: InstanceService, host: ContainerEngine
    ) -> None:
        with pytest.raises(PermissionDenied):
            await instance.request_restart(STANDARD)
        with pytest.raises(PermissionDenied):
            await instance.view(STANDARD)

        assert host.mutations() == []


class TestAcceptedRestart:
    async def test_the_row_is_committed_before_the_restart_is_sent_and_names_the_container(
        self, rig: ContainerRig, instance: InstanceService, host: ContainerEngine
    ) -> None:
        seen_when_sent: list[int] = []

        async def note_rows() -> None:
            seen_when_sent.append(len(await restart_rows(rig)))

        host.on("restart", note_rows)

        request = await instance.request_restart(ADMIN)
        committed = await restart_rows(rig)
        await request.send()

        [row] = committed
        assert (row["result"], row["target_type"], row["target_id"]) == (
            "success",
            "instance",
            SELF_ID,
        )
        assert row["detail"] == {
            "container_name": SELF_NAME,
            "environment_id": ENV_B,
            "stop_timeout": CUSTOM_STOP_TIMEOUT,
        }
        assert seen_when_sent == [1]
        [call] = host.calls("POST", f"/containers/{SELF_ID}/restart")
        assert call.query["t"] == str(CUSTOM_STOP_TIMEOUT)
        assert (request.environment_id, request.container_name) == (ENV_B, SELF_NAME)
        assert request.stop_timeout == CUSTOM_STOP_TIMEOUT

    async def test_nothing_is_sent_by_the_act_of_requesting(
        self, instance: InstanceService, host: ContainerEngine
    ) -> None:
        await instance.request_restart(ADMIN)

        assert host.mutations() == []

    @pytest.mark.parametrize("configured", [None, -1])
    async def test_the_default_stop_timeout_applies_when_the_container_sets_none(
        self, instance: InstanceService, host: ContainerEngine, configured: int | None
    ) -> None:
        host.boxes[SELF_ID].stop_timeout = configured

        request = await instance.request_restart(ADMIN)

        assert request.stop_timeout == DEFAULT_STOP_TIMEOUT

    async def test_a_failed_row_write_refuses_the_restart_and_returns_nothing_to_send(
        self,
        rig: ContainerRig,
        instance: InstanceService,
        host: ContainerEngine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        @asynccontextmanager
        async def broken() -> AsyncIterator[None]:
            raise sqlite3.OperationalError("disk I/O error")
            yield

        monkeypatch.setattr(rig.db, "transaction", broken)

        with pytest.raises(RestartRefused) as refused:
            await instance.request_restart(ADMIN)

        assert refused.value.reason == "record_failed"
        assert host.mutations() == []


class TestView:
    async def test_the_view_names_the_container_its_homes_and_whether_a_restart_would_be_accepted(
        self, instance: InstanceService
    ) -> None:
        view = await instance.view(ADMIN)

        assert (view.containerised, view.container_id, view.can_restart) == (True, SELF_ID, True)
        assert [(h.environment_id, h.restarts_through) for h in view.homes] == [(ENV_B, True)]
        assert view.identified_by is not None
        assert view.incomplete is False
        assert view.boot.boot_id

    async def test_the_view_gives_the_reason_a_restart_is_unavailable(
        self, instance: InstanceService, gate: RestoreGate
    ) -> None:
        gate.phase = "engaged"

        view = await instance.view(ADMIN)

        assert (view.can_restart, view.restart_refusal) == (False, "restore_in_progress")
        assert view.restart_message

    async def test_the_view_lists_environments_that_could_not_be_read(
        self, instance: InstanceService, held: Held
    ) -> None:
        held.identity = SelfIdentity(container_id=SELF_ID, source="mountinfo", unreadable=(ENV_A,))

        view = await instance.view(ADMIN)

        assert [u.environment_id for u in view.unreadable] == [ENV_A]
        assert view.incomplete is True


class TestDispatcher:
    @pytest.fixture
    def slept(self) -> list[float]:
        return []

    @pytest.fixture
    def dispatcher(self, slept: list[float]) -> RestartDispatcher:
        async def sleep(seconds: float) -> None:
            slept.append(seconds)

        return RestartDispatcher(sleep=sleep)

    def request_that(
        self, error: Exception | None, sent: list[int] | None = None
    ) -> RestartRequest:
        async def send() -> None:
            if sent is not None:
                sent.append(1)
            if error is not None:
                raise error

        return RestartRequest(ENV_B, SELF_ID, SELF_NAME, DEFAULT_STOP_TIMEOUT, send)

    async def finish(self, dispatcher: RestartDispatcher) -> None:
        for _ in range(100):
            if dispatcher.pending == 0:
                return
            await asyncio.sleep(0)
        raise AssertionError("the dispatched restart never finished")

    async def test_a_dispatched_restart_waits_the_delay_then_sends_once(
        self, dispatcher: RestartDispatcher, slept: list[float]
    ) -> None:
        sent: list[int] = []

        dispatcher.dispatch(self.request_that(None, sent))
        assert dispatcher.pending == 1
        await self.finish(dispatcher)

        assert slept == [DISPATCH_DELAY_SECONDS]
        assert sent == [1]

    @pytest.mark.parametrize(
        ("stopping", "error", "message"),
        [
            pytest.param(True, DROPPED, SENT, id="dropped-while-stopping"),
            pytest.param(True, UNKNOWN, SENT, id="unknown-while-stopping"),
            pytest.param(False, UNKNOWN, UNCONFIRMED, id="unknown-while-running"),
            pytest.param(False, DROPPED, FAILED, id="dropped-while-running"),
            pytest.param(False, REFUSED, FAILED, id="refused-while-running"),
            pytest.param(True, REFUSED, FAILED, id="refused-while-stopping"),
            pytest.param(False, RuntimeError("bug"), FAILED, id="unexpected"),
        ],
    )
    async def test_the_outcome_of_the_send_is_logged_under_one_message_and_never_raised(
        self,
        dispatcher: RestartDispatcher,
        logged: Callable[[str], list[dict[str, Any]]],
        stopping: bool,
        error: Exception,
        message: str,
    ) -> None:
        if stopping:
            dispatcher.mark_stopping()

        dispatcher.dispatch(self.request_that(error))
        await self.finish(dispatcher)

        [record] = logged(message)
        assert record["environment_id"] == ENV_B
        assert record["error"] == type(error).__name__
        assert (
            sum(
                len(logged(name))
                for name in (
                    "instance_restart_sent",
                    "instance_restart_unconfirmed",
                    "instance_restart_failed",
                )
            )
            == 1
        )


UPDATE_ID = "ab" * 16
HEALTH_SECONDS = 180
RUNNING_VERSION = "1.0.0"
IN_PROGRESS = "An update is in progress"
MANY_TAGS = tuple(f"1.{minor}.0" for minor in range(1, 13))
TEN_NEWEST = tuple(f"1.{minor}.0" for minor in range(12, 2, -1))


def local_home() -> SelfIdentity:
    return home_identity(ENV_B, kind="unix_socket")


@pytest.mark.parametrize(
    ("identity", "phase", "expected"),
    [
        pytest.param(local_home(), "open", None, id="update-possible"),
        pytest.param(NOT_CONTAINERISED, "open", "not_containerised", id="not-containerised"),
        pytest.param(
            dataclasses.replace(local_home(), unreadable=(ENV_A,)),
            "open",
            "identity_incomplete",
            id="incomplete",
        ),
        pytest.param(
            home_identity(ENV_B, kind="unix_socket", compose_project="Shop"),
            "open",
            "compose_project",
            id="compose-project",
        ),
        pytest.param(
            home_identity(ENV_B, kind="unix_socket", swarm_service="service"),
            "open",
            "swarm_task",
            id="swarm-task",
        ),
        pytest.param(
            home_identity(ENV_B, kind="tcp_tls"), "open", "not_restartable", id="remote-only"
        ),
        pytest.param(local_home(), "engaged", "restore_in_progress", id="restoring"),
        pytest.param(
            dataclasses.replace(local_home(), unreadable=(ENV_A,)),
            "engaged",
            "identity_incomplete",
            id="incomplete-before-restoring",
        ),
        pytest.param(
            home_identity(ENV_B, kind="tcp_tls", compose_project="Shop"),
            "open",
            "compose_project",
            id="compose-before-remote",
        ),
    ],
)
def test_an_update_is_refused_for_the_first_reason_the_identity_and_the_restore_gate_give(
    identity: SelfIdentity, phase: Any, expected: str | None
) -> None:
    gate = RestoreGate()
    gate.phase = phase

    assert update_refusal(identity, gate) == expected


@pytest.mark.parametrize(
    ("tags", "running", "offered"),
    [
        pytest.param(
            ("0.9.0", "1.0.0", "1.1.0", "1.2.0", "latest", "1.1.0-rc1"),
            RUNNING_VERSION,
            ("1.2.0", "1.1.0"),
            id="newer-releases-only",
        ),
        pytest.param(
            ("1.9.0", "1.10.0", "1.2.0"), "1.0.0", ("1.10.0", "1.9.0", "1.2.0"), id="numeric-order"
        ),
        pytest.param(("1.0.0",), RUNNING_VERSION, (), id="nothing-newer"),
        pytest.param(
            ("v2.0.0", "01.2.3", "1.2", "1.2.3.4", "2.0.0+build"), "1.0.0", (), id="not-releases"
        ),
        pytest.param(
            ("0.9.0", "1.0.0", "latest"), "0+local", ("1.0.0", "0.9.0"), id="running-not-a-release"
        ),
        pytest.param(MANY_TAGS, "1.0.0", TEN_NEWEST, id="newest-ten"),
    ],
)
def test_the_tags_offered_are_the_newest_releases_newer_than_the_running_version(
    tags: tuple[str, ...], running: str, offered: tuple[str, ...]
) -> None:
    assert newer_tags(tags, running) == offered


class TestRestartDuringAnUpdate:
    @pytest.fixture
    def dispatcher(self) -> UpdateDispatcher:
        return UpdateDispatcher(cast(Any, SimpleNamespace()))

    @pytest.fixture
    def updating(
        self,
        rig: ContainerRig,
        host: ContainerEngine,
        held: Held,
        gate: RestoreGate,
        dispatcher: UpdateDispatcher,
        tmp_path: Path,
    ) -> InstanceService:
        host.add(SELF_ID, SELF_NAME, state="running")
        support = UpdateSupport(
            data_dir=tmp_path,
            health_timeout_seconds=HEALTH_SECONDS,
            bindings=cast(Any, None),
            resolver=cast(Any, None),
            manifests=cast(Any, None),
            checks=cast(Any, None),
            settings=cast(Any, None),
            events=cast(Any, None),
            dispatcher=dispatcher,
        )
        return InstanceService(
            db=rig.db,
            activity=rig.recorder,
            inspector=rig.inspector,
            identity=held,
            gate=gate,
            boot=ProcessBoot.at(STARTED_AT),
            update=support,
        )

    async def test_a_restart_is_refused_while_this_process_holds_an_update(
        self,
        rig: ContainerRig,
        updating: InstanceService,
        dispatcher: UpdateDispatcher,
        host: ContainerEngine,
    ) -> None:
        dispatcher.claim(UPDATE_ID)

        with pytest.raises(RestartRefused) as refused:
            await updating.request_restart(ADMIN)

        [row] = await restart_rows(rig)
        assert refused.value.reason == "update_in_progress"
        assert IN_PROGRESS in str(refused.value)
        assert (row["result"], row["detail"]["reason"]) == ("failure", "update_in_progress")
        assert host.mutations() == []

    async def test_a_restart_is_refused_while_an_update_directory_is_left_in_the_data_directory(
        self,
        rig: ContainerRig,
        updating: InstanceService,
        host: ContainerEngine,
        tmp_path: Path,
    ) -> None:
        reserve_update(tmp_path, UPDATE_ID)

        with pytest.raises(RestartRefused) as refused:
            await updating.request_restart(ADMIN)

        assert refused.value.reason == "update_in_progress"
        assert host.mutations() == []

    async def test_the_view_reports_the_restart_as_unavailable_while_an_update_is_held(
        self, updating: InstanceService, dispatcher: UpdateDispatcher
    ) -> None:
        dispatcher.claim(UPDATE_ID)

        view = await updating.view(ADMIN)

        assert (view.can_restart, view.restart_refusal) == (False, "update_in_progress")
        assert view.update is None

    async def test_a_restart_goes_through_when_no_update_is_held_or_left(
        self, updating: InstanceService, host: ContainerEngine
    ) -> None:
        request = await updating.request_restart(ADMIN)

        assert request.container_name == SELF_NAME
        assert host.mutations() == []
