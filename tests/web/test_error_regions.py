"""Try again on a failed fragment reloads the region that asked for it, and only that one."""

import ast
from pathlib import Path
from typing import NamedTuple, NoReturn

import pytest
from httpx import AsyncClient

from app.services.object_inspector import EnvironmentUnavailable
from app.services.stack_queries import StackQueryService
from app.web.object_pages import Region
from tests.access_support import container_id
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.fake_engine import Refuse
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.web.environment_support import World, seed_stack_rows
from tests.web.helpers import parse_tags

UNAVAILABLE = 503
SERVICE_ID = "a" * 25
SWARM_OBJECT_ID = "b" * 25
STACK = "blog"
IMAGE_ID = "c" * 64
CONTAINER_ID = container_id(0xA1)
NETWORK_ID = container_id(0xB2)
RETRY_LABEL = "Try again"
PROBES = ("/_ping", "/version")
WEB_ROOT = Path(__file__).resolve().parents[2] / "app" / "web"
GUARDED_CALLS = frozenset({"problem_page", "unavailable_page"})


class Fragment(NamedTuple):
    """A route an htmx element requests, the element's id, and how its swap is made."""

    name: str
    path: str
    region: str
    swap: str


STANDALONE = (
    Fragment(
        "container tabs", f"/containers/{CONTAINER_ID}?tab=labels", "container-tabs", "outerHTML"
    ),
    Fragment("network tabs", f"/networks/{NETWORK_ID}", "network-tabs", "outerHTML"),
    Fragment("volume tabs", "/volumes/data", "volume-tabs", "outerHTML"),
    Fragment("image tabs", f"/images/{IMAGE_ID}", "image-tabs", "outerHTML"),
    Fragment("compose stack tabs", f"/stacks/{STACK}", "stack-tabs", "outerHTML"),
)
SWARM = (
    Fragment("swarm service tabs", f"/swarm/services/{SERVICE_ID}", "service-tabs", "outerHTML"),
    Fragment("swarm config tabs", f"/swarm/configs/{SWARM_OBJECT_ID}", "config-tabs", "outerHTML"),
    Fragment("swarm secret tabs", f"/swarm/secrets/{SWARM_OBJECT_ID}", "secret-tabs", "outerHTML"),
    Fragment("swarm stack tabs", "/swarm/stacks/shop", "stack-tabs", "outerHTML"),
)


class Embedded(NamedTuple):
    """An error a template renders inside an element of the page, which "Try again" re-requests."""

    name: str
    path: str
    target: str
    swap: str
    status: int = UNAVAILABLE


LISTS = (
    Embedded("containers list", "/containers", "containers-results", "innerHTML"),
    Embedded("images list", "/images", "images-results", "innerHTML"),
    Embedded("networks list", "/networks", "networks-results", "innerHTML"),
    Embedded("volumes list", "/volumes", "volumes-results", "innerHTML"),
)
COMPOSE_LISTS = (Embedded("compose stacks list", "/stacks", "stacks-results", "innerHTML"),)
STACK_LOGS = (
    Embedded("compose stack logs", f"/stacks/{STACK}?tab=logs", "stack-tabs", "outerHTML"),
)
SWARM_LISTS = (
    Embedded("swarm services list", "/swarm/services", "services-results", "innerHTML"),
    Embedded("swarm configs list", "/swarm/configs", "configs-results", "innerHTML"),
    Embedded("swarm secrets list", "/swarm/secrets", "secrets-results", "innerHTML"),
    Embedded(
        "swarm service tasks", f"/swarm/services/{SERVICE_ID}/tasks", "service-tasks", "outerHTML"
    ),
)
OVERVIEW = Embedded("environment overview", "", "environment-overview", "innerHTML", 200)


class Observed(NamedTuple):
    """What a failed fragment offers: its status, its own id, and where Try again swaps."""

    status: int
    root_id: str | None
    retry_target: str | None
    retry_swap: str | None
    retry_get_matches_href: bool


