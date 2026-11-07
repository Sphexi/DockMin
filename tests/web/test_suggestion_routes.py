"""The name suggestion routes: fragment shape, who is offered what, and the component's markup."""

import asyncio
import hashlib
from http import HTTPStatus
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.auth.principal import Principal
from app.db.repositories.stack import StackKind, StackRepository
from app.db.repositories.team import TeamRepository
from app.services.name_suggestions import MAX_SUGGESTIONS, NameOption, NameSuggestions
from app.web.suggestion_routes import SuggestionFlights
from tests.container_support import plant_control
from tests.docker_engine.fake_engine import error_reply
from tests.docker_engine.swarm_engine import SwarmEngine
from tests.image_support import image_id
from tests.object_support import add_container
from tests.web.environment_support import World
from tests.web.helpers import Tag, parse_tags, render_macro

OK, NOT_FOUND = HTTPStatus.OK, HTTPStatus.NOT_FOUND
FIELD = "name"
UNMATCHED = "zzz"
UNKNOWN_ENVIRONMENT = 999_999
COMPOSE_FILE = "services:\n  web:\n    image: nginx:1\n"
DIGEST = "d" * 64
DIRECTORY_CHARS = 32
MORE_THAN_CAP = MAX_SUGGESTIONS + 5
TIMEOUT = 10
ENVIRONMENT_KINDS = ("containers", "images", "volumes", "networks", "stacks", "services")
SWARM_OBJECT_KINDS = ("configs", "secrets")
HOST_KINDS = (*ENVIRONMENT_KINDS, *SWARM_OBJECT_KINDS)
HOST_READ_KINDS = tuple(kind for kind in HOST_KINDS if kind != "stacks")
LISTINGS = {
    "containers": "GET /containers/json",
    "images": "GET /images/json",
    "volumes": "GET /volumes",
    "networks": "GET /networks",
    "services": "GET /services",
    "configs": "GET /configs",
    "secrets": "GET /secrets",
}


def environment_path(world: World, kind: str, environment_id: int | None = None) -> str:
    return f"/environments/{environment_id or world.remote_id}/suggestions/{kind}"


def typed(text: str, **more: str) -> dict[str, str]:
    return {"field": FIELD, FIELD: text, **more}


def options_of(response: Response) -> list[tuple[str, str | None]]:
    return [
        (tag.attrs["value"], tag.attrs.get("label"))
        for tag in parse_tags(response.text)
        if tag.name == "option"
    ]


def names_of(response: Response) -> list[str]:
    return [value for value, _ in options_of(response)]


def shape(response: Response) -> tuple[int, str, str, str, bytes]:
    """Everything of a response a caller could tell two answers apart by."""
    headers = response.headers
    return (
        response.status_code,
        headers["content-type"],
        headers["cache-control"],
        headers.get("vary", ""),
        response.content,
    )


async def seed_stack(
    app: FastAPI, environment_id: int, name: str, kind: StackKind = "compose"
) -> None:
    async with app.state.database.session() as db:
        await StackRepository(db, app.state.vault, app.state.auth.clock).insert(
            environment_id=environment_id,
            name=name,
            kind=kind,
            origin="created",
            project_directory=hashlib.sha256(name.encode()).hexdigest()[:DIRECTORY_CHARS],
            compose_file=COMPOSE_FILE,
            compose_file_digest=DIGEST,
            created_by_user_id=None,
        )


async def seed_team(app: FastAPI, name: str) -> None:
    async with app.state.database.session() as db:
        await TeamRepository(db, app.state.auth.clock).create(name)


def stock_each_kind(host: SwarmEngine, prefix: str, number: int) -> dict[str, str]:
    """Add one object of every host kind named from `prefix`; return their ids by kind."""
    host.add_image(f"{prefix.lower()}:1", image_id(number))
    host.add_volume(prefix)
    return {
        "containers": add_container(host, number, prefix).id,
        "networks": host.add_network(prefix, number),
        "services": host.named_service(prefix),
        "configs": host.add_config(prefix),
        "secrets": host.add_secret(prefix),
    }


