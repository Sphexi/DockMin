"""Refusals for the container dockmin runs in: lifecycle, recreate, detach, terminals and create."""

from dataclasses import replace
from typing import Any

import pytest

from app.auth.errors import ResourceNotFound, SelfProtected
from app.self_identity import NOT_CONTAINERISED, SelfIdentity
from app.services.assembly import (
    build_container_services,
    build_network_services,
)
from app.services.container_restrictions import require_no_self_reach
from app.services.containers import ContainerAction
from app.services.terminals import TerminalMode
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, container_id, control
from tests.access_support import container_key as key_of
from tests.container_support import WEB
from tests.docker_engine.container_engine import ContainerEngine
from tests.environment_support import activity_rows
from tests.object_support import add_container
from tests.provisioning_support import IMAGE, OLD, create_form, seed_recreatable
from tests.self_support import (
    SELF_ID,
    SELF_NAME,
    SELF_VOLUME,
    Held,
    home_identity,
    self_protected_rows,
)
from tests.services.conftest import ContainerRig

SESSION_TOKEN = "session-token-for-terminal-tests"
SESSION_ID = 7
BROWSER_ADMIN = replace(ADMIN, session_id=SESSION_ID)
OTHER_CONTAINER = container_id(0xA9)
HIDDEN = container_id(0xA3)
REFUSAL_TEXT = str(SelfProtected.container())


def guarded(rig: ContainerRig, identity: Held) -> ContainerRig:
    """Rebuild the rig's container services around an identity the test can change."""
    rig.services = build_container_services(
        rig.db,
        rig.database,
        rig.runtime,
        rig.engine.as_runtime(),
        rig.access,
        rig.inspector,
        rig.recorder,
        self_identity=identity,
    )
    return rig


@pytest.fixture
def held() -> Held:
    return Held(home_identity())


@pytest.fixture
def own(rig: ContainerRig, host: ContainerEngine, held: Held) -> ContainerRig:
    """The rig with dockmin's own container, a second container and a hidden one on the daemon."""
    host.add(SELF_ID, SELF_NAME, state="running")
    host.add(WEB, "web", state="running")
    host.add(HIDDEN, "hidden", state="running")
    return guarded(rig, held)


async def act(
    rig: ContainerRig, action: ContainerAction, container: str = SELF_ID, who: Any = ADMIN
) -> Any:
    options: dict[str, Any] = {"expected_name": SELF_NAME} if action == "remove" else {}
    return await rig.services.actions.perform(who, ENV_A, container, action, **options)


async def container_failures(rig: ContainerRig) -> list[dict[str, Any]]:
    return [r for r in await activity_rows(rig.db) if r["result"] == "failure"]


@pytest.mark.parametrize(
    ("action", "event"),
    [
        ("stop", "container_stopped"),
        ("restart", "container_restarted"),
        ("kill", "container_killed"),
        ("pause", "container_paused"),
        ("remove", "container_removed"),
    ],
)
async def test_an_action_that_would_take_down_dockmins_container_is_refused_and_sends_nothing(
    own: ContainerRig, host: ContainerEngine, action: ContainerAction, event: str
) -> None:
    with pytest.raises(SelfProtected) as refused:
        await act(own, action)

    [row] = await container_failures(own)
    assert str(refused.value) == REFUSAL_TEXT
    assert (row["action"], row["target_id"], row["environment_id"]) == (event, SELF_ID, ENV_A)
    assert row["detail"]["reason"] == "self_protected"
    assert row["detail"]["message"] == REFUSAL_TEXT
    assert host.mutations() == []
    assert len(own.locks) == 0


@pytest.mark.parametrize(
    ("action", "state", "verb"),
    [("start", "exited", "start"), ("unpause", "paused", "unpause")],
)
async def test_starting_or_unpausing_dockmins_container_is_allowed(
    rig: ContainerRig,
    host: ContainerEngine,
    held: Held,
    action: ContainerAction,
    state: str,
    verb: str,
) -> None:
    host.add(SELF_ID, SELF_NAME, state=state)
    guarded(rig, held)

    outcome = await act(rig, action)

    assert outcome.result == "done"
    assert host.mutations() == [f"POST /containers/{SELF_ID}/{verb}"]
    assert await self_protected_rows(rig.db) == []


async def test_a_user_with_write_access_over_dockmins_container_is_refused_like_an_administrator(
    own: ContainerRig, host: ContainerEngine
) -> None:
    await control(own.access, key_of(0x5E1F, ENV_A), public=True, level="write")

    with pytest.raises(SelfProtected):
        await act(own, "stop", who=STANDARD)

    assert len(await self_protected_rows(own.db)) == 1
    assert host.mutations() == []


async def test_a_user_who_cannot_see_dockmins_container_gets_the_not_found_any_hidden_one_gives(
    own: ContainerRig, host: ContainerEngine
) -> None:
    with pytest.raises(ResourceNotFound) as hidden:
        await act(own, "stop", HIDDEN, STANDARD)
    with pytest.raises(ResourceNotFound) as self_container:
        await act(own, "stop", SELF_ID, STANDARD)

    assert str(self_container.value) == str(hidden.value)
    assert await self_protected_rows(own.db) == []
    assert host.mutations() == []


