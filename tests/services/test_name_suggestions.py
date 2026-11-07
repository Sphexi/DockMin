"""Name suggestions: what each kind offers for a typed prefix, and to whom."""

import dataclasses
from collections.abc import Callable, Collection, Mapping, Sequence
from functools import partial
from http import HTTPStatus
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.auth.principal import Principal
from app.auth.services import AuthRuntime, build_services
from app.config import Settings
from app.db.engine import Session
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.db.repositories.stack import StackKind
from app.db.repositories.team import TeamRepository
from app.db.repositories.user import UserRepository
from app.services.access import ResourceKey
from app.services.activity import ActivityRecorder
from app.services.assembly import AccessServices, NetworkServices, VolumeServices
from app.services.name_suggestions import (
    MAX_PREFIX_CHARS,
    MAX_SUGGESTIONS,
    READ_ONLY_LABEL,
    TEAM_LABEL,
    USER_LABEL,
    EnvironmentKind,
    GlobalKind,
    NameOption,
    NameSuggestions,
    SuggestionSources,
)
from app.services.object_inspector import EnvironmentUnavailable
from app.services.stack_views import ManagedStackName
from tests.access_support import ADMIN, ENV_A, ENV_B, STANDARD, container_key, control, volume_key
from tests.auth_support import FakeClock
from tests.db.helpers import PASSWORD_HASH
from tests.docker_engine.container_engine import ContainerEngine, anonymous_volume
from tests.docker_engine.fake_engine import error_reply
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.image_support import image_id
from tests.object_support import add_container
from tests.services.conftest import ContainerRig, ImageRig, SwarmRig
from tests.stack_support import Rig, seed_stack_row

ENVIRONMENT = 1
EVERY_KIND: tuple[EnvironmentKind, ...] = (
    "containers",
    "images",
    "volumes",
    "networks",
    "stacks",
    "services",
    "configs",
    "secrets",
)
PLAIN_KINDS: tuple[EnvironmentKind, ...] = (
    "containers",
    "images",
    "networks",
    "services",
    "configs",
    "secrets",
)
OVER_CAP = MAX_SUGGESTIONS + 5
COMPOSE_ONLY: frozenset[StackKind] = frozenset({"compose"})
SWARM_ONLY: frozenset[StackKind] = frozenset({"swarm"})
BOTH_KINDS: frozenset[StackKind] = frozenset({"compose", "swarm"})
UNUSABLE_INPUT = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param("x" * (MAX_PREFIX_CHARS + 1), id="over the limit"),
]


