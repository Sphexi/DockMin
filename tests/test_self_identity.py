"""Which container dockmin runs in: /proc parsing, protection checks and the background resolver."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from app.self_identity import (
    MOUNTINFO_FILE,
    NOT_CONTAINERISED,
    Candidate,
    ContainerFacts,
    EnvironmentRef,
    HomeEnvironment,
    SelfIdentity,
    SelfIdentityResolver,
    container_id_from_cgroup,
    container_id_from_mountinfo,
    find_candidate,
)

CONTAINER = "a1" * 32
OTHER_CONTAINER = "b2" * 32
HOSTNAME = CONTAINER[:12]
LOCAL = EnvironmentRef(1, "unix_socket")
REMOTE = EnvironmentRef(2, "tcp_tls")
SWARM_REMOTE = EnvironmentRef(3, "tcp_tls", is_swarm=True)
SERVICE_ID = "s" * 25
NODE_ID = "n" * 25
HOST_FILES = ("/etc/hostname", "/etc/hosts", "/etc/resolv.conf")
FIRST_RETRY_SECONDS = 5.0
RETRY_CEILING_SECONDS = 300.0
BACKOFF_UNTIL_CEILING = [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]


def mount_line(root: str, mount_point: str) -> str:
    return f"2001 1990 8:1 {root} {mount_point} rw,relatime master:1 - ext4 /dev/sda1 rw"


def container_mounts(container_id: str, data_root: str = "/var/lib/docker") -> str:
    files = {
        "/etc/hostname": "hostname",
        "/etc/hosts": "hosts",
        "/etc/resolv.conf": "resolv.conf",
    }
    return "\n".join(
        mount_line(f"{data_root}/containers/{container_id}/{name}", mount_point)
        for mount_point, name in files.items()
    )


class TestMountinfo:
    def test_the_id_is_read_from_the_roots_of_the_three_host_files(self) -> None:
        text = container_mounts(CONTAINER)

        assert container_id_from_mountinfo(text) == CONTAINER

    def test_the_id_is_found_with_a_private_cgroup_namespace_and_a_custom_data_root(self) -> None:
        text = container_mounts(CONTAINER, data_root="/mnt/volumes/docker-data")

        assert container_id_from_mountinfo(text) == CONTAINER

    def test_escaped_characters_in_a_path_are_decoded_before_matching(self) -> None:
        text = container_mounts(CONTAINER, data_root="/srv/docker\\040data")

        assert container_id_from_mountinfo(text) == CONTAINER

    def test_other_mounts_and_short_lines_are_ignored(self) -> None:
        text = "\n".join(
            [
                "malformed",
                mount_line("/", "/"),
                mount_line(f"/var/lib/docker/containers/{OTHER_CONTAINER}/hostname", "/srv/other"),
                container_mounts(CONTAINER),
            ]
        )

        assert container_id_from_mountinfo(text) == CONTAINER

    def test_files_that_name_different_containers_give_no_id(self) -> None:
        text = "\n".join(
            [
                mount_line(f"/var/lib/docker/containers/{CONTAINER}/hostname", "/etc/hostname"),
                mount_line(f"/var/lib/docker/containers/{OTHER_CONTAINER}/hosts", "/etc/hosts"),
            ]
        )

        assert container_id_from_mountinfo(text) is None

    def test_a_host_file_mounted_over_from_elsewhere_is_not_counted(self) -> None:
        text = "\n".join(
            [
                mount_line(f"/var/lib/docker/containers/{OTHER_CONTAINER}/hostname", HOST_FILES[0]),
                mount_line("/srv/custom-hostname", HOST_FILES[0]),
                mount_line(f"/var/lib/docker/containers/{CONTAINER}/hosts", HOST_FILES[1]),
            ]
        )

        assert container_id_from_mountinfo(text) == CONTAINER

    def test_a_short_id_is_not_an_id(self) -> None:
        text = mount_line(f"/var/lib/docker/containers/{CONTAINER[:12]}/hostname", HOST_FILES[0])

        assert container_id_from_mountinfo(text) is None


class TestCgroup:
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(f"0::/system.slice/docker-{CONTAINER}.scope\n", id="v2-systemd-scope"),
            pytest.param(f"0::/docker/{CONTAINER}\n", id="v2-cgroupfs"),
            pytest.param(
                f"12:memory:/docker/{CONTAINER}\n11:cpu,cpuacct:/docker/{CONTAINER}\n",
                id="v1-every-controller-agrees",
            ),
            pytest.param(
                f"1:name=systemd:/kubepods/burstable/pod0123/{CONTAINER}\n", id="v1-nested-path"
            ),
        ],
    )
    def test_the_single_id_named_by_the_paths_is_returned(self, text: str) -> None:
        assert container_id_from_cgroup(text) == CONTAINER

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("0::/\n", id="v2-private-namespace"),
            pytest.param("", id="empty"),
            pytest.param(f"0::/docker/{CONTAINER[:63]}\n", id="id-one-digit-short"),
            pytest.param(
                f"12:memory:/docker/{CONTAINER}\n11:cpu:/docker/{OTHER_CONTAINER}\n",
                id="controllers-disagree",
            ),
        ],
    )
    def test_no_id_is_returned_when_none_or_several_are_named(self, text: str) -> None:
        assert container_id_from_cgroup(text) is None


class TestFindCandidate:
    def test_mounts_win_over_the_cgroup(self) -> None:
        candidate = find_candidate(container_mounts(CONTAINER), f"0::/docker/{OTHER_CONTAINER}\n")

        assert candidate == Candidate(CONTAINER, "mountinfo")

    def test_the_cgroup_is_used_when_the_mounts_name_nothing(self) -> None:
        candidate = find_candidate(mount_line("/", "/"), f"0::/docker/{CONTAINER}\n")

        assert candidate == Candidate(CONTAINER, "cgroup")

    def test_no_candidate_is_found_outside_a_container(self) -> None:
        assert find_candidate(mount_line("/", "/"), "0::/\n") is None


class TestProtection:
    @pytest.fixture
    def identity(self) -> SelfIdentity:
        return SelfIdentity(
            container_id=CONTAINER,
            source="mountinfo",
            verified=(HomeEnvironment(1, "unix_socket", "dockmin"),),
            volumes={1: frozenset({"dockmin-data"})},
            compose_project={1: "dockmin-stack"},
            swarm_service={1: SERVICE_ID},
            swarm_node={1: NODE_ID},
            restartable_via=1,
        )

    def test_the_container_id_is_protected_in_every_environment(
        self, identity: SelfIdentity
    ) -> None:
        assert identity.protects_container(CONTAINER)
        assert not identity.protects_container(OTHER_CONTAINER)

    @pytest.mark.parametrize(
        ("environment_id", "protected"), [(1, True), (2, False)], ids=["home", "elsewhere"]
    )
    def test_a_volume_is_protected_only_in_a_home_environment(
        self, identity: SelfIdentity, environment_id: int, protected: bool
    ) -> None:
        assert identity.protects_volume(environment_id, "dockmin-data") is protected

    def test_an_unlisted_volume_is_not_protected(self, identity: SelfIdentity) -> None:
        assert not identity.protects_volume(1, "other-data")

    @pytest.mark.parametrize("name", ["dockmin-stack", "Dockmin-Stack", "DOCKMIN-STACK"])
    def test_a_stack_name_is_compared_without_regard_to_case(
        self, identity: SelfIdentity, name: str
    ) -> None:
        assert identity.protects_stack(1, name)

    def test_a_stack_is_protected_only_in_a_home_environment(self, identity: SelfIdentity) -> None:
        assert not identity.protects_stack(2, "dockmin-stack")
        assert not identity.protects_stack(1, "other-stack")

    def test_a_service_and_a_node_are_protected_only_in_a_home_environment(
        self, identity: SelfIdentity
    ) -> None:
        assert identity.protects_service(1, SERVICE_ID)
        assert not identity.protects_service(2, SERVICE_ID)
        assert not identity.protects_service(1, "x" * 25)
        assert identity.protects_node(1, NODE_ID)
        assert not identity.protects_node(2, NODE_ID)

    def test_home_environments_are_the_verified_ones(self, identity: SelfIdentity) -> None:
        assert identity.is_home(1)
        assert not identity.is_home(2)

    def test_an_identity_with_no_container_protects_nothing(self) -> None:
        assert not NOT_CONTAINERISED.containerised
        assert not NOT_CONTAINERISED.protects_container(CONTAINER)
        assert not NOT_CONTAINERISED.protects_volume(1, "dockmin-data")
        assert not NOT_CONTAINERISED.protects_stack(1, "dockmin-stack")
        assert not NOT_CONTAINERISED.protects_service(1, SERVICE_ID)
        assert not NOT_CONTAINERISED.protects_node(1, NODE_ID)

    @pytest.mark.parametrize(
        ("identity", "incomplete"),
        [
            pytest.param(SelfIdentity(CONTAINER, "mountinfo"), False, id="clean"),
            pytest.param(SelfIdentity(CONTAINER, "mountinfo", unreadable=(2,)), True, id="unread"),
            pytest.param(
                SelfIdentity(CONTAINER, "mountinfo", listing_failed=True), True, id="no-listing"
            ),
        ],
    )
    def test_an_identity_is_incomplete_when_an_environment_or_the_listing_failed(
        self, identity: SelfIdentity, incomplete: bool
    ) -> None:
        assert identity.incomplete is incomplete


class ScriptedProbe:
    """Answers the resolver from a table; an Exception value is raised for that environment."""

    def __init__(self, refs: Sequence[EnvironmentRef]) -> None:
        self.refs = list(refs)
        self.answers: dict[int, ContainerFacts | Exception | None] = {}
        self.listing_error: Exception | None = None

    async def environments(self) -> Sequence[EnvironmentRef]:
        if self.listing_error is not None:
            raise self.listing_error
        return self.refs

    async def inspect(self, ref: EnvironmentRef, container_id: str) -> ContainerFacts | None:
        answer = self.answers.get(ref.environment_id)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Sleeper:
    """Stands in for asyncio.sleep: records each delay, then returns at once or never."""

    def __init__(self, *, block: bool = False) -> None:
        self.delays: list[float] = []
        self.entered = asyncio.Event()
        self.after: Callable[[int], None] | None = None
        self._block = block

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.entered.set()
        if self.after is not None:
            self.after(len(self.delays))
        if self._block:
            await asyncio.Event().wait()
        await asyncio.sleep(0)


def facts(
    container_id: str = CONTAINER,
    *,
    name: str = "dockmin",
    hostname: str | None = HOSTNAME,
    volumes: frozenset[str] = frozenset(),
    compose_project: str | None = None,
    swarm_service_id: str | None = None,
    swarm_node_id: str | None = None,
    mount_sources: frozenset[str] = frozenset(),
) -> ContainerFacts:
    return ContainerFacts(
        container_id,
        name,
        hostname,
        volumes,
        compose_project,
        swarm_service_id,
        swarm_node_id,
        mount_sources,
    )


def reader(mountinfo: str, cgroup: str = "0::/\n") -> Callable[[Path], Awaitable[str]]:
    async def read(path: Path) -> str:
        return mountinfo if path == MOUNTINFO_FILE else cgroup

    return read


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


class BrokenProc:
    """A /proc reader that fails until it is repaired."""

    def __init__(self) -> None:
        self.broken = True

    def repair(self) -> None:
        self.broken = False

    async def read(self, path: Path) -> str:
        if self.broken:
            raise OSError("proc unreadable")
        return container_mounts(CONTAINER)


class Harness:
    def __init__(self, probe: ScriptedProbe, sleeper: Sleeper) -> None:
        self.probe = probe
        self.sleeper = sleeper
        self.published: list[SelfIdentity] = []
        self.resolver = SelfIdentityResolver(
            probe=probe,
            publish=self.published.append,
            read=reader(container_mounts(CONTAINER)),
            hostname=lambda: HOSTNAME,
            sleep=sleeper,
        )

    async def settled(self, sleeps: int) -> None:
        """Run the background retry until it has slept this often and the identity is complete."""
        await until(
            lambda: (
                len(self.sleeper.delays) >= sleeps
                and self.resolver.current.containerised
                and not self.resolver.current.incomplete
            )
        )


@pytest.fixture
def probe() -> ScriptedProbe:
    return ScriptedProbe([LOCAL, REMOTE])


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


@pytest.fixture
def harness(probe: ScriptedProbe, sleeper: Sleeper) -> Harness:
    return Harness(probe, sleeper)


def other_tasks() -> list[asyncio.Task[object]]:
    return [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]


class TestResolution:
    async def test_an_environment_holding_the_exact_id_is_a_home_and_the_identity_is_published(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.answers = {1: facts(volumes=frozenset({"data"}), compose_project="Dockmin")}

        identity = await harness.resolver.resolve()

        assert identity.container_id == CONTAINER
        assert identity.source == "mountinfo"
        assert identity.verified == (HomeEnvironment(1, "unix_socket", "dockmin"),)
        assert identity.volumes == {1: frozenset({"data"})}
        assert identity.compose_project == {1: "dockmin"}
        assert harness.published == [identity]
        assert harness.resolver.current is identity

    async def test_the_sources_of_a_homes_mounts_protect_the_paths_that_hold_or_lie_in_them(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.answers = {1: facts(mount_sources=frozenset({"/srv/dockmin/data"}))}

        identity = await harness.resolver.resolve()

        assert identity.mount_sources == {1: frozenset({"/srv/dockmin/data"})}
        assert identity.protects_path(1, "/srv/dockmin/data")
        assert identity.protects_path(1, "/srv/dockmin")
        assert identity.protects_path(1, "/srv/dockmin/data/inner")
        assert not identity.protects_path(1, "/srv/dockmin-other")
        assert not identity.protects_path(2, "/srv/dockmin/data")

    async def test_a_home_with_no_bind_mounts_records_an_empty_list_and_protects_nothing(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.answers = {1: facts()}

        identity = await harness.resolver.resolve()

        assert identity.mount_sources == {1: frozenset()}
        assert not identity.mounts_unknown(1)
        assert not identity.protects_path(1, "/srv/dockmin/data")

    async def test_a_home_that_recorded_no_mount_list_protects_every_path(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.answers = {1: replace(facts(), mount_sources=None)}

        identity = await harness.resolver.resolve()

        assert identity.mounts_unknown(1)
        assert identity.protects_path(1, "/srv/anything")

    async def test_a_reply_for_a_different_id_is_not_a_home(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.answers = {1: facts(OTHER_CONTAINER)}

        identity = await harness.resolver.resolve()

        assert identity.containerised
        assert identity.verified == ()
        assert identity.restartable_via is None

    @pytest.mark.parametrize(
        ("hostname", "is_home"),
        [(HOSTNAME, True), ("another-host", False), (None, True)],
        ids=["matches", "differs", "task-has-none"],
    )
    async def test_the_hostname_must_agree_when_the_environment_reports_one(
        self, harness: Harness, probe: ScriptedProbe, hostname: str | None, is_home: bool
    ) -> None:
        probe.answers = {1: facts(hostname=hostname)}

        identity = await harness.resolver.resolve()

        assert identity.is_home(1) is is_home

    async def test_the_local_socket_comes_first_and_is_where_a_restart_goes(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.refs = [REMOTE, LOCAL]
        probe.answers = {1: facts(), 2: facts()}

        identity = await harness.resolver.resolve()

        assert [home.environment_id for home in identity.verified] == [1, 2]
        assert identity.restartable_via == 1

    async def test_verification_through_a_remote_environment_alone_gives_no_restart_route(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.answers = {2: facts()}

        identity = await harness.resolver.resolve()

        assert identity.is_home(2)
        assert identity.restartable_via is None

    async def test_a_swarm_task_match_records_the_service_and_the_node(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.refs = [SWARM_REMOTE]
        probe.answers = {
            3: facts(hostname=None, swarm_service_id=SERVICE_ID, swarm_node_id=NODE_ID)
        }

        identity = await harness.resolver.resolve()

        assert identity.protects_service(3, SERVICE_ID)
        assert identity.protects_node(3, NODE_ID)

    async def test_no_candidate_in_proc_publishes_an_identity_with_no_container(
        self, probe: ScriptedProbe, sleeper: Sleeper
    ) -> None:
        published: list[SelfIdentity] = []
        resolver = SelfIdentityResolver(
            probe=probe,
            publish=published.append,
            read=reader(mount_line("/", "/")),
            sleep=sleeper,
        )

        identity = await resolver.resolve()

        assert identity == NOT_CONTAINERISED
        assert published == [NOT_CONTAINERISED]


class TestUnreadableEnvironments:
    async def test_an_environment_that_cannot_be_read_is_listed_and_the_identity_is_incomplete(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.answers = {1: facts(), 2: OSError("down")}

        identity = await harness.resolver.resolve()

        assert identity.unreadable == (2,)
        assert identity.incomplete

    async def test_an_environment_that_becomes_unreadable_keeps_the_state_from_before(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.answers = {1: facts(volumes=frozenset({"data"}), compose_project="dockmin")}
        await harness.resolver.resolve()
        probe.answers = {1: OSError("down")}

        identity = await harness.resolver.resolve()

        assert identity.is_home(1)
        assert identity.protects_volume(1, "data")
        assert identity.protects_stack(1, "dockmin")
        assert identity.restartable_via == 1
        assert not identity.incomplete

    async def test_a_failed_listing_keeps_the_earlier_homes_and_marks_the_listing_failed(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.answers = {1: facts(volumes=frozenset({"data"}))}
        await harness.resolver.resolve()
        probe.listing_error = OSError("database locked")

        identity = await harness.resolver.resolve()

        assert identity.protects_volume(1, "data")
        assert identity.listing_failed
        assert identity.incomplete

    async def test_a_failed_listing_with_no_earlier_identity_still_names_the_container(
        self, harness: Harness, probe: ScriptedProbe
    ) -> None:
        probe.listing_error = OSError("database locked")

        identity = await harness.resolver.resolve()

        assert identity.container_id == CONTAINER
        assert identity.verified == ()
        assert identity.listing_failed


class TestBackgroundRetry:
    async def test_an_incomplete_identity_is_retried_with_a_doubling_delay_up_to_the_ceiling(
        self, harness: Harness, probe: ScriptedProbe, sleeper: Sleeper
    ) -> None:
        probe.answers = {1: facts(), 2: OSError("down")}
        sleeper.after = lambda count: probe.answers.update({2: None}) if count == 8 else None

        harness.resolver.request()
        await harness.settled(sleeps=len(BACKOFF_UNTIL_CEILING))

        assert sleeper.delays == BACKOFF_UNTIL_CEILING
        assert not harness.resolver.current.incomplete

    async def test_the_delay_starts_again_at_the_first_step_after_a_clean_resolution(
        self, harness: Harness, probe: ScriptedProbe, sleeper: Sleeper
    ) -> None:
        probe.answers = {2: OSError("down")}
        sleeper.after = lambda count: probe.answers.update({2: None}) if count == 2 else None
        harness.resolver.request()
        await harness.settled(sleeps=2)
        probe.answers = {2: OSError("down")}
        sleeper.after = lambda count: probe.answers.update({2: None}) if count == 4 else None

        harness.resolver.request()
        await harness.settled(sleeps=4)

        assert sleeper.delays == [5.0, 10.0, 5.0, 10.0]

    async def test_a_request_while_a_retry_sleeps_wakes_it_and_resolves_at_once(
        self, probe: ScriptedProbe
    ) -> None:
        sleeper = Sleeper(block=True)
        harness = Harness(probe, sleeper)
        probe.answers = {2: OSError("down")}
        harness.resolver.request()
        await sleeper.entered.wait()
        probe.answers = {2: None}

        harness.resolver.request()
        await harness.settled(sleeps=1)

        assert sleeper.delays == [FIRST_RETRY_SECONDS]
        assert not harness.resolver.current.incomplete

    async def test_watch_waits_before_its_first_attempt_and_only_while_incomplete(
        self, probe: ScriptedProbe
    ) -> None:
        sleeper = Sleeper(block=True)
        harness = Harness(probe, sleeper)
        probe.answers = {2: OSError("down")}
        await harness.resolver.resolve()
        attempts_before = len(harness.published)

        harness.resolver.watch()
        await sleeper.entered.wait()

        assert len(harness.published) == attempts_before
        assert sleeper.delays == [FIRST_RETRY_SECONDS]
        await harness.resolver.aclose()

    async def test_watch_does_nothing_when_the_identity_is_complete(
        self, harness: Harness, probe: ScriptedProbe, sleeper: Sleeper
    ) -> None:
        probe.answers = {1: facts()}
        await harness.resolver.resolve()

        harness.resolver.watch()

        assert other_tasks() == []
        assert sleeper.delays == []

    async def test_closing_cancels_a_sleeping_retry_and_later_requests_start_nothing(
        self, probe: ScriptedProbe
    ) -> None:
        sleeper = Sleeper(block=True)
        harness = Harness(probe, sleeper)
        probe.answers = {2: OSError("down")}
        harness.resolver.request()
        await sleeper.entered.wait()

        await harness.resolver.aclose()
        harness.resolver.request()
        harness.resolver.watch()

        assert other_tasks() == []

    async def test_a_resolution_that_raises_is_retried_rather_than_ending_the_loop(
        self, probe: ScriptedProbe, sleeper: Sleeper
    ) -> None:
        proc = BrokenProc()
        probe.answers = {1: facts()}
        sleeper.after = lambda count: proc.repair()
        resolver = SelfIdentityResolver(
            probe=probe,
            publish=lambda identity: None,
            read=proc.read,
            hostname=lambda: HOSTNAME,
            sleep=sleeper,
        )

        resolver.request()
        await until(lambda: resolver.current.containerised)

        assert sleeper.delays == [FIRST_RETRY_SECONDS]
        assert resolver.current.is_home(1)


HOME = HomeEnvironment(1, "unix_socket", "dockmin")


@pytest.mark.parametrize(
    ("identity", "environment_id", "unknown"),
    [
        (SelfIdentity(), 1, False),
        (SelfIdentity(container_id=CONTAINER, listing_failed=True), 1, True),
        (SelfIdentity(container_id=CONTAINER, unreadable=(1,)), 1, True),
        (SelfIdentity(container_id=CONTAINER, unreadable=(1,)), 2, False),
        (SelfIdentity(container_id=CONTAINER, verified=(HOME,)), 1, True),
        (SelfIdentity(container_id=CONTAINER, verified=(HOME,)), 2, False),
        (
            SelfIdentity(
                container_id=CONTAINER, verified=(HOME,), mount_sources={1: frozenset({"/srv"})}
            ),
            1,
            False,
        ),
    ],
)
def test_the_mounts_are_unknown_only_where_the_identity_cannot_say_what_the_container_mounts(
    identity: SelfIdentity, environment_id: int, unknown: bool
) -> None:
    assert identity.mounts_unknown(environment_id) is unknown