async def test_a_batch_marks_dockmins_container_protected_and_acts_on_the_others(
    own: ContainerRig, host: ContainerEngine
) -> None:
    result = await own.services.actions.run_batch(ADMIN, ENV_A, "stop", [SELF_ID, WEB])

    assert [(i.container_id, i.status) for i in result.items] == [
        (SELF_ID, "protected"),
        (WEB, "done"),
    ]
    assert result.items[0].message == REFUSAL_TEXT
    assert (result.succeeded, result.failed) == (1, 1)
    assert host.mutations() == [f"POST /containers/{WEB}/stop"]
    assert len(await self_protected_rows(own.db)) == 1


async def test_a_refusal_follows_the_identity_the_process_holds_at_the_time_of_the_call(
    own: ContainerRig, held: Held, host: ContainerEngine
) -> None:
    held.identity = home_identity(container=OTHER_CONTAINER)

    outcome = await act(own, "stop")

    assert outcome.result == "done"
    assert host.mutations() == [f"POST /containers/{SELF_ID}/stop"]


class TestRecreate:
    @pytest.fixture
    def recreating(self, rig: ContainerRig, host: ContainerEngine) -> ContainerRig:
        seed_recreatable(host)
        return guarded(rig, Held(home_identity(container=OLD)))

    async def test_recreating_dockmins_container_is_refused_before_anything_is_sent(
        self, recreating: ContainerRig, host: ContainerEngine
    ) -> None:
        with pytest.raises(SelfProtected):
            await recreating.services.recreation.recreate(ADMIN, ENV_A, OLD, expected_name="web")

        [row] = await self_protected_rows(recreating.db)
        assert (row["action"], row["target_id"]) == ("container_recreated", OLD)
        assert host.mutations() == []
        assert OLD in host.boxes

    async def test_the_recreate_confirmation_refuses_dockmins_container_up_front(
        self, recreating: ContainerRig
    ) -> None:
        with pytest.raises(SelfProtected):
            await recreating.services.recreation.recreate_confirmation(ADMIN, ENV_A, OLD)


class TestNetworkDetach:
    async def test_detaching_dockmins_container_from_a_network_is_refused(
        self, rig: ContainerRig, host: ContainerEngine
    ) -> None:
        network_id = host.add_network("lan", 0x51)
        add_container(host, 0x5E1F, SELF_NAME, state="running", networks={"lan": network_id})
        nets = build_network_services(
            rig.db,
            rig.database,
            rig.runtime,
            rig.engine.as_runtime(),
            rig.access,
            rig.inspector,
            rig.recorder,
            self_identity=Held(home_identity()),
        )

        with pytest.raises(SelfProtected) as refused:
            await nets.attachments.disconnect(ADMIN, ENV_A, SELF_ID, network_id)

        [row] = await self_protected_rows(rig.db)
        assert str(refused.value) == str(SelfProtected.detach())
        assert (row["action"], row["target_id"]) == ("network_disconnected", SELF_ID)
        assert host.network_bodies("disconnect") == []


class TestTerminals:
    @pytest.mark.parametrize("mode", ["exec", "attach"])
    async def test_a_terminal_in_dockmins_container_is_refused_with_one_row_per_attempt(
        self, own: ContainerRig, host: ContainerEngine, mode: TerminalMode
    ) -> None:
        with pytest.raises(SelfProtected) as refused:
            await own.services.terminals.prepare(
                BROWSER_ADMIN, SESSION_TOKEN, ENV_A, SELF_ID, mode, {}
            )

        [row] = await self_protected_rows(own.db)
        assert str(refused.value) == str(SelfProtected.terminal())
        assert row["target_id"] == SELF_ID
        assert row["detail"]["message"] == str(SelfProtected.terminal())
        assert host.mutations() == []

    async def test_a_ticket_issued_before_dockmin_knew_itself_is_refused_when_redeemed(
        self, own: ContainerRig, held: Held, host: ContainerEngine
    ) -> None:
        held.identity = home_identity(container=OTHER_CONTAINER)
        issued = await own.services.terminals.prepare(
            BROWSER_ADMIN, SESSION_TOKEN, ENV_A, SELF_ID, "exec", {}
        )
        held.identity = home_identity()

        with pytest.raises(SelfProtected):
            await own.services.terminals.open(
                BROWSER_ADMIN, SESSION_TOKEN, ENV_A, SELF_ID, issued.ticket
            )

        assert len(await self_protected_rows(own.db)) == 1
        assert host.mutations() == []