def observe(html: str, status: int) -> Observed:
    tags = parse_tags(html)
    root = next(tag for tag in tags if "state--error" in tag.attrs.get("class", "").split())
    retry = next(tag for tag in tags if tag.name == "a" and tag.attrs.get("class") == "btn")
    return Observed(
        status,
        root.attrs.get("id"),
        retry.attrs.get("hx-target"),
        retry.attrs.get("hx-swap"),
        retry.attrs.get("hx-get", retry.attrs["href"]) == retry.attrs["href"],
    )


async def fetch_all(
    client: AsyncClient, base: str, cases: tuple[Fragment, ...]
) -> dict[str, Observed]:
    """Request each fragment as the element that owns it would, and report what came back."""
    observed: dict[str, Observed] = {}
    for case in cases:
        response = await client.get(
            base + case.path, headers={"HX-Request": "true", "HX-Target": case.region}
        )
        observed[case.name] = observe(response.text, response.status_code)
    return observed


def expected(cases: tuple[Fragment, ...]) -> dict[str, Observed]:
    return {
        case.name: Observed(UNAVAILABLE, case.region, f"#{case.region}", case.swap, True)
        for case in cases
    }


async def host_gone(*_: object, **__: object) -> NoReturn:
    """Stand in for a stack read that finds the host unusable, which a stored stack never does."""
    raise EnvironmentUnavailable


def embedded_expectation(cases: tuple[Embedded, ...]) -> dict[str, Observed]:
    """The error sits inside its region, so its own root never carries the region's id."""
    return {
        case.name: Observed(case.status, None, f"#{case.target}", case.swap, True) for case in cases
    }


async def fetch_embedded(
    client: AsyncClient, base: str, cases: tuple[Embedded, ...]
) -> dict[str, Observed]:
    observed: dict[str, Observed] = {}
    for case in cases:
        response = await client.get(
            base + case.path, headers={"HX-Request": "true", "HX-Target": case.target}
        )
        observed[case.name] = observe(response.text, response.status_code)
    return observed


def bring_up(host: ContainerEngine) -> None:
    del host.peek, host.take


def take_down(host: ContainerEngine) -> None:
    """Refuse every Engine call except the probes that succeeded when the host was added."""
    peek, take = host.peek, host.take
    host.peek = lambda method, path: peek(method, path) if path in PROBES else Refuse()  # type: ignore[method-assign]
    host.take = lambda method, path: take(method, path) if path in PROBES else Refuse()  # type: ignore[method-assign]