class Reader:
    """A list service that records each read and answers with fixed rows or a failure."""

    def __init__(self, answer: Sequence[Any] = (), *, failure: Exception | None = None) -> None:
        self.answer = list(answer)
        self.failure = failure
        self.reads: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def read(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.reads.append((args, kwargs))
        if self.failure is not None:
            raise self.failure
        return self.answer

    names = tag_names = managed_names = read

    async def list_page(self, *args: Any, **kwargs: Any) -> Any:
        await self.read(*args, **kwargs)
        return SimpleNamespace(
            items=[SimpleNamespace(username=name, name=name) for name in self.answer]
        )


def _unused() -> NoReturn:
    raise AssertionError("a source the kind does not use was built")


def _same[T](value: T) -> T:
    return value


def sources(**given: object) -> SuggestionSources:
    readers: dict[str, Callable[[], Any]] = {
        field.name: _unused for field in dataclasses.fields(SuggestionSources)
    }
    readers.update({name: partial(_same, reader) for name, reader in given.items()})
    return SuggestionSources(**readers)


def over(kind: EnvironmentKind, reader: Reader) -> NameSuggestions:
    return NameSuggestions(sources(**{kind: reader}))


def numbered(count: int) -> list[str]:
    return [f"n{number:02d}" for number in range(count)]


def values(options: Sequence[NameOption]) -> list[str]:
    return [option.value for option in options]


@pytest.mark.parametrize("kind", EVERY_KIND)
@pytest.mark.parametrize("typed", UNUSABLE_INPUT)
async def test_empty_or_oversized_input_answers_nothing_and_reads_nothing(
    kind: EnvironmentKind, typed: str
) -> None:
    reader = Reader(["alpha"])

    found = await over(kind, reader).in_environment(ADMIN, ENVIRONMENT, kind, typed)

    assert found == () and reader.reads == []


async def test_input_at_the_length_limit_is_still_read() -> None:
    reader = Reader()

    await over("containers", reader).in_environment(
        ADMIN, ENVIRONMENT, "containers", "x" * MAX_PREFIX_CHARS
    )

    assert len(reader.reads) == 1


@pytest.mark.parametrize("kind", PLAIN_KINDS)
async def test_the_typed_text_is_trimmed_and_folded_and_the_read_is_capped(
    kind: EnvironmentKind,
) -> None:
    reader = Reader()

    await over(kind, reader).in_environment(ADMIN, ENVIRONMENT, kind, "  Web-A ")

    assert reader.reads == [((ADMIN, ENVIRONMENT, "web-a"), {"limit": MAX_SUGGESTIONS})]


@pytest.mark.parametrize("kind", PLAIN_KINDS)
async def test_names_are_cut_to_the_prefix_deduplicated_sorted_ignoring_case_and_capped(
    kind: EnvironmentKind,
) -> None:
    reader = Reader(["apple", "Apple", "zeta", "ab", "apple", "AB", *numbered(OVER_CAP)])

    found = await over(kind, reader).in_environment(ADMIN, ENVIRONMENT, kind, "A")

    assert values(found) == ["AB", "ab", "Apple", "apple"]


@pytest.mark.parametrize("kind", PLAIN_KINDS)
async def test_no_more_than_the_cap_is_offered_even_when_the_read_returns_more(
    kind: EnvironmentKind,
) -> None:
    reader = Reader(numbered(OVER_CAP)[::-1])

    found = await over(kind, reader).in_environment(ADMIN, ENVIRONMENT, kind, "n")

    assert values(found) == numbered(MAX_SUGGESTIONS)


@pytest.mark.parametrize("kind", EVERY_KIND)
async def test_each_kind_reads_only_its_own_source_once(kind: EnvironmentKind) -> None:
    readers = {each: Reader() for each in EVERY_KIND}

    await NameSuggestions(sources(**readers)).in_environment(ADMIN, ENVIRONMENT, kind, "a")

    assert {each: len(r.reads) for each, r in readers.items()} == {
        each: int(each == kind) for each in EVERY_KIND
    }


@pytest.mark.parametrize("kind", EVERY_KIND)
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ResourceNotFound("not found"), id="not visible or missing"),
        pytest.param(PermissionDenied("not allowed"), id="role not allowed"),
        pytest.param(EnvironmentUnavailable(), id="host unreachable"),
    ],
)
async def test_a_refused_or_unreadable_environment_answers_the_same_as_no_match(
    kind: EnvironmentKind, failure: Exception
) -> None:
    refused = await over(kind, Reader(failure=failure)).in_environment(
        STANDARD, ENVIRONMENT, kind, "a"
    )
    no_match = await over(kind, Reader()).in_environment(STANDARD, ENVIRONMENT, kind, "a")

    assert refused == no_match == ()


async def test_a_volume_the_caller_cannot_write_is_labelled_read_only() -> None:
    reader = Reader([("data", True), ("Backup", False)])

    read_only = await over("volumes", reader).in_environment(ADMIN, ENVIRONMENT, "volumes", "b")
    writable = await over("volumes", reader).in_environment(ADMIN, ENVIRONMENT, "volumes", "d")

    assert read_only == (NameOption("Backup", READ_ONLY_LABEL),)
    assert writable == (NameOption("data"),)