class TestCreateReach:
    @pytest.fixture
    def creating(self, rig: ContainerRig, host: ContainerEngine) -> ContainerRig:
        host.add_image(IMAGE)
        host.add(SELF_ID, SELF_NAME, state="running")
        identity = home_identity(volumes=(SELF_VOLUME,))
        return guarded(rig, Held(identity))

    async def test_creating_a_container_that_mounts_a_volume_dockmin_has_mounted_is_refused(
        self, creating: ContainerRig, host: ContainerEngine
    ) -> None:
        form = create_form(
            **{"mount-0-type": "volume", "mount-0-source": SELF_VOLUME, "mount-0-target": "/x"}
        )

        with pytest.raises(SelfProtected):
            await creating.services.creation.create(ADMIN, ENV_A, form)

        assert host.calls("POST", "/containers/create") == []
        assert len(host.boxes) == 1


SELF_REFERENCES = [SELF_NAME, f"/{SELF_NAME}", SELF_ID, SELF_ID[:12]]


class TestSelfReachRule:
    @pytest.fixture
    def identity(self) -> SelfIdentity:
        return home_identity(volumes=(SELF_VOLUME,))

    @pytest.mark.parametrize("mode", ["PidMode", "IpcMode", "NetworkMode"])
    @pytest.mark.parametrize("reference", SELF_REFERENCES)
    def test_joining_the_namespace_of_dockmins_container_is_refused(
        self, identity: SelfIdentity, mode: str, reference: str
    ) -> None:
        body = {"HostConfig": {mode: f"container:{reference}"}}

        with pytest.raises(SelfProtected):
            require_no_self_reach(body, environment_id=ENV_A, identity=identity)

    @pytest.mark.parametrize("source", [SELF_NAME, f"{SELF_NAME}:ro", SELF_ID[:12]])
    def test_taking_the_volumes_of_dockmins_container_is_refused(
        self, identity: SelfIdentity, source: str
    ) -> None:
        body = {"HostConfig": {"VolumesFrom": [source]}}

        with pytest.raises(SelfProtected):
            require_no_self_reach(body, environment_id=ENV_A, identity=identity)

    @pytest.mark.parametrize(
        "host_config",
        [
            pytest.param({"Binds": [f"{SELF_VOLUME}:/data:ro"]}, id="bind-string"),
            pytest.param(
                {"Mounts": [{"Type": "volume", "Source": SELF_VOLUME, "Target": "/data"}]},
                id="mount-entry",
            ),
        ],
    )
    def test_mounting_a_volume_dockmins_container_has_mounted_is_refused(
        self, identity: SelfIdentity, host_config: dict[str, Any]
    ) -> None:
        with pytest.raises(SelfProtected):
            require_no_self_reach(
                {"HostConfig": host_config}, environment_id=ENV_A, identity=identity
            )

    @pytest.mark.parametrize(
        ("body", "environment_id"),
        [
            pytest.param({"HostConfig": {"PidMode": "container:other"}}, ENV_A, id="other-name"),
            pytest.param({"HostConfig": {"Binds": ["cache:/data"]}}, ENV_A, id="other-volume"),
            pytest.param({"HostConfig": {"Binds": ["/srv/data:/data"]}}, ENV_A, id="host-path"),
            pytest.param({"HostConfig": {"NetworkMode": "container:"}}, ENV_A, id="empty-name"),
            pytest.param({"HostConfig": {"PidMode": f"container:{SELF_NAME}"}}, ENV_B, id="away"),
            pytest.param({"Image": "nginx:1"}, ENV_A, id="no-host-config"),
        ],
    )
    def test_a_body_that_does_not_reach_dockmins_container_passes(
        self, identity: SelfIdentity, body: dict[str, Any], environment_id: int
    ) -> None:
        require_no_self_reach(body, environment_id=environment_id, identity=identity)

    def test_nothing_is_refused_when_dockmin_runs_in_no_container(self) -> None:
        body = {"HostConfig": {"PidMode": f"container:{SELF_NAME}"}}

        require_no_self_reach(body, environment_id=ENV_A, identity=NOT_CONTAINERISED)


class TestRecreateReach:
    @pytest.fixture
    def joining(self, rig: ContainerRig, host: ContainerEngine) -> ContainerRig:
        host.add(SELF_ID, SELF_NAME, state="running")
        seed_recreatable(host, host_config={"PidMode": f"container:{SELF_NAME}"})
        return guarded(rig, Held(home_identity()))

    async def test_recreating_a_container_that_joins_dockmins_namespaces_is_refused_untouched(
        self, joining: ContainerRig, host: ContainerEngine
    ) -> None:
        mutations = host.mutations()

        with pytest.raises(SelfProtected):
            await joining.services.recreation.recreate(ADMIN, ENV_A, OLD, expected_name="web")

        [row] = await self_protected_rows(joining.db)
        assert (row["action"], row["target_id"]) == ("container_recreated", OLD)
        assert host.mutations() == mutations
        assert OLD in host.boxes

    async def test_the_recreate_confirmation_refuses_such_a_container_up_front(
        self, joining: ContainerRig
    ) -> None:
        with pytest.raises(SelfProtected):
            await joining.services.recreation.recreate_confirmation(ADMIN, ENV_A, OLD)