async def test_each_standalone_fragment_answers_a_down_host_with_try_again_for_its_own_region(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    take_down(container_host)
    monkeypatch.setattr(StackQueryService, "detail", host_gone)

    observed = await fetch_all(admin_client, f"/environments/{world.remote_id}", STANDALONE)

    assert observed == expected(STANDALONE)


async def test_each_swarm_fragment_answers_a_down_host_with_try_again_for_its_own_region(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    take_down(swarm_host)

    observed = await fetch_all(admin_client, f"/environments/{world.remote_id}", SWARM)

    assert observed == expected(SWARM)


async def test_a_fragment_aimed_at_another_element_or_a_page_load_gets_the_whole_page_link(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    take_down(container_host)
    path = f"/environments/{world.remote_id}{STANDALONE[0].path}"

    elsewhere = await admin_client.get(path, headers={"HX-Request": "true", "HX-Target": "other"})
    untargeted = await admin_client.get(path, headers={"HX-Request": "true"})
    boosted = await admin_client.get(
        path, headers={"HX-Request": "true", "HX-Boosted": "true", "HX-Target": "container-tabs"}
    )
    page = await admin_client.get(path)

    for response in (elsewhere, untargeted):
        assert observe(response.text, response.status_code) == Observed(
            UNAVAILABLE, None, None, None, True
        )
    assert "<html" in boosted.text and "<html" in page.text
    assert [RETRY_LABEL in r.text for r in (boosted, page)] == [True, True]
    assert not any('hx-target="#container-tabs"' in r.text for r in (boosted, page))


async def test_each_standalone_list_logs_tab_and_overview_retries_only_its_own_element(
    admin_client: AsyncClient,
    world: World,
    container_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with world.app.state.database.session() as db:
        await seed_stack_rows(db, world.remote_id, (STACK, "compose"))
    take_down(container_host)
    monkeypatch.setattr(StackQueryService, "page", host_gone)
    base = f"/environments/{world.remote_id}"

    observed = await fetch_embedded(
        admin_client, base, (*LISTS, *COMPOSE_LISTS, *STACK_LOGS, OVERVIEW)
    )

    assert observed == embedded_expectation((*LISTS, *COMPOSE_LISTS, *STACK_LOGS, OVERVIEW))


async def test_each_swarm_list_and_the_task_panel_retries_only_its_own_element(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    take_down(swarm_host)

    observed = await fetch_embedded(admin_client, f"/environments/{world.remote_id}", SWARM_LISTS)

    assert observed == embedded_expectation(SWARM_LISTS)


async def test_try_again_on_a_list_and_on_the_overview_recovers_once_the_host_is_back(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    base = f"/environments/{world.remote_id}"
    take_down(container_host)
    failed = {
        case.name: await admin_client.get(
            base + case.path, headers={"HX-Request": "true", "HX-Target": case.target}
        )
        for case in (LISTS[0], OVERVIEW)
    }
    retries = {
        name: next(
            tag
            for tag in parse_tags(response.text)
            if tag.name == "a" and tag.attrs["class"] == "btn"
        )
        for name, response in failed.items()
    }
    bring_up(container_host)

    answers = {
        name: await admin_client.get(
            retry.attrs["hx-get"],
            headers={"HX-Request": "true", "HX-Target": retry.attrs["hx-target"].removeprefix("#")},
        )
        for name, retry in retries.items()
    }

    assert {name: response.status_code for name, response in answers.items()} == {
        LISTS[0].name: 200,
        OVERVIEW.name: 200,
    }
    assert not any("state--error" in response.text for response in answers.values())
    assert not any("<html" in response.text for response in answers.values())


async def test_the_overview_region_is_in_the_page_once_and_a_page_load_gets_no_fragment(
    admin_client: AsyncClient, world: World, container_host: ContainerEngine
) -> None:
    take_down(container_host)
    path = f"/environments/{world.remote_id}"

    page = await admin_client.get(path)
    untargeted = await admin_client.get(path, headers={"HX-Request": "true", "HX-Target": "other"})

    assert page.text.count('id="environment-overview"') == 1
    assert [
        tag.attrs.get("hx-target")
        for tag in parse_tags(page.text)
        if tag.name == "a" and tag.attrs.get("class") == "btn" and "hx-target" in tag.attrs
    ] == ["#environment-overview"]
    assert "<html" in untargeted.text and 'id="environment-overview"' in untargeted.text


@pytest.mark.parametrize("region_id", ["", "1tabs", "has space", "x" * 65, "a.b", "a#b"], ids=repr)
def test_a_region_id_outside_the_grammar_is_refused(region_id: str) -> None:
    with pytest.raises(ValueError, match="not a region id"):
        Region(region_id, "outerHTML")


@pytest.mark.parametrize("region_id", ["a", "container-tabs", "A_b-9", "x" * 64])
def test_a_region_id_inside_the_grammar_is_accepted(region_id: str) -> None:
    assert Region(region_id, "innerHTML").id == region_id


def calls_without_a_region() -> list[str]:
    missing: list[str] = []
    for path in sorted(WEB_ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in GUARDED_CALLS
                and not any(keyword.arg == "region" for keyword in node.keywords)
            ):
                missing.append(f"{path.relative_to(WEB_ROOT)}:{node.lineno}")
    return missing


def test_no_problem_page_or_unavailable_page_call_leaves_out_its_region() -> None:
    assert calls_without_a_region() == []
