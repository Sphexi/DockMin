"""Who may open the Automation pages and what the per-object panel shows each reader."""

import re
from html import unescape

import pytest
from httpx import AsyncClient

from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.web.csrf import CSRF_FORM_FIELD
from tests.auth_support import ADMIN_ID, csrf_form
from tests.container_support import DB
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import rows
from tests.stack_support import stopped_stack_jobs  # noqa: F401
from tests.web.environment_support import World
from tests.web.flash_support import landed_texts
from tests.web.helpers import hidden_inputs, parse_tags, reviewed_run_data
from tests.web.test_automation_pages import (
    HEALTH,
    NO_STORE,
    OK,
    RESTARTER,
    SEE_OTHER,
    UNPROCESSABLE,
    create,
    enable_data,
    form_for,
    location,
)
from tests.web.test_stack_pages import compose_file, create_blog, stacks_path

CONFLICT = 409
FORBIDDEN = 403
NOT_FOUND = 404
OTHER_USERS_RULE = "An automation of another user"
PAUSE_PATHS = ("/enable", "/disable", "/pause", "/resume", "/run", "/dry-run", "/delete")
PAGE_PATHS = ("", "/edit", "/enable", "/run", "/delete", "/runs/1")


def container_page(world: World) -> str:
    return f"/environments/{world.remote_id}/containers/{DB}"


def automation_tab(world: World) -> str:
    return f"{container_page(world)}?tab=automation"