@pytest.mark.parametrize(
    ("stack_kinds", "asked_kind"),
    [
        pytest.param(COMPOSE_ONLY, "compose", id="compose"),
        pytest.param(SWARM_ONLY, "swarm", id="swarm"),
        pytest.param(BOTH_KINDS, None, id="both"),
    ],
)
async def test_stacks_are_read_for_the_kinds_asked_with_the_prefix_pushed_into_the_query(
    stack_kinds: frozenset[StackKind], asked_kind: StackKind | None
) -> None:
    reader = Reader()

    await over("stacks", reader).in_environment(
        ADMIN, ENVIRONMENT, "stacks", "Sh", stack_kinds=stack_kinds
    )

    assert reader.reads == [
        ((ADMIN, ENVIRONMENT), {"kind": asked_kind, "prefix": "sh", "limit": MAX_PAGE_SIZE})
    ]


async def test_stacks_of_a_kind_that_was_not_asked_for_are_left_out() -> None:
    reader = Reader([ManagedStackName("shop", "compose"), ManagedStackName("shed", "swarm")])

    found = await over("stacks", reader).in_environment(
        ADMIN, ENVIRONMENT, "stacks", "sh", stack_kinds=COMPOSE_ONLY
    )

    assert values(found) == ["shop"]


async def test_no_stack_kind_at_all_answers_nothing_without_reading() -> None:
    reader = Reader([ManagedStackName("shop", "compose")])

    found = await over("stacks", reader).in_environment(
        ADMIN, ENVIRONMENT, "stacks", "sh", stack_kinds=frozenset()
    )

    assert found == () and reader.reads == []


async def test_stacks_that_differ_only_by_case_are_offered_once() -> None:
    reader = Reader(
        [
            ManagedStackName("Shop", "swarm"),
            ManagedStackName("shop", "compose"),
            ManagedStackName("shed", "compose"),
        ]
    )

    found = await over("stacks", reader).in_environment(ADMIN, ENVIRONMENT, "stacks", "sh")

    assert values(found) == ["shed", "Shop"]


@pytest.mark.parametrize("kind", ["users", "grantees"])
async def test_a_standard_user_is_offered_no_user_or_team_and_nothing_is_read(
    kind: GlobalKind,
) -> None:
    users, teams = Reader(["alice"]), Reader(["alpha"])

    found = await NameSuggestions(sources(users=users, teams=teams)).global_names(
        STANDARD, kind, "al"
    )

    assert found == () and users.reads == [] and teams.reads == []


@pytest.mark.parametrize("kind", ["users", "grantees"])
@pytest.mark.parametrize("typed", UNUSABLE_INPUT)
async def test_empty_or_oversized_input_offers_no_user_or_team(
    kind: GlobalKind, typed: str
) -> None:
    users, teams = Reader(["alice"]), Reader(["alpha"])

    found = await NameSuggestions(sources(users=users, teams=teams)).global_names(
        ADMIN, kind, typed
    )

    assert found == () and users.reads == [] and teams.reads == []


async def test_users_are_offered_without_a_label_and_teams_are_not_read() -> None:
    users = Reader(["Alice", "albert", "alfie"])

    found = await NameSuggestions(sources(users=users)).global_names(ADMIN, "users", " AL ")

    assert found == (NameOption("albert"), NameOption("alfie"), NameOption("Alice"))
    assert users.reads == [
        ((ADMIN,), {"limit": MAX_SUGGESTIONS, "cursor": None, "name_prefix": "al"})
    ]


async def test_grantees_are_users_then_teams_each_labelled_with_what_it_is() -> None:
    users, teams = Reader(["alice"]), Reader(["Alpha", "alchemists"])

    found = await NameSuggestions(sources(users=users, teams=teams)).global_names(
        ADMIN, "grantees", "al"
    )

    assert found == (
        NameOption("alice", USER_LABEL),
        NameOption("alchemists", TEAM_LABEL),
        NameOption("Alpha", TEAM_LABEL),
    )
    assert teams.reads == [
        ((ADMIN,), {"limit": MAX_SUGGESTIONS, "cursor": None, "name_prefix": "al"})
    ]


