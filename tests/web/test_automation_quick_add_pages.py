"""The quick add pages: the list on each object page, the confirmation, and the confirmed post."""

import json
import re
from html import unescape
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.services.automation_rules import NO_CREATE_ACCESS, NO_WRITE_ACCESS
from app.services.container_derivation import SWARM_SERVICE_LABEL
from app.services.jobs import NOT_AVAILABLE
from app.services.object_inspector import EnvironmentUnavailable
from tests.access_support import container_id, service_id
from tests.auth_support import ADMIN_ID, STANDARD_ID, csrf_form
from tests.container_support import ABSENT, CACHE, DB, JOB, WORKER, plant_control
from tests.docker_engine.container_engine import ContainerEngine
from tests.docker_engine.swarm_engine import SwarmEngine, plain_spec
from tests.job_support import events, execute, last_event_id, rows, withhold_handler
from tests.quick_add_support import (
    HEALTH,
    NEVER,
    RESTARTER,
    UPDATER,
    hold_containers,
    insert_rule,
    seed_compose_stack,
    seed_standard_user,
    selector_json,
)
from tests.web.environment_support import World
from tests.web.helpers import hidden_inputs, inputs_named, rule_binding
from tests.web.registry_support import visible_text
from tests.web.test_swarm_stack_pages import Browser, create_shop
from tests.webhook_support import HookFactory

OK = 200
SEE_OTHER = 303
FORBIDDEN = 403
NOT_FOUND = 404
UNPROCESSABLE = 422
UNAVAILABLE = 503
ADD_PATH = "/automation/rules/-/quick-add"
UPDATED = "automation_rule_updated"
LABEL = "Add this {} to an automation"
BUTTON = "Add to automation"
COMPOSE_HELP = "To add this container's stack to a container-updater or webhook-receiver automation"
TASK_HELP = "Swarm replaces this task container under a new name"
STAYS_ENABLED = "The automation stays enabled and acts on this"
OWNER_ACCESS = "Runs of this automation act with the access of its owner"
TASK_ID = container_id(0xB2)
SWARM_CONTAINER_ID = container_id(0xB1)
MANAGED_REFUSAL = "db is managed by the Compose project shop"
SHOP = "Shop"
WORDS = {"container": "container", "stack": "stack", "swarm_stack": "stack", "service": "service"}
BLOG = "blog"
WORKER_SELECTOR = selector_json("container", "worker")
FIRST_HELD = "a" * 64
SECOND_HELD = "b" * 64
ONE_RELEASED = "1 held container is released; the next run may restart it."
TWO_RELEASED = "2 held containers are released; the next run may restart them."
RELEASE_LINE = "released; the next run may restart"
READ_ONLY_OWNER = "can see this container but cannot change it"
HIDDEN_FROM_OWNER = "cannot see this container"
LOST_OWNER = "account is disabled or removed"
WEBHOOK_OPEN = "anyone holding its webhook token can act on this container"
STAYS_ENABLED_STACK = "The automation stays enabled and acts on this Swarm stack"
READ_ONLY_SWARM_STACK = "can see this Swarm stack but cannot change it"
HIDDEN_SWARM_STACK = "cannot see this Swarm stack"


def options_of(html: str) -> list[str]:
    block = re.search(r'<select[^>]*name="rule_id".*?</select>', html, flags=re.S)
    if block is None:
        return []
    return [unescape(text) for text in re.findall(r"<option[^>]*>([^<]*)</option>", block.group(0))]


def object_values(html: str) -> list[str]:
    return [tag.attrs["value"] for tag in inputs_named(html, "object")]


async def add_data(
    client: AsyncClient, app: FastAPI, rule_id: int, reference: str, name: str
) -> dict[str, Any]:
    binding = await rule_binding(app, rule_id)
    return csrf_form(client, rule_id=str(rule_id), object=reference, expected_name=name, **binding)


def confirm_path(rule_id: int, reference: str) -> str:
    return f"{ADD_PATH}?rule_id={rule_id}&object={reference}"


