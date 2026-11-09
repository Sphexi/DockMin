"""The origin reference: strict parsing, and resolution that only ever yields a read object."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from app.auth.errors import PermissionDenied, ResourceNotFound
from app.auth.principal import Principal
from app.docker_engine.errors import EnvironmentUnreachable
from app.services.object_inspector import EnvironmentUnavailable
from app.services.origins import (
    REF_MAX,
    Origin,
    OriginRef,
    OriginResolver,
    OriginTarget,
    Reader,
    list_ref,
    list_target,
    parse_origin,
)

VIEWER = cast(Principal, SimpleNamespace())
ADMINISTRATOR = cast(Principal, SimpleNamespace(is_administrator=True))
STANDARD = cast(Principal, SimpleNamespace(is_administrator=False))
ENVIRONMENT = 3
CONTAINER = "a" * 64
SERVICE = "b" * 25
IMAGE_HEX = "c" * 64
BUDGET = 0.05
STACK_REF = f"stack:{ENVIRONMENT}:blog"
CONTAINER_REF = f"container:{ENVIRONMENT}:{CONTAINER}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (STACK_REF, OriginRef("stack", ENVIRONMENT, "blog")),
        ("stack:3:Blog.v2", OriginRef("stack", ENVIRONMENT, "Blog.v2")),
        (CONTAINER_REF, OriginRef("container", ENVIRONMENT, CONTAINER)),
        (f"service:3:{SERVICE}", OriginRef("service", ENVIRONMENT, SERVICE)),
        (f"network:3:{SERVICE}", OriginRef("network", ENVIRONMENT, SERVICE)),
        (f"network:3:{CONTAINER}", OriginRef("network", ENVIRONMENT, CONTAINER)),
        ("volume:3:data_1.bak", OriginRef("volume", ENVIRONMENT, "data_1.bak")),
        (f"image:3:sha256:{IMAGE_HEX}", OriginRef("image", ENVIRONMENT, IMAGE_HEX)),
        ("automation:-:17", OriginRef("automation", None, "17")),
        ("activity:-:9", OriginRef("activity", None, "9")),
        (f"config:3:{SERVICE}", OriginRef("config", ENVIRONMENT, SERVICE)),
        (f"secret:3:{SERVICE}", OriginRef("secret", ENVIRONMENT, SERVICE)),
        ("environment:-:4", OriginRef("environment", None, "4")),
        ("team:-:2", OriginRef("team", None, "2")),
        ("list:3:containers", OriginRef("list", ENVIRONMENT, "containers")),
        ("list:-:activity", OriginRef("list", None, "activity")),
    ],
)
def test_a_well_formed_reference_parses_to_its_kind_environment_and_key(
    text: str, expected: OriginRef
) -> None:
    assert parse_origin(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "stack",
        "stack:3",
        "stack:3:",
        ":3:blog",
        "nothing:3:blog",
        "STACK:3:blog",
        "stack:0:blog",
        "stack:03:blog",
        "stack:-:blog",
        "stack:٣:blog",
        "stack:99999999999999999999:blog",
        "stack:3:blog:extra",
        "stack:3:-blog",
        "stack:3:a/b",
        "stack:3:a\\b",
        "stack:3:..",
        "stack:3:%2e%2e",
        "stack:3:a%2Fb",
        "stack:3:blog\n",
        "stack:3:blog ",
        "stack:3:bl\x00og",
        "stack:3:blög",
        "stack:3:" + "a" * 64,
        "//evil.test/x",
        "https://evil.test/x",
        "javascript:alert(1)",
        "/environments/3/stacks/blog",
        f"container:3:{CONTAINER.upper()}",
        f"container:3:{CONTAINER[:-1]}",
        f"container:3:{CONTAINER}0",
        f"service:3:{SERVICE.upper()}",
        f"image:3:{IMAGE_HEX}",
        f"image:3:sha256:{IMAGE_HEX[:-1]}",
        "volume:3:../etc",
        "volume:3:" + "a" * 256,
        "automation:3:17",
        "automation:-:0",
        "automation:-:017",
        "automation:-:1x",
        "activity:3:9",
        "activity:-:9223372036854775808",
        "stack:3:" + "a" * REF_MAX,
        "environment:3:4",
        "team:-:0",
        f"config:3:{CONTAINER}",
        "list:-:containers",
        "list:3:activity",
        "list:3:stacks",
        "list:3:Containers",
        "list:3:",
    ],
    ids=repr,
)
def test_anything_else_is_not_a_reference(text: str) -> None:
    assert parse_origin(text) is None


def test_the_written_form_of_a_reference_reproduces_the_parsed_one() -> None:
    image = parse_origin(f"image:3:sha256:{IMAGE_HEX}")
    automation = parse_origin("automation:-:17")

    assert image is not None and automation is not None
    assert (image.text, automation.text) == (f"image:3:sha256:{IMAGE_HEX}", "automation:-:17")


class Fixture:
    """A resolver over scripted readers that records which ones ran."""

    def __init__(self, reader: Reader, status: str = "reachable") -> None:
        self.calls: list[tuple[str, int | None, str, bool]] = []
        self.status = status

        async def recording(
            principal: Principal, environment_id: int | None, key: str, reachable: bool
        ) -> OriginTarget:
            self.calls.append(("read", environment_id, key, reachable))
            return await reader(principal, environment_id, key, reachable)

        async def status_of(environment_id: int) -> str:
            if self.status == "missing":
                raise ResourceNotFound("not found")
            return self.status

        def href_for(kind: str, environment_id: int | None, key: str, variant: str) -> str:
            return f"/{kind}/{environment_id}/{variant}/{key}"

        readers = dict.fromkeys(("stack", "container", "automation", "activity"), recording)
        self.resolver = OriginResolver(readers, status_of, href_for, budget=BUDGET)


def named(name: str, key: str | None = None, variant: str = "") -> Reader:
    async def read(
        principal: Principal, environment_id: int | None, given: str, reachable: bool
    ) -> OriginTarget:
        return OriginTarget(name, given if key is None else key, variant)

    return read


def raising(error: BaseException) -> Reader:
    async def read(
        principal: Principal, environment_id: int | None, key: str, reachable: bool
    ) -> OriginTarget:
        raise error

    return read


async def stalled(
    principal: Principal, environment_id: int | None, key: str, reachable: bool
) -> OriginTarget:
    await asyncio.sleep(60)
    raise AssertionError("the budget did not cut the read off")


async def test_a_readable_origin_resolves_with_a_label_and_a_link_built_from_the_object() -> None:
    fixture = Fixture(named("blog", variant="compose"))

    origin = await fixture.resolver.resolve(VIEWER, STACK_REF)

    assert origin == Origin(
        label="stack blog", href=f"/stack/{ENVIRONMENT}/compose/blog", ref=STACK_REF
    )


async def test_the_link_and_reference_come_from_the_key_the_reader_returns_not_the_input() -> None:
    fixture = Fixture(named("Blog", key="Blog", variant="swarm"))

    origin = await fixture.resolver.resolve(VIEWER, "stack:3:blog")

    assert origin == Origin(
        label="stack Blog", href=f"/stack/{ENVIRONMENT}/swarm/Blog", ref="stack:3:Blog"
    )


async def test_an_activity_origin_is_labelled_without_any_name_and_belongs_to_no_environment() -> (
    None
):
    fixture = Fixture(named(""))

    origin = await fixture.resolver.resolve(VIEWER, "activity:-:9")

    assert origin == Origin(label="activity event", href="/activity/None//9", ref="activity:-:9")


@pytest.mark.parametrize(
    "error",
    [
        ResourceNotFound("not found"),
        PermissionDenied("no"),
        EnvironmentUnavailable(),
        EnvironmentUnreachable(ENVIRONMENT, "refused"),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_a_reader_that_cannot_read_the_object_leaves_no_origin(error: BaseException) -> None:
    fixture = Fixture(raising(error))

    assert await fixture.resolver.resolve(VIEWER, STACK_REF) is None


async def test_a_read_that_outlasts_the_budget_leaves_no_origin() -> None:
    fixture = Fixture(stalled)

    assert await fixture.resolver.resolve(VIEWER, STACK_REF) is None


async def test_a_host_kind_is_not_read_while_its_environment_is_not_stored_as_reachable() -> None:
    fixture = Fixture(named("web"), status="unreachable")

    unreachable = await fixture.resolver.resolve(VIEWER, CONTAINER_REF)
    stack = await fixture.resolver.resolve(VIEWER, STACK_REF)

    assert unreachable is None
    assert stack is not None
    assert fixture.calls == [("read", ENVIRONMENT, "blog", False)]


async def test_an_environment_that_does_not_exist_leaves_no_origin() -> None:
    fixture = Fixture(named("web"), status="missing")

    assert await fixture.resolver.resolve(VIEWER, CONTAINER_REF) is None
    assert fixture.calls == []


@pytest.mark.parametrize("text", ["", "stack:3:a/b", "//evil.test", "image:3:sha256:short"])
async def test_a_malformed_reference_reaches_no_reader(text: str) -> None:
    fixture = Fixture(named("blog"))

    assert await fixture.resolver.resolve(VIEWER, text) is None
    assert fixture.calls == []


async def test_a_kind_with_no_reader_leaves_no_origin() -> None:
    fixture = Fixture(named("web"))

    assert await fixture.resolver.resolve(VIEWER, f"service:3:{SERVICE}") is None


def test_a_list_reference_is_written_with_its_environment_only_when_the_list_has_one() -> None:
    assert (list_ref("containers", ENVIRONMENT), list_ref("automation")) == (
        f"list:{ENVIRONMENT}:containers",
        "list:-:automation",
    )


@pytest.mark.parametrize(
    ("name", "environment_id", "error"),
    [
        ("containers", None, ValueError),
        ("automation", ENVIRONMENT, ValueError),
        ("x", None, KeyError),
    ],
)
def test_a_list_reference_that_cannot_resolve_is_refused_when_written(
    name: str, environment_id: int | None, error: type[Exception]
) -> None:
    with pytest.raises(error):
        list_ref(name, environment_id)


async def test_a_list_origin_is_labelled_with_the_list_and_linked_by_its_route() -> None:
    async def status_of(environment_id: int) -> str:
        return "reachable"

    def href_for(kind: str, environment_id: int | None, key: str, variant: str) -> str:
        return f"/{kind}/{environment_id}/{key}"

    resolver = OriginResolver({"list": named("containers")}, status_of, href_for, budget=BUDGET)

    origin = await resolver.resolve(VIEWER, "list:3:containers")

    assert origin == Origin(label="containers", href="/list/3/containers", ref="list:3:containers")


class ListGate:
    """Records the environment checks a list origin makes and answers them as scripted."""

    def __init__(
        self, *, visible: bool = True, environment_type: str | None = "standalone"
    ) -> None:
        self.checked: list[int] = []
        self.visible = visible
        self.environment_type = environment_type

    async def require_environment(self, principal: Principal, environment_id: int) -> None:
        self.checked.append(environment_id)
        if not self.visible:
            raise ResourceNotFound("not found")

    async def type_of(self, environment_id: int) -> str | None:
        return self.environment_type

    async def target(
        self, principal: Principal, environment_id: int | None, name: str
    ) -> OriginTarget:
        return await list_target(
            principal,
            environment_id,
            name,
            require_environment=self.require_environment,
            environment_type=self.type_of,
        )


async def test_an_environment_list_origin_is_read_only_through_the_environment_check() -> None:
    gate = ListGate()

    target = await gate.target(STANDARD, ENVIRONMENT, "containers")

    assert target == OriginTarget("containers", "containers")
    assert gate.checked == [ENVIRONMENT]


@pytest.mark.parametrize(
    ("principal", "environment_id", "name", "gate"),
    [
        pytest.param(STANDARD, None, "activity", ListGate(), id="administrators-only-list"),
        pytest.param(
            STANDARD, ENVIRONMENT, "containers", ListGate(visible=False), id="hidden-environment"
        ),
        pytest.param(
            ADMINISTRATOR, ENVIRONMENT, "services", ListGate(), id="swarm-list-on-standalone"
        ),
    ],
)
async def test_a_list_origin_the_viewer_cannot_open_is_not_found(
    principal: Principal, environment_id: int | None, name: str, gate: ListGate
) -> None:
    with pytest.raises(ResourceNotFound):
        await gate.target(principal, environment_id, name)