async def test_grantees_are_capped_in_total_with_users_ahead_of_teams() -> None:
    users, teams = Reader(numbered(15)), Reader(numbered(15))

    found = await NameSuggestions(sources(users=users, teams=teams)).global_names(
        ADMIN, "grantees", "n"
    )

    assert [o.label for o in found] == [USER_LABEL] * 15 + [TEAM_LABEL] * 5


@pytest.mark.parametrize("kind", ["users", "grantees"])
async def test_a_refused_read_of_users_or_teams_answers_nothing(kind: GlobalKind) -> None:
    users = Reader(failure=PermissionDenied("not allowed"))

    found = await NameSuggestions(sources(users=users, teams=Reader(["alpha"]))).global_names(
        ADMIN, kind, "al"
    )

    assert found == ()


CONTAINER_LIST = "GET /containers/json"
VOLUME_LIST = "GET /volumes"
NETWORK_LIST = "GET /networks"
IMAGE_LIST = "GET /images/json"
HANDSHAKE = ("GET /_ping", "GET /version")
HOST_LISTS = (CONTAINER_LIST, VOLUME_LIST, NETWORK_LIST)
HOST_KINDS: tuple[EnvironmentKind, ...] = ("containers", "volumes", "networks")
UNKNOWN_ENVIRONMENT = 999
UNMATCHED = "zzz"


def listings(host: ContainerEngine, *wanted: str) -> list[str]:
    return [path for path in host.paths() if path in wanted]


@pytest.fixture
def host_suggestions(
    rig: ContainerRig, vols: VolumeServices, nets: NetworkServices
) -> NameSuggestions:
    return NameSuggestions(
        sources(containers=rig.services.queries, volumes=vols.queries, networks=nets.queries)
    )


def nothing_for(kinds: Sequence[EnvironmentKind]) -> dict[str, list[NameOption]]:
    return {kind: [] for kind in kinds}


def sizes(found: Mapping[str, Collection[NameOption]]) -> dict[str, int]:
    return {kind: len(options) for kind, options in found.items()}


async def ask_each(
    suggestions: NameSuggestions,
    actor: Principal,
    environment_id: int,
    kinds: Sequence[EnvironmentKind],
    typed: str,
) -> dict[str, list[NameOption]]:
    return {
        kind: list(await suggestions.in_environment(actor, environment_id, kind, typed))
        for kind in kinds
    }


async def test_containers_volumes_and_networks_offer_names_by_prefix_with_one_host_read_each(
    host_suggestions: NameSuggestions, host: ContainerEngine
) -> None:
    add_container(host, 1, "Web-1")
    add_container(host, 2, "web-2")
    add_container(host, 3, "worker")
    host.add_volume("web-data")
    host.add_volume("Web-cache")
    host.add_volume("other")
    host.add_volume(anonymous_volume(1), anonymous=True)
    host.add_network("web-net", 0x41)
    host.add_network("Web-back", 0x42)
    host.add_network("db-net", 0x43)

    found = await ask_each(host_suggestions, ADMIN, ENV_A, HOST_KINDS, "WEB")

    assert {kind: values(options) for kind, options in found.items()} == {
        "containers": ["Web-1", "web-2"],
        "volumes": ["Web-cache", "web-data"],
        "networks": ["Web-back", "web-net"],
    }
    assert listings(host, *HOST_LISTS) == list(HOST_LISTS)


async def test_no_kind_offers_a_whole_list_when_many_names_share_the_prefix(
    host_suggestions: NameSuggestions, host: ContainerEngine
) -> None:
    for number in range(OVER_CAP):
        add_container(host, number + 1, f"c{number:02d}")
        host.add_volume(f"v{number:02d}")
        host.add_network(f"n{number:02d}", 0x100 + number)

    found = await ask_each(host_suggestions, ADMIN, ENV_A, HOST_KINDS, "")
    capped: dict[str, tuple[NameOption, ...]] = {
        kind: await host_suggestions.in_environment(ADMIN, ENV_A, kind, kind[0])
        for kind in HOST_KINDS
    }

    assert found == nothing_for(HOST_KINDS)
    assert sizes(capped) == dict.fromkeys(HOST_KINDS, MAX_SUGGESTIONS)
    assert values(capped["containers"]) == [f"c{number:02d}" for number in range(MAX_SUGGESTIONS)]