async def test_every_environment_kind_offers_matching_names_sorted_without_case_and_not_cached(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    for number, prefix in enumerate(("Web-b", "web-a", "other"), start=1):
        stock_each_kind(swarm_host, prefix, number)
    for stack in ("web-b", "web-a", "other"):
        await seed_stack(world.app, world.remote_id, stack)

    answers = {
        kind: await admin_client.get(environment_path(world, kind), params=typed("WEB"))
        for kind in HOST_KINDS
    }

    assert {kind: names_of(response) for kind, response in answers.items()} == {
        "containers": ["web-a", "Web-b"],
        "images": ["web-a:1", "web-b:1"],
        "volumes": ["web-a", "Web-b"],
        "networks": ["web-a", "Web-b"],
        "stacks": ["web-a", "web-b"],
        "services": ["web-a", "Web-b"],
        "configs": ["web-a", "Web-b"],
        "secrets": ["web-a", "Web-b"],
    }
    assert {r.status_code for r in answers.values()} == {OK}
    assert {r.headers["cache-control"] for r in answers.values()} == {"no-store"}
    assert {r.headers["content-type"].split(";")[0] for r in answers.values()} == {"text/html"}
    assert all("HX-Request" in r.headers["vary"] for r in answers.values())
    assert all("<html" not in r.text for r in answers.values())
    assert {kind: swarm_host.paths().count(path) for kind, path in LISTINGS.items()} == (
        dict.fromkeys(LISTINGS, 1)
    )


RESOURCE_TYPES = {
    "containers": "container",
    "volumes": "volume",
    "networks": "network",
    "stacks": "stack",
    "services": "service",
    "configs": "config",
    "secrets": "secret",
}
CONTROLLED_KINDS = tuple(RESOURCE_TYPES)


async def test_a_standard_user_gets_the_same_answer_for_hidden_objects_as_for_no_match_in_each_kind(
    standard_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    stock_each_kind(swarm_host, "private-x", 1)
    shared = stock_each_kind(swarm_host, "shared-x", 2)
    shared |= {"volumes": "shared-x", "stacks": "shared-x"}
    for stack in ("private-x", "shared-x"):
        await seed_stack(world.app, world.remote_id, stack)
    for kind, resource_id in shared.items():
        if kind in RESOURCE_TYPES:
            await plant_control(
                world.app,
                world.remote_id,
                RESOURCE_TYPES[kind],  # type: ignore[arg-type]
                resource_id,
                public=True,
            )

    async def ask(text: str) -> dict[str, Response]:
        return {
            kind: await standard_client.get(environment_path(world, kind), params=typed(text))
            for kind in CONTROLLED_KINDS
        }

    hidden, nothing, visible = await ask("private"), await ask(UNMATCHED), await ask("shared")

    assert {k: shape(r) for k, r in hidden.items()} == {k: shape(r) for k, r in nothing.items()}
    assert {r.status_code for r in hidden.values()} == {OK}
    assert {kind: names_of(response) for kind, response in visible.items()} == {
        kind: ["shared-x"] for kind in CONTROLLED_KINDS
    }
    assert all(names_of(r) == [] for r in nothing.values())


async def test_an_environment_that_is_hidden_missing_or_unreachable_answers_like_no_match(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
) -> None:
    stock_each_kind(swarm_host, "web-x", 1)

    async def ask(client: AsyncClient, environment_id: int, text: str) -> dict[str, Response]:
        return {
            kind: await client.get(
                environment_path(world, kind, environment_id), params=typed(text)
            )
            for kind in HOST_KINDS
        }

    no_match = await ask(admin_client, world.remote_id, UNMATCHED)
    not_visible = await ask(standard_client, world.down_id, "web")
    missing = await ask(admin_client, UNKNOWN_ENVIRONMENT, "web")
    swarm_host.listing_status = HTTPStatus.INTERNAL_SERVER_ERROR
    swarm_host.outage = {"services", "configs", "secrets"}
    swarm_host.route("GET", "/volumes", error_reply(HTTPStatus.BAD_GATEWAY))
    swarm_host.route("GET", "/networks", error_reply(HTTPStatus.BAD_GATEWAY))
    swarm_host.fail("image_list", HTTPStatus.BAD_GATEWAY)
    unreachable = await ask(admin_client, world.remote_id, "web")

    expected = {kind: shape(response) for kind, response in no_match.items()}
    assert {kind: shape(r) for kind, r in not_visible.items()} == expected
    assert {kind: shape(r) for kind, r in missing.items()} == expected
    assert {kind: shape(unreachable[kind]) for kind in HOST_READ_KINDS} == {
        kind: expected[kind] for kind in HOST_READ_KINDS
    }


async def test_an_administrator_is_offered_users_and_grantees_with_labels_and_a_standard_user_none(
    admin_client: AsyncClient, standard_client: AsyncClient, world: World
) -> None:
    await seed_team(world.app, "standard-team")

    users = await admin_client.get("/suggestions/users", params=typed("STAND"))
    grantees = await admin_client.get("/suggestions/grantees", params=typed("stand"))
    refused = {
        kind: await standard_client.get(f"/suggestions/{kind}", params=typed("stand"))
        for kind in ("users", "grantees")
    }
    quiet = await admin_client.get("/suggestions/grantees", params=typed(UNMATCHED))

    assert options_of(users) == [("standard-user", None)]
    assert options_of(grantees) == [("standard-user", "user"), ("standard-team", "team")]
    assert {kind: shape(r) for kind, r in refused.items()} == {
        kind: shape(quiet) for kind in refused
    }
    assert users.headers["cache-control"] == "no-store"


CONTAINERS = "/environments/1/suggestions/containers"
USERS = "/suggestions/users"
UNKNOWN_KIND_PATHS = (
    "/environments/1/suggestions/nodes",
    "/environments/1/suggestions/users",
    "/environments/1/suggestions/",
    "/suggestions/containers",
    "/suggestions/teams",
)
REFUSED_FIELDS: dict[str, Any] = {
    "missing": [("name", "web")],
    "empty": [("field", ""), ("name", "web")],
    "with a space": [("field", "na me"), ("na me", "web")],
    "with a dot": [("field", "a.b"), ("a.b", "web")],
    "with markup": [("field", "<b>"), ("<b>", "web")],
    "too long": [("field", "f" * 65), ("f" * 65, "web")],
}
ANSWERED_EMPTY: dict[str, Any] = {
    "typed field absent": [("field", "name")],
    "typed in another input": [("field", "name"), ("other", "web")],
    "typed twice": [("field", "name"), ("name", "web"), ("name", "wet")],
    "typed text empty": [("field", "name"), ("name", "")],
    "typed text blank": [("field", "name"), ("name", "   ")],
    "typed text too long": [("field", "name"), ("name", "x" * 513)],
}


async def test_a_missing_or_malformed_field_is_not_found_and_unusable_input_answers_empty(
    admin_client: AsyncClient,
) -> None:
    refused = {
        label: [
            (await admin_client.get(path, params=params)).status_code
            for path in (CONTAINERS, USERS)
        ]
        for label, params in REFUSED_FIELDS.items()
    }
    answered = {
        label: [await admin_client.get(path, params=params) for path in (CONTAINERS, USERS)]
        for label, params in ANSWERED_EMPTY.items()
    }
    unknown = {
        path: (await admin_client.get(path, params=typed("web"))).status_code
        for path in UNKNOWN_KIND_PATHS
    }

    assert refused == {label: [NOT_FOUND, NOT_FOUND] for label in REFUSED_FIELDS}
    assert {label: [r.status_code for r in each] for label, each in answered.items()} == {
        label: [OK, OK] for label in ANSWERED_EMPTY
    }
    assert {label: [options_of(r) for r in each] for label, each in answered.items()} == {
        label: [[], []] for label in ANSWERED_EMPTY
    }
    assert unknown == dict.fromkeys(UNKNOWN_KIND_PATHS, NOT_FOUND)


async def test_a_signed_out_request_is_sent_to_sign_in_and_offered_no_names(
    anonymous_client: AsyncClient, world: World
) -> None:
    htmx = {"HX-Request": "true"}
    path = environment_path(world, "containers")

    page = await anonymous_client.get(path, params=typed("web"))
    fragment = await anonymous_client.get(path, params=typed("web"), headers=htmx)
    users = await anonymous_client.get(USERS, params=typed("a"), headers=htmx)

    assert page.status_code == HTTPStatus.SEE_OTHER and page.headers["location"].startswith(
        "/login"
    )
    assert (fragment.status_code, users.status_code) == (HTTPStatus.UNAUTHORIZED,) * 2
    assert "HX-Redirect" in fragment.headers and options_of(fragment) == []


RESTARTER = "stack-restarter"
UPDATER = "container-updater"
HEALTH_CHECKER = "container-health-checker"
RULE_TARGETS = "rule-targets"


async def stock_shop(world: World, host: SwarmEngine) -> None:
    add_container(host, 1, "shop-box")
    host.named_service("shop-svc")
    await seed_stack(world.app, world.remote_id, "shop-c", "compose")
    await seed_stack(world.app, world.remote_id, "shop-s", "swarm")


async def test_the_stacks_kind_offers_the_stack_kinds_asked_for(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    await stock_shop(world, swarm_host)
    path = environment_path(world, "stacks")

    asked = {
        choice: names_of(await admin_client.get(path, params=typed("shop", stack_kind=choice)))
        for choice in ("compose", "swarm", "any", "unknown")
    }
    unspecified = await admin_client.get(path, params=typed("shop"))

    assert asked == {
        "compose": ["shop-c"],
        "swarm": ["shop-s"],
        "any": ["shop-c", "shop-s"],
        "unknown": [],
    }
    assert names_of(unspecified) == ["shop-c", "shop-s"]


@pytest.mark.parametrize(
    ("subsystem", "target_kind", "expected"),
    [
        pytest.param(RESTARTER, "stack", ["shop-c"], id="restarter stacks are compose only"),
        pytest.param(UPDATER, "stack", ["shop-c", "shop-s"], id="updater stacks are both kinds"),
        pytest.param(RESTARTER, "container", ["shop-box"], id="restarter containers"),
        pytest.param(UPDATER, "service", ["shop-svc"], id="updater services"),
        pytest.param(HEALTH_CHECKER, "container", ["shop-box"], id="health checker containers"),
        pytest.param(
            HEALTH_CHECKER, "stack", ["shop-c"], id="health checker stacks are compose only"
        ),
        pytest.param(HEALTH_CHECKER, "service", [], id="health checker has no service targets"),
        pytest.param(RESTARTER, "service", [], id="restarter has no service targets"),
        pytest.param(UPDATER, "labels", [], id="labels are not a name"),
        pytest.param(UPDATER, "bogus", [], id="unknown target kind"),
        pytest.param("nonexistent", "container", [], id="unknown subsystem"),
        pytest.param("backup-syncer", "container", [], id="subsystem with no targets"),
    ],
)
async def test_rule_targets_follow_the_subsystem_and_the_chosen_target_kind(
    admin_client: AsyncClient,
    world: World,
    swarm_host: SwarmEngine,
    subsystem: str,
    target_kind: str,
    expected: list[str],
) -> None:
    await stock_shop(world, swarm_host)

    response = await admin_client.get(
        environment_path(world, RULE_TARGETS),
        params=typed("shop", subsystem=subsystem, target_kind=target_kind),
    )

    assert response.status_code == OK and names_of(response) == expected


async def test_rule_targets_without_a_subsystem_or_target_kind_offer_nothing_and_read_nothing(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    await stock_shop(world, swarm_host)
    path = environment_path(world, RULE_TARGETS)

    bare = await admin_client.get(path, params=typed("shop"))
    no_subsystem = await admin_client.get(path, params=typed("shop", target_kind="container"))
    no_kind = await admin_client.get(path, params=typed("shop", subsystem=RESTARTER))

    assert [names_of(r) for r in (bare, no_subsystem, no_kind)] == [[], [], []]
    assert [p for p in swarm_host.paths() if p in LISTINGS.values()] == []


USER = Principal(1, "someone", "standard")
OTHER_USER = Principal(2, "another", "standard")
CONTAINER_KIND, IMAGE_KIND = "containers", "images"


class Held:
    """A read that waits to be released and notes whether it was cancelled."""

    def __init__(self, value: str, *, hold: bool = True) -> None:
        self.value = value
        self.gate = asyncio.Event()
        self.hold = hold
        self.cancelled = False

    async def read(self) -> tuple[NameOption, ...]:
        try:
            if self.hold:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return (NameOption(self.value),)


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_a_newer_read_cancels_the_same_users_earlier_read_of_that_kind() -> None:
    flights = SuggestionFlights()
    first, second = Held("first"), Held("second", hold=False)
    earlier = asyncio.create_task(flights.run(USER, CONTAINER_KIND, first.read()))
    await settle()

    latest = await flights.run(USER, CONTAINER_KIND, second.read())

    assert await earlier == ()
    assert first.cancelled and latest == (NameOption("second"),)


async def test_a_read_of_another_kind_or_by_another_user_is_not_cancelled() -> None:
    flights = SuggestionFlights()
    held = Held("held")
    running = asyncio.create_task(flights.run(USER, CONTAINER_KIND, held.read()))
    await settle()

    other_kind = await flights.run(USER, IMAGE_KIND, Held("kind", hold=False).read())
    other_user = await flights.run(OTHER_USER, CONTAINER_KIND, Held("user", hold=False).read())
    held.gate.set()

    assert other_kind == (NameOption("kind"),) and other_user == (NameOption("user"),)
    assert await running == (NameOption("held"),) and not held.cancelled


async def test_a_finished_read_leaves_nothing_to_cancel_and_the_next_read_runs() -> None:
    flights = SuggestionFlights()

    first = await flights.run(USER, CONTAINER_KIND, Held("one", hold=False).read())
    second = await flights.run(USER, CONTAINER_KIND, Held("two", hold=False).read())

    assert first == (NameOption("one"),) and second == (NameOption("two"),)


async def test_cancelling_the_caller_of_a_read_cancels_the_read_and_propagates() -> None:
    flights = SuggestionFlights()
    held = Held("held")
    caller = asyncio.create_task(flights.run(USER, CONTAINER_KIND, held.read()))
    await settle()

    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert held.cancelled


async def test_a_read_that_fails_raises_to_its_caller_and_does_not_block_the_next() -> None:
    flights = SuggestionFlights()

    async def failing() -> tuple[NameOption, ...]:
        raise RuntimeError("the read failed")

    with pytest.raises(RuntimeError):
        await flights.run(USER, CONTAINER_KIND, failing())
    after = await flights.run(USER, CONTAINER_KIND, Held("next", hold=False).read())

    assert after == (NameOption("next"),)


async def test_no_route_offers_a_whole_list_however_many_names_match(
    admin_client: AsyncClient, world: World, swarm_host: SwarmEngine
) -> None:
    for number in range(MORE_THAN_CAP):
        add_container(swarm_host, number + 1, f"c{number:02d}")
        await seed_stack(world.app, world.remote_id, f"c{number:02d}")

    answers = {
        kind: await admin_client.get(environment_path(world, kind), params=typed("c"))
        for kind in ("containers", "stacks")
    }
    blank = await admin_client.get(environment_path(world, "containers"), params=typed(""))

    assert {kind: len(names_of(r)) for kind, r in answers.items()} == {
        "containers": MAX_SUGGESTIONS,
        "stacks": MAX_SUGGESTIONS,
    }
    assert names_of(answers["containers"]) == [f"c{n:02d}" for n in range(MAX_SUGGESTIONS)]
    assert names_of(blank) == []


class Pending:
    """Stands in for the environment read: each request waits until released, by what was typed."""

    def __init__(self) -> None:
        self.started: dict[str, asyncio.Event] = {}
        self.released = asyncio.Event()

    def event(self, text: str) -> asyncio.Event:
        return self.started.setdefault(text, asyncio.Event())

    async def in_environment(
        self, actor: Principal, environment_id: int, kind: str, text: str, **_: object
    ) -> tuple[NameOption, ...]:
        self.event(text).set()
        await self.released.wait()
        return (NameOption(text),)


async def test_a_newer_request_supersedes_the_same_users_older_one_of_that_kind_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = Pending()
    monkeypatch.setattr(NameSuggestions, "in_environment", pending.in_environment)
    containers, images = (environment_path(world, kind) for kind in ("containers", "images"))

    def get(client: AsyncClient, path: str, text: str) -> asyncio.Task[Response]:
        return asyncio.create_task(client.get(path, params=typed(text)))

    older = get(admin_client, containers, "older")
    await asyncio.wait_for(pending.event("older").wait(), TIMEOUT)
    newer = get(admin_client, containers, "newer")
    await asyncio.wait_for(pending.event("newer").wait(), TIMEOUT)
    other_kind = get(admin_client, images, "other-kind")
    other_user = get(standard_client, containers, "other-user")
    await asyncio.wait_for(pending.event("other-user").wait(), TIMEOUT)
    superseded = await asyncio.wait_for(older, TIMEOUT)
    pending.released.set()
    answers = await asyncio.wait_for(asyncio.gather(newer, other_kind, other_user), TIMEOUT)

    assert superseded.status_code == OK and options_of(superseded) == []
    assert [names_of(r) for r in answers] == [["newer"], ["other-kind"], ["other-user"]]


SUGGEST_HREF = "/environments/2/suggestions/containers"
HOSTILE_VALUE = '"><script>alert(1)</script>&'


def field_html(**more: Any) -> str:
    return render_macro(
        "name_field", "name_field", name="target", label="Target", suggest_href=SUGGEST_HREF, **more
    )


def cell_html(**more: Any) -> str:
    return render_macro(
        "name_field",
        "name_cell",
        name="target_name",
        label="Name",
        subject="Target",
        suggest_href=SUGGEST_HREF,
        **more,
    )


def tags_named(html: str, name: str) -> list[Tag]:
    return [tag for tag in parse_tags(html) if tag.name == name]


@pytest.mark.parametrize(
    ("html", "input_id", "list_id"),
    [
        pytest.param(field_html(), "field-target", "suggest-target", id="field"),
        pytest.param(cell_html(), "field-target_name", "suggest-target_name", id="cell"),
        pytest.param(
            cell_html(id_suffix="target_name-3"),
            "field-target_name-3",
            "suggest-target_name-3",
            id="cell with a row suffix",
        ),
    ],
)
def test_the_component_is_a_plain_text_input_tied_to_an_empty_datalist_and_needs_no_script(
    html: str, input_id: str, list_id: str
) -> None:
    [field] = tags_named(html, "input")
    [datalist] = tags_named(html, "datalist")

    assert (field.attrs["type"], field.attrs["id"], field.attrs["list"]) == (
        "text",
        input_id,
        list_id,
    )
    assert field.attrs["autocomplete"] == "off"
    assert datalist.attrs["id"] == list_id
    assert f'<datalist id="{list_id}"></datalist>' in html
    assert tags_named(html, "script") == [] and tags_named(html, "option") == []
    assert not [a for tag in parse_tags(html) for a in tag.attrs if a.startswith("on")]
    assert [a for a in field.attrs if a.startswith("hx-")] == []
    assert [t.attrs["for"] for t in tags_named(html, "label")] == [input_id]


@pytest.mark.parametrize(
    ("html", "name"),
    [
        pytest.param(field_html(), "target", id="field"),
        pytest.param(cell_html(), "target_name", id="cell"),
    ],
)
def test_the_suggestion_request_sits_on_the_wrapper_and_names_the_field_to_read(
    html: str, name: str
) -> None:
    [wrapper] = [
        t for t in tags_named(html, "div") if "suggest" in t.attrs.get("class", "").split()
    ]

    assert wrapper.attrs["hx-get"] == f"{SUGGEST_HREF}?field={name}"
    assert wrapper.attrs["hx-target"] == "find datalist"
    assert wrapper.attrs["hx-swap"] == "innerHTML"
    assert "keyup" in wrapper.attrs["hx-trigger"] and "delay:" in wrapper.attrs["hx-trigger"]
    assert wrapper.attrs["hx-include"].split(",")[0] == "find input"


def test_a_suggest_address_that_has_a_query_gets_the_field_appended_with_an_ampersand() -> None:
    html = render_macro(
        "name_field",
        "name_field",
        name="target",
        label="Target",
        suggest_href=f"{SUGGEST_HREF}?stack_kind=compose",
    )

    [wrapper] = [t for t in tags_named(html, "div") if "hx-get" in t.attrs]

    assert wrapper.attrs["hx-get"] == f"{SUGGEST_HREF}?stack_kind=compose&field=target"


def test_the_component_keeps_the_value_the_requirement_and_the_error_on_the_plain_input() -> None:
    html = field_html(value="web", required=True, error="Name is taken.", placeholder="a name")

    [field] = tags_named(html, "input")

    assert field.attrs["value"] == "web" and "required" in field.attrs
    assert field.attrs["aria-invalid"] == "true"
    assert field.attrs["aria-describedby"] == "field-target-error"
    assert field.attrs["placeholder"] == "a name"
    assert "Name is taken." in html


def test_the_component_escapes_a_hostile_value() -> None:
    html = field_html(value=HOSTILE_VALUE)

    assert tags_named(html, "script") == []
    assert tags_named(html, "input")[0].attrs["value"] == HOSTILE_VALUE


def test_the_options_fragment_lists_plain_names_and_labelled_objects_escaped() -> None:
    html = render_macro(
        "name_options",
        "name_options",
        options=[NameOption("alpha"), NameOption("beta", "read-only"), NameOption(HOSTILE_VALUE)],
    )

    options = [(t.attrs["value"], t.attrs.get("label")) for t in tags_named(html, "option")]

    assert options == [("alpha", None), ("beta", "read-only"), (HOSTILE_VALUE, None)]
    assert tags_named(html, "script") == []


def test_the_options_fragment_accepts_one_name_and_none() -> None:
    single = render_macro("name_options", "name_options", options="solo")
    empty = render_macro("name_options", "name_options", options=[])

    assert [t.attrs["value"] for t in tags_named(single, "option")] == ["solo"]
    assert tags_named(empty, "option") == []