async def selector_names(app: FastAPI, rule_id: int) -> list[str]:
    [found] = await rows(
        app, "SELECT target_selector FROM automation_rule WHERE id = ?", (rule_id,)
    )
    return list(json.loads(found["target_selector"])["names"])


async def test_each_object_page_lists_the_automations_and_a_confirmed_add_returns_to_its_tab(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world = swarm_stacks_world
    app, remote = world.app, world.remote_id
    swarm_host.add(SWARM_CONTAINER_ID, "cache")
    service = swarm_host.add_service(plain_spec("api"))
    await seed_compose_stack(app, remote, BLOG)
    await create_shop(Browser(admin_client), world)
    container_rule = await insert_rule(
        app, subsystem=HEALTH, selector=WORKER_SELECTOR, name="beat", environment_id=remote
    )
    stack_rule = await insert_rule(
        app,
        subsystem=RESTARTER,
        selector=selector_json("stack", "other"),
        name="roll",
        environment_id=remote,
    )
    swarm_stack_rule = await insert_rule(
        app,
        subsystem=UPDATER,
        selector=selector_json("stack", "other"),
        name="redeploy",
        environment_id=remote,
    )
    service_rule = await insert_rule(
        app,
        subsystem=UPDATER,
        selector=selector_json("service", "web"),
        name="rollout",
        environment_id=remote,
    )
    pages = {
        "container": f"/environments/{remote}/containers/{SWARM_CONTAINER_ID}",
        "stack": f"/environments/{remote}/stacks/{BLOG}",
        "swarm_stack": f"/environments/{remote}/swarm/stacks/{SHOP}",
        "service": f"/environments/{remote}/swarm/services/{service}",
    }
    references = {
        "container": f"container:{SWARM_CONTAINER_ID}",
        "stack": f"stack:{BLOG}",
        "swarm_stack": f"swarm_stack:{SHOP}",
        "service": f"service:{service}",
    }
    names = {"container": "cache", "stack": BLOG, "swarm_stack": SHOP, "service": "api"}
    rule_ids = {
        "container": container_rule,
        "stack": stack_rule,
        "swarm_stack": swarm_stack_rule,
        "service": service_rule,
    }

    tabs = {kind: await admin_client.get(f"{path}?tab=automation") for kind, path in pages.items()}
    posted = {
        kind: await admin_client.post(
            ADD_PATH,
            data=await add_data(admin_client, app, rule_ids[kind], references[kind], names[kind]),
        )
        for kind in pages
    }

    assert {
        kind: LABEL.format(WORDS[kind]) in visible_text(tab.text) for kind, tab in tabs.items()
    } == dict.fromkeys(pages, True)
    assert {kind: object_values(tab.text) for kind, tab in tabs.items()} == {
        kind: [references[kind]] for kind in pages
    }
    assert all(BUTTON in visible_text(tab.text) for tab in tabs.values())
    assert [options_of(tabs[kind].text) for kind in pages] == [
        [f"beat ({HEALTH}: Container worker)"],
        [f"roll ({RESTARTER}: Stack other)", f"redeploy ({UPDATER}: Stack other)"],
        [f"redeploy ({UPDATER}: Stack other)"],
        [f"rollout ({UPDATER}: Swarm service web)"],
    ]
    assert {kind: response.status_code for kind, response in posted.items()} == dict.fromkeys(
        pages, SEE_OTHER
    )
    assert {kind: response.headers["location"] for kind, response in posted.items()} == {
        kind: f"{path}?tab=automation" for kind, path in pages.items()
    }
    assert await selector_names(app, container_rule) == ["cache", "worker"]
    assert await selector_names(app, stack_rule) == [BLOG, "other"]
    assert await selector_names(app, swarm_stack_rule) == [SHOP, "other"]
    assert await selector_names(app, service_rule) == ["api", "web"]


async def test_the_container_page_lists_what_fits_and_points_elsewhere_where_it_cannot(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    app, remote, local = world.app, world.remote_id, world.local_id
    shared_host.add(TASK_ID, "task", labels={SWARM_SERVICE_LABEL: service_id(1)})
    await insert_rule(
        app, subsystem=HEALTH, selector=WORKER_SELECTOR, name="beat", environment_id=remote
    )
    await insert_rule(
        app, subsystem=UPDATER, selector=WORKER_SELECTOR, name="update", environment_id=remote
    )
    await insert_rule(
        app,
        subsystem=UPDATER,
        selector=selector_json("service", "web"),
        name="rollout",
        environment_id=remote,
    )
    await insert_rule(
        app,
        subsystem=HEALTH,
        selector=WORKER_SELECTOR,
        name="mine",
        environment_id=local,
        owner=STANDARD_ID,
    )
    await plant_control(app, local, "container", DB, public=True, level="write")
    base = f"/environments/{remote}/containers"

    plain = await admin_client.get(f"{base}/{CACHE}?tab=automation")
    managed = await admin_client.get(f"{base}/{DB}?tab=automation")
    task = await admin_client.get(f"{base}/{TASK_ID}?tab=automation")
    read_only = await standard_client.get(f"/environments/{local}/containers/{DB}?tab=automation")

    plain_text, managed_text = visible_text(plain.text), visible_text(managed.text)
    assert LABEL.format("container") in plain_text and BUTTON in plain_text
    assert options_of(plain.text) == [
        f"beat ({HEALTH}: Container worker)",
        f"update ({UPDATER}: Container worker)",
    ]
    assert COMPOSE_HELP not in plain_text and TASK_HELP not in plain_text
    assert LABEL.format("container") in managed_text
    assert options_of(managed.text) == [f"beat ({HEALTH}: Container worker)"]
    assert COMPOSE_HELP in managed_text
    task_text = visible_text(task.text)
    assert LABEL.format("container") not in task_text and BUTTON not in task_text
    assert TASK_HELP in task_text and "Add automation" in task_text
    assert read_only.status_code == OK
    assert LABEL.format("container") not in visible_text(read_only.text)
    assert options_of(read_only.text) == []


async def test_the_confirmation_describes_the_add_and_a_refused_one_offers_no_confirm(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    app, remote = world.app, world.remote_id
    users = await insert_rule(
        app,
        subsystem=HEALTH,
        selector=WORKER_SELECTOR,
        name="beat",
        environment_id=remote,
        owner=STANDARD_ID,
    )
    mine = await insert_rule(
        app, subsystem=UPDATER, selector=WORKER_SELECTOR, name="update", environment_id=remote
    )
    paused = await insert_rule(
        app,
        subsystem=RESTARTER,
        selector=WORKER_SELECTOR,
        name="paused",
        environment_id=remote,
        paused_until=NEVER,
    )
    failing = await insert_rule(
        app,
        subsystem=HEALTH,
        selector=selector_json("container", "job"),
        name="failing",
        environment_id=remote,
        job_disabled=True,
    )

    await plant_control(app, remote, "container", CACHE, public=True, level="write")

    def confirm(rule_id: int, object_id: str) -> str:
        return f"{ADD_PATH}?rule_id={rule_id}&object=container:{object_id}"

    offered = await admin_client.get(confirm(users, CACHE))
    refused = await admin_client.get(confirm(mine, DB))
    own = await admin_client.get(confirm(mine, CACHE))
    on_pause = await admin_client.get(confirm(paused, CACHE))
    on_failure = await admin_client.get(confirm(failing, CACHE))

    offered_text, refused_text = visible_text(offered.text), visible_text(refused.text)
    assert offered.status_code == OK and offered.headers["cache-control"] == "no-store"
    assert BUTTON in offered_text and STAYS_ENABLED in offered_text
    assert OWNER_ACCESS in offered_text
    fields = hidden_inputs(offered.text)
    assert (fields["rule_id"], fields["object"], fields["expected_name"]) == (
        str(users),
        f"container:{CACHE}",
        "cache",
    )
    assert fields["expected_digest"] and fields["expected_updated_at"]
    assert refused.status_code == OK
    assert MANAGED_REFUSAL in refused_text
    assert "expected_digest" not in hidden_inputs(refused.text)
    assert STAYS_ENABLED not in refused_text
    assert OWNER_ACCESS not in visible_text(own.text)
    assert "Paused" in visible_text(on_pause.text)
    assert "Disabled after" in visible_text(on_failure.text)
    assert all("expected_digest" in hidden_inputs(page.text) for page in (on_pause, on_failure))


async def test_a_post_that_cannot_be_honoured_changes_nothing_and_says_why(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, remote = world.app, world.remote_id
    theirs = await insert_rule(
        app, subsystem=HEALTH, selector=WORKER_SELECTOR, name="theirs", environment_id=remote
    )
    mine = await insert_rule(
        app,
        subsystem=HEALTH,
        selector=selector_json("container", "db"),
        name="mine",
        environment_id=remote,
        owner=STANDARD_ID,
    )
    stale = await insert_rule(
        app, subsystem=HEALTH, selector=WORKER_SELECTOR, name="stale", environment_id=remote
    )
    unreachable = await insert_rule(
        app, subsystem=HEALTH, selector=WORKER_SELECTOR, name="unreachable", environment_id=remote
    )
    mark = await last_event_id(app)
    cache, job = f"container:{CACHE}", f"container:{JOB}"
    reviewed = await add_data(admin_client, app, stale, cache, "cache")
    await execute(app, "UPDATE automation_rule SET updated_at = ? WHERE id = ?", (NEVER, stale))
    unsigned = {"rule_id": str(theirs), "object": cache, "expected_name": "cache"}

    statuses = {
        "no token": await admin_client.post(ADD_PATH, data=unsigned),
        "bad rule": await admin_client.post(
            ADD_PATH, data=csrf_form(admin_client, rule_id="abc", object=cache)
        ),
        "bad object": await admin_client.post(
            ADD_PATH, data=csrf_form(admin_client, rule_id=str(theirs), object="bogus")
        ),
        "absent object": await admin_client.post(
            ADD_PATH,
            data=csrf_form(admin_client, rule_id=str(theirs), object=f"container:{ABSENT}"),
        ),
        "hidden rule": await standard_client.post(
            ADD_PATH, data=await add_data(standard_client, app, theirs, cache, "cache")
        ),
        "unreadable object": await standard_client.post(
            ADD_PATH, data=await add_data(standard_client, app, mine, cache, "cache")
        ),
        "read-only object": await standard_client.post(
            ADD_PATH, data=await add_data(standard_client, app, mine, job, "job")
        ),
        "stale review": await admin_client.post(ADD_PATH, data=reviewed),
        "confirm bad rule": await admin_client.get(f"{ADD_PATH}?rule_id=0&object={cache}"),
        "confirm hidden rule": await standard_client.get(
            f"{ADD_PATH}?rule_id={theirs}&object={cache}"
        ),
    }
    with monkeypatch.context() as patched:

        async def down(client: object, name: str) -> None:
            raise EnvironmentUnavailable("unreachable")

        patched.setattr("app.subsystems.selectors.find_container", down)
        statuses["unreachable host"] = await admin_client.post(
            ADD_PATH, data=await add_data(admin_client, app, unreachable, cache, "cache")
        )

    assert {name: response.status_code for name, response in statuses.items()} == {
        "no token": FORBIDDEN,
        "bad rule": NOT_FOUND,
        "bad object": NOT_FOUND,
        "absent object": NOT_FOUND,
        "hidden rule": NOT_FOUND,
        "unreadable object": NOT_FOUND,
        "read-only object": FORBIDDEN,
        "stale review": UNPROCESSABLE,
        "confirm bad rule": NOT_FOUND,
        "confirm hidden rule": NOT_FOUND,
        "unreachable host": UNAVAILABLE,
    }
    assert "changed since you reviewed it" in visible_text(statuses["stale review"].text)
    assert "unreachable" in visible_text(statuses["unreachable host"].text)
    assert [
        await selector_names(app, rule_id) for rule_id in (theirs, mine, stale, unreachable)
    ] == [
        ["worker"],
        ["db"],
        ["worker"],
        ["worker"],
    ]
    recorded = await events(app, since=mark)
    assert [(row["action"], row["result"]) for row in recorded] == [(UPDATED, "failure")] * 2
    assert json.loads(recorded[0]["detail"])["reason"] == "changed_since_review"
    assert {row["actor_user_id"] for row in recorded} == {ADMIN_ID}


async def test_an_automation_with_no_handler_gets_no_confirm_form_and_a_refused_post(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    app, remote = world.app, world.remote_id
    rule_id = await insert_rule(
        app, subsystem=HEALTH, selector=WORKER_SELECTOR, name="beat", environment_id=remote
    )
    cache = f"container:{CACHE}"
    reviewed = await add_data(admin_client, app, rule_id, cache, "cache")
    withhold_handler(app, HEALTH)

    page = await admin_client.get(confirm_path(rule_id, cache))
    posted = await admin_client.post(ADD_PATH, data=reviewed)

    assert page.status_code == OK
    assert NOT_AVAILABLE in visible_text(page.text)
    assert "expected_digest" not in hidden_inputs(page.text)
    assert f'action="{ADD_PATH}"' not in page.text
    assert posted.status_code == UNPROCESSABLE
    assert NOT_AVAILABLE in visible_text(posted.text)
    assert await selector_names(app, rule_id) == ["worker"]


async def test_the_confirmation_says_how_many_held_containers_the_add_releases(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    app, remote = world.app, world.remote_id
    ids = {
        name: await insert_rule(
            app, subsystem=subsystem, selector=WORKER_SELECTOR, name=name, environment_id=remote
        )
        for name, subsystem in (
            ("one", HEALTH),
            ("two", HEALTH),
            ("none", HEALTH),
            ("restarter", RESTARTER),
        )
    }
    await hold_containers(app, ids["one"], remote, FIRST_HELD)
    await hold_containers(app, ids["two"], remote, FIRST_HELD, SECOND_HELD)
    await hold_containers(app, ids["restarter"], remote, FIRST_HELD)
    cache = f"container:{CACHE}"

    pages = {name: await admin_client.get(confirm_path(ids[name], cache)) for name in ids}

    texts = {name: visible_text(page.text) for name, page in pages.items()}
    assert ONE_RELEASED in texts["one"]
    assert TWO_RELEASED in texts["two"]
    assert RELEASE_LINE not in texts["none"]
    assert RELEASE_LINE not in texts["restarter"]


async def test_the_confirmation_warns_when_the_owner_cannot_change_the_object_and_says_who_can_act(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    app, remote = world.app, world.remote_id
    gone = await seed_standard_user(app, "gone-owner", enabled=False)
    await plant_control(app, remote, "container", WORKER, public=True, level="write")
    hook = await HookFactory(app).hook(
        remote, owner_id=STANDARD_ID, selector=selector_json("container", "job")
    )

    def owned(name: str, owner: int) -> Any:
        return insert_rule(
            app,
            subsystem=HEALTH,
            selector=WORKER_SELECTOR,
            name=name,
            environment_id=remote,
            owner=owner,
        )

    scenarios = {
        "write": (await owned("write", STANDARD_ID), DB),
        "read": (await owned("read", STANDARD_ID), JOB),
        "hidden": (await owned("hidden", STANDARD_ID), CACHE),
        "lost": (await owned("lost", gone), DB),
        "webhook": (hook.rule_id, WORKER),
    }

    texts = {
        name: visible_text((await admin_client.get(confirm_path(rule_id, f"container:{key}"))).text)
        for name, (rule_id, key) in scenarios.items()
    }

    assert STAYS_ENABLED in texts["write"]
    assert not any(w in texts["write"] for w in (READ_ONLY_OWNER, HIDDEN_FROM_OWNER, LOST_OWNER))
    assert READ_ONLY_OWNER in texts["read"]
    assert HIDDEN_FROM_OWNER in texts["hidden"]
    assert LOST_OWNER in texts["lost"]
    assert all(STAYS_ENABLED not in texts[name] for name in ("read", "hidden", "lost"))
    assert WEBHOOK_OPEN in texts["webhook"]
    assert STAYS_ENABLED not in texts["webhook"]


async def test_a_swarm_stack_owners_standing_follows_the_control_on_the_stack_name(
    admin_client: AsyncClient, swarm_stacks_world: World, swarm_host: SwarmEngine
) -> None:
    world = swarm_stacks_world
    app, remote = world.app, world.remote_id
    await create_shop(Browser(admin_client), world)
    rule_id = await insert_rule(
        app,
        subsystem=UPDATER,
        selector=selector_json("stack", "other"),
        name="redeploy",
        environment_id=remote,
        owner=STANDARD_ID,
    )
    shop = confirm_path(rule_id, f"swarm_stack:{SHOP}")

    administrators_only = await admin_client.get(shop)
    await execute(
        app,
        "UPDATE resource_control SET is_administrators_only = 0, is_public = 1, "
        "access_level = 'read' WHERE environment_id = ? AND resource_type = 'stack'",
        (remote,),
    )
    read_only = await admin_client.get(shop)
    await execute(app, "UPDATE resource_control SET access_level = 'write'")
    writable = await admin_client.get(shop)

    assert HIDDEN_SWARM_STACK in visible_text(administrators_only.text)
    assert READ_ONLY_SWARM_STACK in visible_text(read_only.text)
    writable_text = visible_text(writable.text)
    assert STAYS_ENABLED_STACK in writable_text
    assert not any(w in writable_text for w in (READ_ONLY_SWARM_STACK, HIDDEN_SWARM_STACK))


async def test_a_refusal_for_missing_access_reads_the_same_on_the_confirmation_and_on_the_post(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    app, remote, local = world.app, world.remote_id, world.local_id
    await plant_control(app, local, "container", DB, public=True, level="write")
    cannot_create = await insert_rule(
        app,
        subsystem=HEALTH,
        selector=WORKER_SELECTOR,
        name="local",
        environment_id=local,
        owner=STANDARD_ID,
    )
    cannot_write = await insert_rule(
        app,
        subsystem=HEALTH,
        selector=WORKER_SELECTOR,
        name="remote",
        environment_id=remote,
        owner=STANDARD_ID,
    )
    local_db, remote_job = f"container:{DB}", f"container:{JOB}"
    create_form = await add_data(standard_client, app, cannot_create, local_db, "db")
    write_form = await add_data(standard_client, app, cannot_write, remote_job, "job")

    create_page = await standard_client.get(confirm_path(cannot_create, local_db))
    write_page = await standard_client.get(confirm_path(cannot_write, remote_job))
    create_post = await standard_client.post(ADD_PATH, data=create_form)
    write_post = await standard_client.post(ADD_PATH, data=write_form)

    assert [page.status_code for page in (create_page, write_page)] == [OK, OK]
    assert [post.status_code for post in (create_post, write_post)] == [FORBIDDEN, FORBIDDEN]
    assert all(
        "expected_digest" not in hidden_inputs(page.text) for page in (create_page, write_page)
    )
    assert [NO_CREATE_ACCESS in visible_text(r.text) for r in (create_page, create_post)] == [
        True,
        True,
    ]
    assert [NO_WRITE_ACCESS in visible_text(r.text) for r in (write_page, write_post)] == [
        True,
        True,
    ]
    assert await selector_names(app, cannot_create) == ["worker"]
    assert await selector_names(app, cannot_write) == ["worker"]