async def test_a_prefix_matching_only_hidden_objects_answers_like_one_matching_nothing(
    rig: ContainerRig,
    host_suggestions: NameSuggestions,
    host: ContainerEngine,
) -> None:
    add_container(host, 1, "private-app")
    add_container(host, 2, "shared-app")
    host.add_volume("private-data")
    host.add_volume("shared-data")
    host.add_network("private-net", 0x41)
    shared_net = host.add_network("shared-net", 0x42)
    for key in (
        container_key(2),
        volume_key("shared-data"),
        ResourceKey(ENV_A, "network", shared_net),
    ):
        await control(rig.access, key, public=True)

    hidden = await ask_each(host_suggestions, STANDARD, ENV_A, HOST_KINDS, "private")
    nothing = await ask_each(host_suggestions, STANDARD, ENV_A, HOST_KINDS, UNMATCHED)
    shared = await ask_each(host_suggestions, STANDARD, ENV_A, HOST_KINDS, "shared")

    assert hidden == nothing == nothing_for(HOST_KINDS)
    assert {kind: values(options) for kind, options in shared.items()} == {
        "containers": ["shared-app"],
        "volumes": ["shared-data"],
        "networks": ["shared-net"],
    }


async def test_a_volume_is_labelled_read_only_unless_the_caller_may_write_it(
    rig: ContainerRig, host_suggestions: NameSuggestions, host: ContainerEngine
) -> None:
    host.add_volume("shared-ro")
    host.add_volume("shared-rw")
    await control(rig.access, volume_key("shared-ro"), public=True, level="read")
    await control(rig.access, volume_key("shared-rw"), public=True, level="write")

    standard = await host_suggestions.in_environment(STANDARD, ENV_A, "volumes", "shared")
    administrator = await host_suggestions.in_environment(ADMIN, ENV_A, "volumes", "shared")

    assert standard == (NameOption("shared-ro", READ_ONLY_LABEL), NameOption("shared-rw"))
    assert administrator == (NameOption("shared-ro"), NameOption("shared-rw"))


async def test_a_standard_user_on_a_local_socket_is_offered_volumes_as_read_only(
    rig: ContainerRig, host_suggestions: NameSuggestions, host: ContainerEngine
) -> None:
    host.add_volume("local-vol")
    await control(rig.access, volume_key("local-vol", ENV_B), public=True, level="write")

    found = await host_suggestions.in_environment(STANDARD, ENV_B, "volumes", "local")

    assert found == (NameOption("local-vol", READ_ONLY_LABEL),)


@pytest.mark.usefixtures("no_grants")
async def test_an_environment_that_is_hidden_missing_or_unreachable_answers_like_no_match(
    host_suggestions: NameSuggestions, host: ContainerEngine
) -> None:
    add_container(host, 1, "web-1")
    host.add_volume("web-data")
    host.add_network("web-net", 0x41)
    expected = nothing_for(HOST_KINDS)

    not_visible = await ask_each(host_suggestions, STANDARD, ENV_A, HOST_KINDS, "web")
    missing = await ask_each(host_suggestions, ADMIN, UNKNOWN_ENVIRONMENT, HOST_KINDS, "web")
    reads_while_hidden = len(listings(host, *HOST_LISTS))
    host.listing_status = HTTPStatus.INTERNAL_SERVER_ERROR
    host.route("GET", "/volumes", error_reply(HTTPStatus.BAD_GATEWAY))
    host.route("GET", "/networks", error_reply(HTTPStatus.BAD_GATEWAY))
    unreachable = await ask_each(host_suggestions, ADMIN, ENV_A, HOST_KINDS, "web")

    assert not_visible == missing == unreachable == expected
    assert reads_while_hidden == 0