async def test_a_standard_user_may_aim_a_rule_only_at_a_host_they_may_change(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    chooser = await standard_client.get("/automation/rules/new")
    on_local = await standard_client.post(
        "/automation/rules",
        data=form_for(standard_client, world, environment_id=str(world.local_id)),
    )
    second_step = await standard_client.get(
        f"/automation/rules/new?subsystem={HEALTH}&environment_id={world.local_id}"
    )

    assert chooser.status_code == OK
    main = chooser.text.split("<main", 1)[1]
    assert "remote-box" in main and "local-box" not in main
    assert on_local.status_code == UNPROCESSABLE
    assert second_step.status_code == UNPROCESSABLE


async def test_a_user_sees_their_own_rules_and_an_administrator_sees_all_with_their_owners(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    mine = await create(standard_client, world, name="Mine")
    theirs = await create(admin_client, world, name="Admins <b>rule</b>")

    as_user = await standard_client.get("/automation")
    as_admin = await admin_client.get("/automation")
    owner_page = await admin_client.get(f"/automation/rules/{mine}")
    user_page = await standard_client.get(f"/automation/rules/{mine}")

    assert "Mine" in as_user.text and "Admins" not in as_user.text and "Owner" not in as_user.text
    assert "Mine" in as_admin.text and "Admins &lt;b&gt;rule&lt;/b&gt;" in as_admin.text
    assert "Owner" in as_admin.text and "standard-user" in as_admin.text
    assert "Open the job" in owner_page.text and "Open the job" not in user_page.text
    assert theirs != mine


@pytest.mark.parametrize("path", PAGE_PATHS)
async def test_another_users_rule_page_is_not_found_whatever_the_page(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
    path: str,
) -> None:
    theirs = await create(admin_client, world)

    response = await standard_client.get(f"/automation/rules/{theirs}{path}")

    assert response.status_code == NOT_FOUND
    assert response.headers["cache-control"] == NO_STORE


@pytest.mark.parametrize("path", PAUSE_PATHS)
async def test_another_users_rule_cannot_be_changed_from_any_action(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
    path: str,
) -> None:
    theirs = await create(admin_client, world)
    form = csrf_form(
        standard_client, duration="1h", expected_name="Watch db", typed_name="Watch db"
    )

    response = await standard_client.post(f"/automation/rules/{theirs}{path}", data=form)

    assert response.status_code == NOT_FOUND
    assert (await admin_client.get(f"/automation/rules/{theirs}")).status_code == OK


@pytest.mark.parametrize("path", [*PAUSE_PATHS, ""])
async def test_a_post_without_the_csrf_token_is_refused_and_changes_nothing(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine, path: str
) -> None:
    rule = await create(admin_client, world)
    bare = {"duration": "1h", "expected_name": "Watch db", "typed_name": "Watch db"}

    response = await admin_client.post(f"/automation/rules/{rule}{path}", data=bare)
    refused_create = await admin_client.post(
        "/automation/rules",
        data={
            k: v for k, v in form_for(admin_client, world, name="Z").items() if k != CSRF_FORM_FIELD
        },
    )

    assert (response.status_code, refused_create.status_code) == (FORBIDDEN, FORBIDDEN)
    still = await admin_client.get(f"/automation/rules/{rule}")
    assert still.status_code == OK and "Automation disabled" in still.text
    assert len(await rows(world.app, "SELECT id FROM automation_rule")) == 1


async def test_an_anonymous_visitor_is_not_let_into_any_automation_page(
    anonymous_client: AsyncClient,
) -> None:
    paths = ["/automation", "/automation/rules/new", "/automation/rules/1"]

    statuses = {p: (await anonymous_client.get(p)).status_code for p in paths}

    assert set(statuses.values()) <= {SEE_OTHER, 401}


async def test_the_navigation_offers_automation_to_every_signed_in_user(
    standard_client: AsyncClient, admin_client: AsyncClient
) -> None:
    pages = [(await c.get("/")).text for c in (standard_client, admin_client)]

    assert all('href="/automation"' in page for page in pages)
    assert 'href="/jobs"' not in pages[0]


async def test_the_rule_form_prefills_from_an_object_page_for_a_standard_user(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    page = await standard_client.get(
        f"/automation/rules/new?environment_id={world.remote_id}&target_kind=container&target=db"
    )

    assert page.status_code == OK
    assert f'value="{world.remote_id}" selected' in page.text
    assert hidden_inputs(page.text)["target"] == "db"


async def test_the_panel_names_a_rule_to_its_owner_and_administrators_and_hides_it_from_others(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    mine = await create(standard_client, world, name="Mine")
    theirs = await create(admin_client, world, name="Admins <b>rule</b>")
    await standard_client.post(
        f"/automation/rules/{mine}/enable", data=await enable_data(standard_client, mine)
    )

    as_user = await standard_client.get(automation_tab(world))
    as_admin = await admin_client.get(automation_tab(world))

    assert as_user.status_code == OK
    assert "Automation" in as_user.text and OTHER_USERS_RULE in as_user.text
    assert "Admins" not in as_user.text and f"/automation/rules/{theirs}" not in as_user.text
    assert f"/automation/rules/{mine}" in as_user.text and "Mine" in as_user.text
    assert "Add automation" in as_user.text
    assert f"container:{DB}" in as_user.text
    assert (
        "Admins &lt;b&gt;rule&lt;/b&gt;" in as_admin.text and OTHER_USERS_RULE not in as_admin.text
    )
    assert f"/automation/rules/{theirs}" in as_admin.text


async def test_the_panel_shows_a_reader_that_a_rule_acts_without_naming_the_rule_or_its_target(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    await create(admin_client, world, name="Secret name", target_kind="environment", target_name=[])

    page = await standard_client.get(automation_tab(world))

    assert OTHER_USERS_RULE in page.text
    assert "Secret name" not in page.text
    assert "Every container in the environment" not in page.text


async def test_a_panel_button_returns_to_the_object_and_nothing_else_is_followed(
    standard_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    rule = await create(standard_client, world, name="Mine")
    page = automation_tab(world)

    back = await standard_client.post(
        f"/automation/rules/{rule}/pause",
        data=csrf_form(standard_client, duration="1h", return_to=f"container:{DB}"),
    )
    offsite = await standard_client.post(
        f"/automation/rules/{rule}/resume",
        data=csrf_form(standard_client, return_to="https://evil.example/"),
    )
    offsite_notices = await landed_texts(standard_client, offsite)
    traversal = await standard_client.post(
        f"/automation/rules/{rule}/pause",
        data=csrf_form(standard_client, duration="1h", return_to="container:../x"),
    )
    traversal_notices = await landed_texts(standard_client, traversal)
    stack_of_nothing = await standard_client.post(
        f"/automation/rules/{rule}/pause",
        data=csrf_form(standard_client, duration="1h", return_to="stack:UPPER"),
    )
    stack_of_nothing_notices = await landed_texts(standard_client, stack_of_nothing)

    assert location(back) == page
    assert location(offsite) == f"/automation/rules/{rule}"
    assert offsite_notices == ["The automation is resumed."]
    assert location(traversal) == location(stack_of_nothing) == f"/automation/rules/{rule}"
    assert traversal_notices == stack_of_nothing_notices == ["The automation is paused."]


async def test_the_stack_page_shows_its_rules_and_a_panel_button_returns_to_it(
    admin_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    stopped_stack_jobs: None,  # noqa: F811
) -> None:
    await create_blog(admin_client, world, file=compose_file())
    rule = await create(
        admin_client,
        world,
        name="Restart blog",
        subsystem=RESTARTER,
        target_kind="stack",
        target_name="blog",
        schedule_expression="3600",
        param_max_restarts="",
        param_window_seconds="",
    )
    confirmation = await admin_client.get(f"/automation/rules/{rule}/enable")
    await admin_client.post(
        f"/automation/rules/{rule}/enable", data=await enable_data(admin_client, rule)
    )
    page = await admin_client.get(f"{stacks_path(world)}/blog?tab=automation")
    swapped = await admin_client.get(
        f"{stacks_path(world)}/blog?tab=file", headers={"HX-Request": "true"}
    )
    paused = await admin_client.post(
        f"/automation/rules/{rule}/pause",
        data=csrf_form(admin_client, duration="4h", return_to="stack:blog"),
    )

    assert "Restart blog" in page.text and "Stack blog" in page.text
    assert "Add automation" in page.text and "stack:blog" in page.text
    assert "stopped and then started" in confirmation.text.replace("\n", " ")
    assert "automation-panel" not in swapped.text
    assert location(paused) == f"{stacks_path(world)}/blog?tab=automation"


async def test_the_restarter_form_embeds_no_stack_names_and_its_suggestions_list_the_visible_ones(
    admin_client: AsyncClient,
    world: World,
    stack_host: ContainerEngine,
    stopped_stack_jobs: None,  # noqa: F811
) -> None:
    await create_blog(admin_client, world, file=compose_file())
    page = await admin_client.get(
        f"/automation/rules/new?subsystem={RESTARTER}&environment_id={world.remote_id}"
    )
    wrapper = next(
        tag for tag in parse_tags(page.text) if "suggest" in tag.attrs.get("class", "").split()
    )
    href = unescape(wrapper.attrs["hx-get"])

    suggested = await admin_client.get(
        f"{href}&target_name=bl&target_kind=stack&subsystem={RESTARTER}"
    )

    assert "blog" not in page.text
    assert href.startswith(f"/environments/{world.remote_id}/suggestions/rule-targets?")
    assert '<option value="blog">' in suggested.text


async def seed_runs(world: World, rule: int) -> dict[str, int]:
    """Finish one skipped run per kind of holder and leave one run in flight."""
    app, clock = world.app, world.app.state.auth.clock
    holders = {
        "admin": ("user", "admin-user"),
        "self": ("user", "standard-user"),
        "subsystem": ("subsystem", RESTARTER),
    }
    ids: dict[str, int] = {}
    async with app.state.database.session() as db:
        job = await ScheduledJobRepository(db, clock).get_for_rule(rule)
        assert job is not None
        runs = JobRunRepository(db, clock)
        for name, (kind, label) in holders.items():
            started = await runs.start(job.id, trigger="schedule", scheduled_for=None)
            await runs.finish(
                started.id,
                outcome="skipped",
                reason="object_busy",
                message="busy",
                detail={"holder_type": kind, "holder_label": label, "holder_action": "restart"},
            )
            ids[name] = started.id
    return ids


async def test_a_busy_holder_is_named_to_an_administrator_and_described_to_a_standard_viewer(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    rule = await create(standard_client, world)
    ids = await seed_runs(world, rule)

    def holder(page_text: str) -> str:
        found = re.search(r"holder_label</dt>\s*<dd[^>]*>(.*?)</dd>", page_text, re.S)
        assert found, page_text[:500]
        return re.sub(r"<[^>]+>", "", found.group(1)).strip()

    seen = {
        (who, name): holder((await client.get(f"/automation/rules/{rule}/runs/{run}")).text)
        for who, client in (("admin", admin_client), ("standard", standard_client))
        for name, run in ids.items()
    }

    assert seen == {
        ("admin", "admin"): "admin-user",
        ("admin", "self"): "standard-user",
        ("admin", "subsystem"): RESTARTER,
        ("standard", "admin"): "another user",
        ("standard", "self"): "you",
        ("standard", "subsystem"): RESTARTER,
    }


async def test_a_run_of_another_rule_is_not_found_through_this_rule(
    standard_client: AsyncClient,
    admin_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    mine = await create(standard_client, world, name="Mine")
    other = await create(standard_client, world, name="Other")
    ids = await seed_runs(world, other)

    response = await standard_client.get(f"/automation/rules/{mine}/runs/{ids['admin']}")

    assert response.status_code == NOT_FOUND


async def test_run_dry_run_and_delete_are_refused_while_a_run_is_in_flight(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    rule = await create(admin_client, world, name="Busy")
    await admin_client.post(
        f"/automation/rules/{rule}/enable", data=await enable_data(admin_client, rule)
    )
    reviewed = await reviewed_run_data(admin_client, f"/automation/rules/{rule}/run")
    app, clock = world.app, world.app.state.auth.clock
    async with app.state.database.session() as db:
        job = await ScheduledJobRepository(db, clock).get_for_rule(rule)
        assert job is not None
        await JobRunRepository(db, clock).start(
            job.id, trigger="schedule", scheduled_for=None, requested_by_user_id=ADMIN_ID
        )

    run = await admin_client.post(f"/automation/rules/{rule}/run", data=reviewed)
    dry = await admin_client.post(f"/automation/rules/{rule}/dry-run", data=csrf_form(admin_client))
    delete = await admin_client.post(
        f"/automation/rules/{rule}/delete",
        data=csrf_form(admin_client, expected_name="Busy", typed_name="Busy"),
    )
    page = await admin_client.get(f"/automation/rules/{rule}")

    assert (run.status_code, dry.status_code, delete.status_code) == (CONFLICT,) * 3
    assert "already running" in run.text and "This automation is running" in delete.text
    assert "Cannot run now" in page.text