SWARM_KINDS: tuple[EnvironmentKind, ...] = ("services", "configs", "secrets")
OBJECT_TYPES = {"services": "service", "configs": "config", "secrets": "secret"}
IMAGE_ONE, IMAGE_TWO, IMAGE_THREE = image_id(1), image_id(2), image_id(3)


@pytest.fixture
def swarm_suggestions(swarm: SwarmRig) -> NameSuggestions:
    queries = swarm.sw
    return NameSuggestions(
        sources(services=queries.services, configs=queries.configs, secrets=queries.secrets)
    )


def add_swarm_objects(host: SwarmEngine, prefix: str) -> dict[str, str]:
    """Add a service, a config and a secret named `prefix-<kind>`; return their ids by kind."""
    return {
        "services": host.named_service(f"{prefix}-app"),
        "configs": host.add_config(f"{prefix}-conf"),
        "secrets": host.add_secret(f"{prefix}-key"),
    }


async def test_services_configs_and_secrets_offer_names_by_prefix_ignoring_case(
    swarm_suggestions: NameSuggestions, swarm_host: SwarmEngine
) -> None:
    swarm_host.named_service("Web-b")
    swarm_host.named_service("web-a")
    swarm_host.named_service("db")
    swarm_host.add_config("WEB-conf")
    swarm_host.add_config("other")
    swarm_host.add_secret("web-key")
    swarm_host.add_secret("Web-cert")

    found = await ask_each(swarm_suggestions, ADMIN, ENV_A, SWARM_KINDS, "wEB")

    assert {kind: values(options) for kind, options in found.items()} == {
        "services": ["web-a", "Web-b"],
        "configs": ["WEB-conf"],
        "secrets": ["Web-cert", "web-key"],
    }
    assert swarm_host.paths().count("GET /services") == 1
    assert swarm_host.paths().count("GET /configs") == 1
    assert swarm_host.paths().count("GET /secrets") == 1


async def test_swarm_kinds_are_capped_at_the_limit_and_never_offer_a_whole_list(
    swarm_suggestions: NameSuggestions, swarm_host: SwarmEngine
) -> None:
    for number in range(OVER_CAP):
        swarm_host.named_service(f"s{number:02d}")
        swarm_host.add_config(f"c{number:02d}")
        swarm_host.add_secret(f"x{number:02d}")

    blank = await ask_each(swarm_suggestions, ADMIN, ENV_A, SWARM_KINDS, "")
    capped = {
        "services": await swarm_suggestions.in_environment(ADMIN, ENV_A, "services", "s"),
        "configs": await swarm_suggestions.in_environment(ADMIN, ENV_A, "configs", "c"),
        "secrets": await swarm_suggestions.in_environment(ADMIN, ENV_A, "secrets", "x"),
    }

    assert blank == nothing_for(SWARM_KINDS)
    assert sizes(capped) == dict.fromkeys(SWARM_KINDS, MAX_SUGGESTIONS)


async def test_swarm_kinds_answer_a_prefix_matching_only_hidden_objects_like_no_match(
    swarm: SwarmRig, swarm_suggestions: NameSuggestions, swarm_host: SwarmEngine
) -> None:
    add_swarm_objects(swarm_host, "private")
    shared = add_swarm_objects(swarm_host, "shared")
    for kind, object_id in shared.items():
        await control(swarm.access, ResourceKey(ENV_A, OBJECT_TYPES[kind], object_id), public=True)  # type: ignore[arg-type]

    hidden = await ask_each(swarm_suggestions, STANDARD, ENV_A, SWARM_KINDS, "private")
    nothing = await ask_each(swarm_suggestions, STANDARD, ENV_A, SWARM_KINDS, UNMATCHED)
    visible = await ask_each(swarm_suggestions, STANDARD, ENV_A, SWARM_KINDS, "shared")

    assert hidden == nothing == nothing_for(SWARM_KINDS)
    assert {kind: values(options) for kind, options in visible.items()} == {
        "services": ["shared-app"],
        "configs": ["shared-conf"],
        "secrets": ["shared-key"],
    }


async def test_a_swarm_environment_that_is_unusable_answers_like_no_match(
    swarm: SwarmRig, swarm_suggestions: NameSuggestions, swarm_host: SwarmEngine
) -> None:
    add_swarm_objects(swarm_host, "web")
    await swarm.db.execute("UPDATE environment SET type = 'standalone' WHERE id = ?", (ENV_B,))
    expected = nothing_for(SWARM_KINDS)

    not_a_manager = await ask_each(swarm_suggestions, ADMIN, ENV_B, SWARM_KINDS, "web")
    missing = await ask_each(swarm_suggestions, ADMIN, UNKNOWN_ENVIRONMENT, SWARM_KINDS, "web")
    swarm_host.outage = {"services", "configs", "secrets"}
    unreachable = await ask_each(swarm_suggestions, ADMIN, ENV_A, SWARM_KINDS, "web")

    assert not_a_manager == missing == unreachable == expected


@pytest.fixture
def image_suggestions(image_rig: ImageRig) -> NameSuggestions:
    return NameSuggestions(sources(images=image_rig.images.queries))


async def test_images_offer_tag_references_not_ids_by_prefix_with_one_image_list_read(
    image_rig: ImageRig, image_suggestions: NameSuggestions
) -> None:
    host = image_rig.host
    host.add_image("nginx:1", IMAGE_ONE)
    host.add_image("nginx:Stable", IMAGE_ONE)
    host.add_image("ghcr.io/o/nginx-app:2", IMAGE_TWO)
    host.add_image("redis:7", IMAGE_THREE)
    host.add_image(None, image_id(4))

    nginx = await image_suggestions.in_environment(ADMIN, ENV_A, "images", "NG")
    registry = await image_suggestions.in_environment(STANDARD, ENV_A, "images", "ghcr")
    dangling = await image_suggestions.in_environment(ADMIN, ENV_A, "images", "<")
    by_id = await image_suggestions.in_environment(ADMIN, ENV_A, "images", IMAGE_ONE[:4])

    assert values(nginx) == ["nginx:1", "nginx:Stable"]
    assert values(registry) == ["ghcr.io/o/nginx-app:2"]
    assert dangling == by_id == ()


async def test_one_image_suggestion_makes_only_the_image_list_call(
    image_rig: ImageRig, image_suggestions: NameSuggestions
) -> None:
    image_rig.host.add_image("nginx:1", IMAGE_ONE)
    image_rig.host.add_image("redis:7", IMAGE_TWO)

    await image_suggestions.in_environment(ADMIN, ENV_A, "images", "n")

    assert [p for p in image_rig.host.paths() if p not in HANDSHAKE] == [IMAGE_LIST]


async def test_image_suggestions_for_a_missing_environment_or_unreadable_host_are_empty(
    image_rig: ImageRig, image_suggestions: NameSuggestions
) -> None:
    image_rig.host.add_image("nginx:1", IMAGE_ONE)

    missing = await image_suggestions.in_environment(ADMIN, UNKNOWN_ENVIRONMENT, "images", "n")
    image_rig.host.fail("image_list", HTTPStatus.INTERNAL_SERVER_ERROR)
    unreachable = await image_suggestions.in_environment(ADMIN, ENV_A, "images", "n")

    assert missing == unreachable == ()


COMPOSE_FILE = "services:\n  web:\n    image: nginx:1\n"


async def seed_stacks(rig: Rig, *names: str, owner: int | None = None) -> None:
    for name in names:
        await seed_stack_row(rig, ENV_A, name, COMPOSE_FILE, owner_user_id=owner)


async def test_stacks_offer_managed_names_by_prefix_from_the_database_without_a_host_read(
    stack_rig: Rig,
) -> None:
    await seed_stacks(stack_rig, "shop", "shelf", "shed", "blog")
    suggestions = NameSuggestions(sources(stacks=stack_rig.fresh().queries))
    requests_before = len(stack_rig.host.requests)

    found = await suggestions.in_environment(ADMIN, ENV_A, "stacks", "SH")

    assert values(found) == ["shed", "shelf", "shop"]
    assert len(stack_rig.host.requests) == requests_before


async def test_stacks_answer_a_prefix_matching_only_hidden_stacks_like_no_match(
    stack_rig: Rig,
) -> None:
    await seed_stacks(stack_rig, "shed")
    await seed_stacks(stack_rig, "shop", owner=STANDARD.user_id)
    suggestions = NameSuggestions(sources(stacks=stack_rig.fresh().queries))

    hidden = await suggestions.in_environment(STANDARD, ENV_A, "stacks", "she")
    nothing = await suggestions.in_environment(STANDARD, ENV_A, "stacks", UNMATCHED)
    visible = await suggestions.in_environment(STANDARD, ENV_A, "stacks", "sh")

    assert hidden == nothing == ()
    assert values(visible) == ["shop"]


async def test_stacks_treat_an_underscore_in_the_prefix_as_a_literal_character(
    stack_rig: Rig,
) -> None:
    await seed_stacks(stack_rig, "a_b", "axb", "a-b")
    suggestions = NameSuggestions(sources(stacks=stack_rig.fresh().queries))

    found = await suggestions.in_environment(ADMIN, ENV_A, "stacks", "a_")

    assert values(found) == ["a_b"]


BULK = 510
TARGET = "zz-target"


@pytest.fixture
def directory_suggestions(
    db: Session, runtime: AuthRuntime, access: AccessServices, settings: Settings
) -> NameSuggestions:
    auth = build_services(db, settings, runtime, ActivityRecorder(db, runtime.clock))
    return NameSuggestions(sources(users=auth.user_admin, teams=access.teams))


async def test_users_and_teams_are_found_by_prefix_past_the_first_five_hundred(
    db: Session, clock: FakeClock, directory_suggestions: NameSuggestions
) -> None:
    users, teams = UserRepository(db, clock), TeamRepository(db, clock)
    for number in range(BULK):
        await users.create(f"person-{number:03d}", PASSWORD_HASH, "standard")
        await teams.create(f"group-{number:03d}")
    await users.create(TARGET, PASSWORD_HASH, "standard")
    await teams.create(TARGET)

    people = await directory_suggestions.global_names(ADMIN, "users", "zz")
    grantees = await directory_suggestions.global_names(ADMIN, "grantees", "ZZ")

    assert people == (NameOption(TARGET),)
    assert grantees == (NameOption(TARGET, USER_LABEL), NameOption(TARGET, TEAM_LABEL))


async def test_users_and_teams_treat_wildcard_characters_in_the_prefix_as_literal(
    db: Session, clock: FakeClock, directory_suggestions: NameSuggestions
) -> None:
    users, teams = UserRepository(db, clock), TeamRepository(db, clock)
    for name in ("a_b", "axb", "a%b"):
        await users.create(name, PASSWORD_HASH, "standard")
        await teams.create(name)
    await teams.create("a\\b")

    underscore = await directory_suggestions.global_names(ADMIN, "grantees", "a_")
    percent = await directory_suggestions.global_names(ADMIN, "grantees", "a%")
    backslash = await directory_suggestions.global_names(ADMIN, "grantees", "a\\")

    assert underscore == (NameOption("a_b", USER_LABEL), NameOption("a_b", TEAM_LABEL))
    assert percent == (NameOption("a%b", USER_LABEL), NameOption("a%b", TEAM_LABEL))
    assert backslash == (NameOption("a\\b", TEAM_LABEL),)


async def test_a_standard_user_is_offered_no_real_user_or_team(
    directory_suggestions: NameSuggestions,
) -> None:
    users = await directory_suggestions.global_names(STANDARD, "users", "a")
    grantees = await directory_suggestions.global_names(STANDARD, "grantees", "a")

    assert users == grantees == ()
