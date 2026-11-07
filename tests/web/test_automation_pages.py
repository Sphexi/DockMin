"""The Automation pages: creating, reviewing and changing a rule through script-free forms."""

import json
import re
from collections.abc import Sequence
from html import unescape
from typing import Any

import pytest
from httpx import AsyncClient, Response

from app.db.repositories.activity_export_cursor import ActivityExportCursorRepository
from app.db.repositories.job_run import JobRunRepository
from app.db.repositories.scheduled_job import ScheduledJobRepository
from app.services.object_inspector import EnvironmentUnavailable
from app.web.csrf import CSRF_COOKIE, CSRF_FORM_FIELD
from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import ADMIN, execute, job_services, rows
from tests.web.environment_support import World
from tests.web.flash_support import flash_texts, land, landed_texts
from tests.web.helpers import (
    field_error,
    hidden_inputs,
    inline_code_in,
    inputs_named,
    parse_tags,
    refresh_metas_in,
    reviewed_run_data,
    rule_binding,
    run_region_in,
    selected_option,
    value_of,
)

OK = 200
SEE_OTHER = 303
UNPROCESSABLE = 422
CONFLICT = 409
UNAVAILABLE = 503
HEALTH = "container-health-checker"
RESTARTER = "stack-restarter"
RULE_NAME = "Watch db"
HEALTH_INTERVAL = "300"
RESTARTER_INTERVAL = "3600"
NO_STORE = "no-store"
CRON_EXPRESSION = "0 3 * * *"
ZONE = "Europe/London"
INSTANCE_TARGET = "This instance"
PAUSED_WARNING = "Automation is paused after a restore."
NOTICE_CREATED = "The automation was created and is disabled. Run a dry run, then enable it."
NOTICE_UPDATED = "The automation is saved."
NOTICE_ENABLED = "The automation is enabled."
NOTICE_PAUSED = "The automation is paused."
NOTICE_RESUMED = "The automation is resumed."
NOTICE_DISABLED = "The automation is disabled."
NOTICE_DELETED = "The automation is deleted."


def location(response: Response) -> str:
    assert response.status_code == SEE_OTHER, (response.status_code, response.text[:1500])
    return response.headers["location"]


def rule_id_of(href: str) -> int:
    found = re.search(r"/automation/rules/(\d+)", href)
    assert found, href
    return int(found.group(1))


def form_for(client: AsyncClient, world: World, **changes: str | Sequence[str]) -> dict[str, Any]:
    data: dict[str, str | Sequence[str]] = {
        "subsystem": HEALTH,
        "environment_id": str(world.remote_id),
        "name": RULE_NAME,
        "target_kind": "container",
        "target_name": "db",
        "schedule_kind": "interval",
        "schedule_expression": "300",
        "time_zone": "UTC",
        "param_max_restarts": "2",
        "param_window_seconds": "600",
    }
    return csrf_form(client, **(data | changes))


async def create(client: AsyncClient, world: World, **changes: str | Sequence[str]) -> int:
    response = await client.post("/automation/rules", data=form_for(client, world, **changes))
    return rule_id_of(location(response))


async def enable_data(client: AsyncClient, rule_id: int) -> dict[str, str]:
    """Fill the enable form the way a browser would: from the confirmation page's hidden fields."""
    page = await client.get(f"/automation/rules/{rule_id}/enable")
    fields = {k: v for k, v in hidden_inputs(page.text).items() if k != CSRF_FORM_FIELD}
    return csrf_form(client, **fields)


def text_of(response: Response) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", response.text)).split())


@pytest.fixture
async def rule(admin_client: AsyncClient, world: World, shared_host: ContainerEngine) -> int:
    return await create(admin_client, world)


async def test_every_page_of_a_rule_renders_never_cached_and_without_inline_code(
    admin_client: AsyncClient, world: World, rule: int
) -> None:
    await admin_client.post(f"/automation/rules/{rule}/dry-run", data=csrf_form(admin_client))
    await world.app.state.scheduler.scheduler.idle()
    run_id = (await rows(world.app, "SELECT id FROM job_run ORDER BY id DESC LIMIT 1"))[0]["id"]
    pages = [
        "/automation",
        "/automation/rules/new",
        f"/automation/rules/new?subsystem={HEALTH}&environment_id={world.remote_id}",
        f"/automation/rules/{rule}",
        f"/automation/rules/{rule}/edit",
        f"/automation/rules/{rule}/enable",
        f"/automation/rules/{rule}/run",
        f"/automation/rules/{rule}/delete",
        f"/automation/rules/{rule}/runs/{run_id}",
    ]

    responses = {path: await admin_client.get(path) for path in pages}

    assert {p: r.status_code for p, r in responses.items()} == dict.fromkeys(pages, OK)
    assert {r.headers["cache-control"] for r in responses.values()} == {NO_STORE}
    assert {p: inline_code_in(r.text) for p, r in responses.items()} == {p: [] for p in pages}


async def test_every_form_carries_the_csrf_token_and_posts_to_this_site(
    admin_client: AsyncClient, world: World, rule: int
) -> None:
    token = admin_client.cookies[CSRF_COOKIE]
    pages = [
        f"/automation/rules/new?subsystem={HEALTH}&environment_id={world.remote_id}",
        f"/automation/rules/{rule}",
        f"/automation/rules/{rule}/edit",
        f"/automation/rules/{rule}/enable",
        f"/automation/rules/{rule}/delete",
    ]

    html = [(await admin_client.get(path)).text for path in pages]

    assert [hidden_inputs(page).get(CSRF_FORM_FIELD) for page in html] == [token] * len(pages)
    tags = [tag for page in html for tag in re.findall(r"<form\b[^>]*>", page)]
    assert tags
    assert all('method="post"' in tag.lower() and 'action="/' in tag for tag in tags)


async def test_the_first_step_offers_both_automations_and_the_second_prefills_a_default_schedule(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    first = await admin_client.get("/automation/rules/new")
    health = await admin_client.get(
        f"/automation/rules/new?subsystem={HEALTH}&environment_id={world.remote_id}"
    )
    restarter = await admin_client.get(
        f"/automation/rules/new?subsystem={RESTARTER}&environment_id={world.remote_id}"
    )

    assert HEALTH in first.text and RESTARTER in first.text
    assert (
        value_of(health.text, "schedule_kind"),
        value_of(health.text, "schedule_expression"),
    ) == (
        "interval",
        HEALTH_INTERVAL,
    )
    assert (
        selected_option(restarter.text, "schedule_kind"),
        value_of(restarter.text, "schedule_expression"),
    ) == ("interval", RESTARTER_INTERVAL)


@pytest.mark.parametrize(
    ("kind", "target", "field", "expected"),
    [
        ("container", "db", "target_name", "db"),
        ("stack", "shop", "target_name", "shop"),
    ],
)
async def test_an_object_page_can_prefill_the_target_of_a_new_rule(
    admin_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
    kind: str,
    target: str,
    field: str,
    expected: str,
) -> None:
    subsystem = RESTARTER if kind == "stack" else HEALTH

    page = await admin_client.get(
        f"/automation/rules/new?subsystem={subsystem}&environment_id={world.remote_id}"
        f"&target_kind={kind}&target={target}"
    )

    assert page.status_code == OK
    assert selected_option(page.text, "target_kind") == kind
    assert value_of(page.text, field) == expected


@pytest.mark.parametrize(
    "query",
    ["subsystem=nope&environment_id={remote}", "subsystem={health}&environment_id=999"],
)
async def test_a_first_step_choice_that_is_not_offered_is_refused(
    admin_client: AsyncClient, world: World, query: str
) -> None:
    path = "/automation/rules/new?" + query.format(remote=world.remote_id, health=HEALTH)

    response = await admin_client.get(path)

    assert response.status_code == UNPROCESSABLE


TARGET_KIND_FIELD = "field-target_kind"
RENDER_TARGETS = "/automation/rules/-/targets"
HX_REQUEST = {"HX-Request": "true"}
MOST_NAMES = 20
SORTED_NAMES = ["alpha", "bravo", "charlie", "delta", "echo"]
CANONICAL_NAMES = {"kind": "container", "names": SORTED_NAMES}
LABELS_WITH_A_NAME = (
    "Target name is not used when the automation acts on containers with labels. "
    "Clear it, or choose a target that takes names."
)
NAME_WITH_LABELS = (
    "Labels is only used when the automation acts on containers with labels. "
    "Clear it, or choose that target."
)
ENVIRONMENT_WITH_A_NAME = (
    "Target name is not used when the automation acts on every container in the environment. "
    "Clear it, or choose a target that takes names."
)


def typed_names(html: str) -> list[str]:
    return [tag.attrs.get("value", "") for tag in inputs_named(html, "target_name")]


def submit_buttons(html: str) -> list[dict[str, str]]:
    form = html[html.index('<form class="form create-form"') :]
    return [
        tag.attrs
        for tag in parse_tags(form)
        if tag.name == "button" and tag.attrs.get("type") == "submit"
    ]


async def test_the_target_section_has_one_name_field_and_saving_is_the_default_submit(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    page = await admin_client.get(
        f"/automation/rules/new?subsystem={HEALTH}&environment_id={world.remote_id}"
    )

    names = {tag.attrs["name"] for tag in parse_tags(page.text) if "name" in tag.attrs}
    assert typed_names(page.text) == [""]
    assert {"target_container", "target_stack", "target_service"}.isdisjoint(names)
    assert {"target_kind", "target_labels"} <= names
    first = submit_buttons(page.text)[0]
    assert "name" not in first
    assert [
        button["value"]
        for button in submit_buttons(page.text)
        if button.get("name") == "target_rows"
    ] == ["add"]


async def test_a_value_in_a_field_the_chosen_target_does_not_use_is_refused_naming_the_field(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    async def refusal(**changes: str | list[str]) -> tuple[int, str | None]:
        response = await admin_client.post(
            "/automation/rules", data=form_for(admin_client, world, **changes)
        )
        return response.status_code, field_error(response.text, TARGET_KIND_FIELD)

    labels = await refusal(target_kind="labels", target_labels="app=web")
    names = await refusal(target_kind="container", target_labels="app=web")
    environment = await refusal(target_kind="environment")

    assert labels == (UNPROCESSABLE, LABELS_WITH_A_NAME)
    assert names == (UNPROCESSABLE, NAME_WITH_LABELS)
    assert environment == (UNPROCESSABLE, ENVIRONMENT_WITH_A_NAME)
    assert await rows(world.app, "SELECT id FROM automation_rule") == []


async def test_target_rows_are_added_and_removed_without_script_and_nothing_is_saved(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine, rule: int
) -> None:
    async def posted(path: str, **changes: str | list[str]) -> list[str]:
        response = await admin_client.post(path, data=form_for(admin_client, world, **changes))
        assert response.status_code == OK
        return typed_names(response.text)

    before = await rows(world.app, "SELECT id, target_selector FROM automation_rule")
    added = await posted("/automation/rules", target_rows="add")
    removed = await posted(
        "/automation/rules", target_name=["db", "web", "cache"], target_rows="remove-1"
    )
    full = await posted(
        "/automation/rules",
        target_name=[f"c{number}" for number in range(MOST_NAMES)],
        target_rows="add",
    )
    on_edit = await posted(f"/automation/rules/{rule}", target_rows="add")

    assert added == ["db", ""]
    assert removed == ["db", "cache"]
    assert full == [f"c{number}" for number in range(MOST_NAMES)]
    assert on_edit == ["db", ""]
    assert await rows(world.app, "SELECT id, target_selector FROM automation_rule") == before


async def test_the_render_route_answers_the_rows_fragment_and_saves_nothing(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    async def fragment(**changes: str | list[str]) -> Response:
        return await admin_client.post(
            RENDER_TARGETS, data=form_for(admin_client, world, **changes), headers=HX_REQUEST
        )

    added = await fragment(target_name=["db", "web"], target_rows="add")
    removed = await fragment(target_name=["db", "web"], target_rows="remove-0")

    assert (added.status_code, removed.status_code) == (OK, OK)
    assert "<html" not in added.text and 'id="target-names"' in added.text
    assert typed_names(added.text) == ["db", "web", ""]
    assert typed_names(removed.text) == ["web"]
    assert await rows(world.app, "SELECT id FROM automation_rule") == []


async def test_several_names_are_saved_sorted_without_repeats_and_listed_in_full_on_the_pages(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    names = ["echo", "alpha", "delta", "charlie", "bravo", "alpha"]
    rule_id = await create(admin_client, world, target_name=names)

    stored = await rows(world.app, "SELECT target_selector FROM automation_rule")
    detail = text_of(await admin_client.get(f"/automation/rules/{rule_id}"))
    overview = text_of(await admin_client.get("/automation"))
    edit = await admin_client.get(f"/automation/rules/{rule_id}/edit")

    assert stored == [{"target_selector": json.dumps(CANONICAL_NAMES, separators=(",", ":"))}]
    assert "Containers alpha, bravo, charlie and 2 more alpha bravo charlie delta echo" in detail
    assert "Containers alpha, bravo, charlie and 2 more" in overview
    assert typed_names(edit.text) == SORTED_NAMES


async def test_the_run_page_lists_the_targets_their_outcomes_and_the_names_not_found(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    rule_id = await create(
        admin_client,
        world,
        subsystem=RESTARTER,
        schedule_expression=RESTARTER_INTERVAL,
        target_name=["db", "ghost"],
        param_max_restarts="",
        param_window_seconds="",
    )
    await admin_client.post(f"/automation/rules/{rule_id}/dry-run", data=csrf_form(admin_client))
    await world.app.state.scheduler.scheduler.idle()
    run_id = (await rows(world.app, "SELECT id FROM job_run ORDER BY id DESC LIMIT 1"))[0]["id"]

    text = text_of(await admin_client.get(f"/automation/rules/{rule_id}/runs/{run_id}"))

    assert "targets db" in text and "not_found_names ghost" in text
    assert "not_found 1" in text


async def test_a_rule_is_created_disabled_and_the_page_says_to_dry_run_it_first(
    admin_client: AsyncClient, rule: int
) -> None:
    page = await admin_client.get(f"/automation/rules/{rule}")

    assert page.status_code == OK
    assert flash_texts(page.text) == [NOTICE_CREATED]
    assert "Automation disabled" in page.text and RULE_NAME in page.text
    assert "Container db" in page.text
    assert "Run a dry run, then enable it." in unescape(page.text)


async def test_a_cron_rule_shows_its_time_zone_once(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    rule_id = await create(
        admin_client,
        world,
        subsystem=RESTARTER,
        schedule_kind="cron",
        schedule_expression=CRON_EXPRESSION,
        time_zone=ZONE,
    )

    page = await admin_client.get(f"/automation/rules/{rule_id}")

    assert text_of(page).count(f"{CRON_EXPRESSION} ({ZONE})") == 1
    assert f"({ZONE}) ({ZONE})" not in text_of(page)


async def test_a_rule_goes_through_edit_dry_run_enable_pause_run_disable_and_delete_without_script(
    admin_client: AsyncClient, world: World, rule: int
) -> None:
    client = admin_client
    edit = await client.get(f"/automation/rules/{rule}/edit")
    saved = await client.post(
        f"/automation/rules/{rule}",
        data=form_for(client, world, name="Watch db 2", param_max_restarts="4"),
    )
    saved_notices = await landed_texts(client, saved)
    dry = await client.post(f"/automation/rules/{rule}/dry-run", data=csrf_form(client))
    await world.app.state.scheduler.scheduler.idle()
    dry_page = await client.get(location(dry))
    bound = await rule_binding(world.app, rule)
    refused = await client.post(f"/automation/rules/{rule}/run", data=csrf_form(client, **bound))
    confirmation = await client.get(f"/automation/rules/{rule}/enable")
    enabled = await client.post(
        f"/automation/rules/{rule}/enable", data=await enable_data(client, rule)
    )
    enabled_notices = await landed_texts(client, enabled)
    paused = await client.post(
        f"/automation/rules/{rule}/pause", data=csrf_form(client, duration="1h")
    )
    paused_page = await land(client, paused)
    resumed = await client.post(f"/automation/rules/{rule}/resume", data=csrf_form(client))
    resumed_notices = await landed_texts(client, resumed)
    reviewed = await reviewed_run_data(client, f"/automation/rules/{rule}/run")
    started = await client.post(f"/automation/rules/{rule}/run", data=reviewed)
    await world.app.state.scheduler.scheduler.idle()
    disabled = await client.post(f"/automation/rules/{rule}/disable", data=csrf_form(client))
    disabled_notices = await landed_texts(client, disabled)
    delete_page = await client.get(f"/automation/rules/{rule}/delete")
    deleted = await client.post(
        f"/automation/rules/{rule}/delete",
        data=csrf_form(client, expected_name="Watch db 2", typed_name="Watch db 2"),
    )
    deleted_notices = await landed_texts(client, deleted)
    gone = await client.get(f"/automation/rules/{rule}")

    assert value_of(edit.text, "param_max_restarts") == "2"
    assert saved_notices == [NOTICE_UPDATED]
    assert (
        dry_page.status_code == OK
        and "Dry run: no container, stack or automation was changed" in dry_page.text
    )
    assert refused.status_code == CONFLICT and "The automation is disabled" in refused.text
    assert "Container db" in confirmation.text and "Dry run first" in confirmation.text
    assert enabled_notices == [NOTICE_ENABLED]
    assert flash_texts(paused_page.text) == [NOTICE_PAUSED]
    assert "Resume" in paused_page.text and "Paused until" in paused_page.text
    assert resumed_notices == [NOTICE_RESUMED]
    assert "/runs/" in location(started)
    assert disabled_notices == [NOTICE_DISABLED]
    assert "Type the automation" in delete_page.text
    assert deleted_notices == [NOTICE_DELETED]
    assert gone.status_code == 404


async def test_enabling_from_a_stale_review_is_refused_and_the_rule_stays_disabled(
    admin_client: AsyncClient, world: World, rule: int
) -> None:
    stale = await enable_data(admin_client, rule)
    async with world.app.state.database.session() as db:
        await db.execute(
            "UPDATE automation_rule SET updated_at = '2999-01-01T00:00:00.000Z' WHERE id = ?",
            (rule,),
        )

    response = await admin_client.post(f"/automation/rules/{rule}/enable", data=stale)

    assert response.status_code == UNPROCESSABLE
    assert "changed since you reviewed it" in text_of(response)
    assert "Automation disabled" in (await admin_client.get(f"/automation/rules/{rule}")).text


async def test_a_delete_needs_the_name_typed_and_the_name_the_page_showed(
    admin_client: AsyncClient, rule: int
) -> None:
    mistyped = await admin_client.post(
        f"/automation/rules/{rule}/delete",
        data=csrf_form(admin_client, expected_name=RULE_NAME, typed_name="nope"),
    )
    renamed = await admin_client.post(
        f"/automation/rules/{rule}/delete",
        data=csrf_form(admin_client, expected_name="Stale", typed_name="Stale"),
    )

    assert mistyped.status_code == UNPROCESSABLE and "Type the automation" in mistyped.text
    assert renamed.status_code == UNPROCESSABLE
    assert (await admin_client.get(f"/automation/rules/{rule}")).status_code == OK


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"name": ""}, "Use 1 to 64"),
        ({"schedule_expression": "5"}, ""),
        ({"param_max_restarts": "abc"}, "not valid"),
        ({"subsystem": RESTARTER, "schedule_expression": "60"}, ""),
        ({"target_kind": "labels", "target_labels": "", "target_name": []}, ""),
        ({"target_kind": "bogus"}, "Choose what the automation acts on"),
        (
            {
                "target_kind": "labels",
                "target_labels": "\n".join(f"k{n}" for n in range(9)),
                "target_name": [],
            },
            "at most 8 labels",
        ),
    ],
)
async def test_a_rule_that_is_not_valid_is_shown_the_form_again_with_the_reason(
    admin_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
    changes: dict[str, str | list[str]],
    fragment: str,
) -> None:
    response = await admin_client.post(
        "/automation/rules", data=form_for(admin_client, world, **changes)
    )

    assert response.status_code == UNPROCESSABLE
    assert fragment in response.text
    assert value_of(response.text, "name") is not None


async def test_labels_are_taken_one_per_line_with_or_without_a_value(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    rule = await create(
        admin_client,
        world,
        target_kind="labels",
        target_labels="app=web\ntier\n\n",
        target_name=[],
    )

    page = await admin_client.get(f"/automation/rules/{rule}")

    assert "Containers labelled app=web, tier" in page.text


async def test_a_host_that_cannot_say_whether_the_target_exists_shows_the_reason_and_saves_nothing(
    admin_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def down(client: object, name: str) -> None:
        raise EnvironmentUnavailable("unreachable")

    monkeypatch.setattr("app.subsystems.selectors.find_container", down)

    response = await admin_client.post("/automation/rules", data=form_for(admin_client, world))

    assert response.status_code == UNAVAILABLE
    assert "The environment is unreachable; the target could not be checked." in response.text
    assert await rows(world.app, "SELECT id FROM automation_rule") == []


async def test_names_targets_and_dry_run_detail_are_shown_escaped(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    rule = await create(
        admin_client,
        world,
        name="<script>x</script>",
        target_kind="labels",
        target_labels='"><img src=x onerror=1>=v',
        target_name=[],
    )

    pages = [
        await admin_client.get(f"/automation/rules/{rule}"),
        await admin_client.get(f"/automation/rules/{rule}/edit"),
        await admin_client.get("/automation"),
    ]

    for page in pages:
        assert "<script>x</script>" not in page.text
        assert "<img src=x" not in page.text
    assert "&lt;script&gt;x&lt;/script&gt;" in pages[0].text


async def test_the_overview_filters_pages_and_serves_fragments_to_htmx_only(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    await create(admin_client, world, name="One")
    await create(admin_client, world, name="Two")

    by_subsystem = await admin_client.get(f"/automation?subsystem={RESTARTER}")
    by_host = await admin_client.get(f"/automation?environment_id={world.remote_id}")
    unknown = await admin_client.get("/automation?subsystem=nope")
    fragment = await admin_client.get("/automation", headers={"HX-Request": "true"})
    boosted = await admin_client.get(
        "/automation", headers={"HX-Request": "true", "HX-Boosted": "true"}
    )
    restore = await admin_client.get(
        "/automation", headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}
    )
    paged = await admin_client.get("/automation?limit=1")

    assert "Nothing matches these filters" in by_subsystem.text
    assert "One" in by_host.text and "Two" in by_host.text
    assert unknown.status_code == UNPROCESSABLE
    assert "<html" not in fragment.text and 'id="rules-table"' in fragment.text
    assert "<html" in boosted.text and "<html" in restore.text
    assert 'rel="next"' in paged.text


async def test_a_run_history_pages_and_serves_a_fragment_to_htmx(
    admin_client: AsyncClient, world: World, rule: int
) -> None:
    for _ in range(2):
        await admin_client.post(f"/automation/rules/{rule}/dry-run", data=csrf_form(admin_client))
        await world.app.state.scheduler.scheduler.idle()

    paged = await admin_client.get(f"/automation/rules/{rule}?limit=1")
    fragment = await admin_client.get(f"/automation/rules/{rule}", headers={"HX-Request": "true"})

    assert 'rel="next"' in paged.text
    assert "<html" not in fragment.text and 'id="runs-table"' in fragment.text


@pytest.mark.parametrize(
    "path",
    [
        "/automation/rules/abc",
        f"/automation/rules/{'9' * 40}",
        "/automation/rules/999999",
        "/automation/rules/999999/runs/1",
        "/automation/rules/1/runs/abc",
    ],
)
async def test_an_id_that_names_no_rule_or_run_is_not_found(
    admin_client: AsyncClient, world: World, path: str
) -> None:
    response = await admin_client.get(path)

    assert response.status_code == 404
    assert response.headers["cache-control"] == NO_STORE


async def test_an_unrecognised_cursor_is_refused_rather_than_ignored(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/automation?cursor=garbage")

    assert response.status_code in {400, UNPROCESSABLE}


async def test_the_jobs_pages_link_a_rule_and_offer_its_dry_run(
    admin_client: AsyncClient, world: World, rule: int
) -> None:
    job_id = (await rows(world.app, "SELECT id FROM scheduled_job WHERE rule_id = ?", (rule,)))[0][
        "id"
    ]

    page = await admin_client.get(f"/jobs/{job_id}")
    ran = await admin_client.post(
        f"/jobs/{job_id}/run", data=csrf_form(admin_client, dry_run="yes")
    )
    await world.app.state.scheduler.scheduler.idle()
    run_page = await admin_client.get(location(ran))

    assert f'href="/automation/rules/{rule}"' in page.text
    assert "Cannot run now" in page.text and ">Dry run<" in page.text
    assert "Dry run: no container, stack or automation was changed" in run_page.text
    assert f'/automation/rules/{rule}"' in run_page.text


EXPORTER = "activity-log-exporter"
SYNCER = "backup-syncer"
EXPORTER_NAME = "Export <i>log</i>"
INSERT_TARGET = (
    "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
    "secret_access_key_ciphertext, use_path_style, verify_tls, ca_bundle, backup_prefix, "
    "activity_log_prefix, retention_count, created_at, updated_at) VALUES ('b', "
    "'http://127.0.0.1:29000', 'us-east-1', 'b', 'k', 'x', 1, 1, NULL, 'backups', 'logs', NULL, "
    "'x', 'x')"
)
RETENTION_DAYS = 30
EXTRA_EVENTS = 5
INSERT_EVENTS = (
    "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < ?) "
    "INSERT INTO activity_event (category, actor_type, target_type, action, result, occurred_at) "
    "SELECT 'authentication', 'anonymous', 'user', 'login_failed', 'failure', "
    "'2026-01-01T00:00:00Z' FROM n"
)


async def exporter_rule(world: World) -> int:
    async with world.app.state.database.session() as db:
        await db.execute(INSERT_TARGET)
    async with job_services(world.app) as services:
        rule = await services.rules.create(
            ADMIN,
            name=EXPORTER_NAME,
            subsystem=EXPORTER,
            environment_id=None,
            target_selector="",
            parameters={"object_storage_target_id": 1},
            schedule_kind="interval",
            schedule_expression="3600",
        )
    return rule.id


async def move_cursor(world: World, rule_id: int, last_event_id: int) -> None:
    async with world.app.state.database.session() as db, db.transaction():
        await ActivityExportCursorRepository(db, world.app.state.auth.clock).move_export_cursor(
            rule_id, last_event_id=last_event_id, object_key="k"
        )


async def total_events(world: World) -> int:
    return int((await rows(world.app, "SELECT count(*) AS n FROM activity_event"))[0]["n"])


async def delete_page_text(client: AsyncClient, rule_id: int) -> str:
    return text_of(await client.get(f"/automation/rules/{rule_id}/delete"))


async def test_the_run_confirmation_of_an_instance_wide_rule_names_this_instance(
    admin_client: AsyncClient, world: World
) -> None:
    rule_id = await exporter_rule(world)
    async with job_services(world.app) as services:
        await services.rules.enable(ADMIN, rule_id)

    page = await admin_client.get(f"/automation/rules/{rule_id}/run")

    assert page.status_code == OK
    assert INSTANCE_TARGET in text_of(page)
    assert "on this instance" in text_of(page)
    assert PAUSED_WARNING not in text_of(page)


async def test_the_run_confirmation_warns_while_automation_is_paused_after_a_restore(
    admin_client: AsyncClient, world: World, rule: int
) -> None:
    world.app.state.restore_gate.hold_automation()

    page = await admin_client.get(f"/automation/rules/{rule}/run")

    assert page.status_code == OK
    assert PAUSED_WARNING in text_of(page)
    assert INSTANCE_TARGET not in text_of(page)


async def test_the_chooser_offers_a_subsystem_that_takes_no_environment_to_administrators_only(
    admin_client: AsyncClient,
    standard_client: AsyncClient,
    world: World,
    shared_host: ContainerEngine,
) -> None:
    administrator = await admin_client.get("/automation/rules/new")
    standard = await standard_client.get("/automation/rules/new")
    forced = await standard_client.get(f"/automation/rules/new?subsystem={EXPORTER}")

    offered_to_administrator = re.findall(
        r'name="subsystem"[^>]*value="([^"]+)"', administrator.text
    )
    offered_to_standard = re.findall(r'name="subsystem"[^>]*value="([^"]+)"', standard.text)
    assert administrator.status_code == OK and standard.status_code == OK
    assert {HEALTH, RESTARTER, EXPORTER, SYNCER} <= set(offered_to_administrator)
    assert {HEALTH, RESTARTER} <= set(offered_to_standard)
    assert EXPORTER not in standard.text and SYNCER not in standard.text
    assert forced.status_code == UNPROCESSABLE


async def test_an_exporter_rules_delete_page_names_how_many_events_the_deletion_releases(
    admin_client: AsyncClient, world: World
) -> None:
    rule_id = await exporter_rule(world)
    await execute(world.app, INSERT_EVENTS, (EXTRA_EVENTS,))
    total = await total_events(world)
    initial = await delete_page_text(admin_client, rule_id)
    await move_cursor(world, rule_id, total - 1)
    one = await delete_page_text(admin_client, rule_id)
    await move_cursor(world, rule_id, total + 1_000)
    none = await delete_page_text(admin_client, rule_id)

    assert "Activity held for export" in initial
    assert f"{total} activity events are held back from retention" in initial
    assert "1 activity event is held back from retention" in one
    assert "holds no activity events back" in none


@pytest.mark.parametrize(
    ("days", "retention_sentence"), [(RETENTION_DAYS, True), (None, False)], ids=["on", "off"]
)
async def test_the_delete_page_mentions_retention_only_when_retention_is_on(
    admin_client: AsyncClient,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    days: int | None,
    retention_sentence: bool,
) -> None:
    rule_id = await exporter_rule(world)
    await move_cursor(world, rule_id, await total_events(world) - 1)
    settings = world.app.state.settings.model_copy(update={"activity_retention_days": days})
    monkeypatch.setattr(world.app.state, "settings", settings)

    text = await delete_page_text(admin_client, rule_id)

    assert "Deleting the automation ends the hold." in text
    assert ("Retention may then remove it." in text) is retention_sentence


async def test_a_rule_of_another_subsystem_has_no_hold_section_on_its_delete_page(
    admin_client: AsyncClient, rule: int
) -> None:
    page = await admin_client.get(f"/automation/rules/{rule}/delete")

    assert page.status_code == OK
    assert "Activity held for export" not in page.text


SYNCER_RULE = "Nightly backup"
TARGET_NAME = "primary-target"
INSERT_NAMED_TARGET = (
    "INSERT INTO object_storage_target (name, endpoint_url, region, bucket, access_key_id, "
    "secret_access_key_ciphertext, use_path_style, verify_tls, backup_prefix, "
    "activity_log_prefix, created_at, updated_at) VALUES (?, 'https://s3.example', "
    "'us-east-1', 'b', 'AK', 'v1:x', 1, 1, 'backups', 'logs', 'x', 'x')"
)
TARGET_FIELD = "param_object_storage_target_id"
TARGET_SELECT = re.compile(
    r'<select[^>]*name="param_object_storage_target_id"[^>]*>(.*?)</select>', re.DOTALL
)


def instance_form(client: AsyncClient, subsystem: str, **changes: str) -> dict[str, str]:
    base = {
        "subsystem": subsystem,
        "name": SYNCER_RULE,
        "schedule_kind": "interval",
        "schedule_expression": "3600",
        "time_zone": "UTC",
        TARGET_FIELD: "1",
    }
    return csrf_form(client, **(base | changes))


@pytest.fixture
async def target(world: World) -> int:
    await execute(world.app, INSERT_NAMED_TARGET, (TARGET_NAME,))
    return 1


async def test_the_second_step_of_an_environment_less_automation_has_a_target_select_only(
    admin_client: AsyncClient, target: int
) -> None:
    response = await admin_client.get(f"/automation/rules/new?subsystem={SYNCER}")

    html = response.text
    field_names = set(re.findall(r'name="([^"]+)"', html))
    select = TARGET_SELECT.search(html)
    assert response.status_code == OK
    assert "section-target-title" not in html and "environment_id" not in field_names
    assert select is not None and TARGET_NAME in select.group(1) and 'value="1"' in select.group(1)
    assert "param_suppression_seconds" not in html and "param_max_consecutive_failures" in html
    assert "overrun_policy" not in field_names
    assert value_of(html, "schedule_expression") == "3600"


@pytest.mark.parametrize("subsystem", [SYNCER, EXPORTER])
async def test_an_administrator_creates_an_environment_less_rule_that_names_its_target(
    admin_client: AsyncClient, target: int, subsystem: str
) -> None:
    response = await admin_client.post(
        "/automation/rules", data=instance_form(admin_client, subsystem)
    )

    assert response.status_code == SEE_OTHER
    detail = await admin_client.get(response.headers["location"].split("?")[0])
    text = text_of(detail)
    assert 'href="/object-storage/1"' in detail.text and TARGET_NAME in text
    assert "Unreadable target" not in text and "object_storage_target_id" not in text
    assert "not tied to an environment" in text


async def test_the_edit_form_selects_the_target_and_saving_it_redirects(
    admin_client: AsyncClient, target: int
) -> None:
    created = await admin_client.post("/automation/rules", data=instance_form(admin_client, SYNCER))
    rule_url = created.headers["location"].split("?")[0]

    edit = await admin_client.get(f"{rule_url}/edit")
    saved = await admin_client.post(
        rule_url,
        data=csrf_form(
            admin_client,
            name="Nightly backup 2",
            schedule_kind="interval",
            schedule_expression="7200",
            time_zone="UTC",
            **{TARGET_FIELD: "1"},
        ),
    )

    select = TARGET_SELECT.search(edit.text)
    assert select is not None and 'value="1" selected' in select.group(1)
    assert "section-target-title" not in edit.text
    assert saved.status_code == SEE_OTHER


@pytest.mark.parametrize(
    "changes", [{TARGET_FIELD: "77"}, {TARGET_FIELD: ""}], ids=["missing", "none"]
)
async def test_a_rule_aimed_at_no_target_or_a_deleted_one_is_refused_on_the_form(
    admin_client: AsyncClient, target: int, changes: dict[str, str]
) -> None:
    response = await admin_client.post(
        "/automation/rules", data=instance_form(admin_client, SYNCER, **changes)
    )

    assert response.status_code == UNPROCESSABLE


async def test_a_standard_user_cannot_create_an_environment_less_rule(
    standard_client: AsyncClient, world: World, target: int
) -> None:
    response = await standard_client.post(
        "/automation/rules", data=instance_form(standard_client, SYNCER)
    )

    assert response.status_code in (403, UNPROCESSABLE)
    assert await rows(world.app, "SELECT 1 FROM automation_rule") == []


async def test_the_chooser_describes_the_environment_less_automations_and_marks_them(
    admin_client: AsyncClient, target: int
) -> None:
    response = await admin_client.get("/automation/rules/new")

    html = response.text
    radios = {
        t.attrs["value"]: t.attrs
        for t in parse_tags(html)
        if t.name == "input" and t.attrs.get("name") == "subsystem"
    }
    assert radios[SYNCER].get("data-environment") == "none"
    assert "data-environment" not in radios[HEALTH]
    assert "not tied to an environment" in html and "gzip-compressed CSV" in html


async def test_a_rule_whose_target_was_deleted_says_so_and_points_to_the_edit_page(
    admin_client: AsyncClient, world: World, target: int
) -> None:
    created = await admin_client.post("/automation/rules", data=instance_form(admin_client, SYNCER))
    await execute(world.app, "DELETE FROM object_storage_target")

    detail = await admin_client.get(created.headers["location"].split("?")[0])

    assert "Deleted. Edit the automation to choose another target." in detail.text
    assert "Unreadable target" not in text_of(detail)


POLL_TRIGGER = "every 2s"
NOSCRIPT_REFRESH = "5"


async def unfinished_run(world: World, rule_id: int) -> int:
    clock = world.app.state.auth.clock
    async with world.app.state.database.session() as db:
        job = await ScheduledJobRepository(db, clock).get_for_rule(rule_id)
        assert job is not None
        run = await JobRunRepository(db, clock).start(
            job.id, trigger="schedule", scheduled_for=None
        )
    return run.id


async def test_an_unfinished_rule_run_polls_its_region_and_a_script_request_gets_only_the_region(
    admin_client: AsyncClient, world: World, rule: int
) -> None:
    path = f"/automation/rules/{rule}/runs/{await unfinished_run(world, rule)}"

    page = await admin_client.get(path)
    fragment = await admin_client.get(path, headers={"HX-Request": "true"})

    region = run_region_in(page.text)
    assert region is not None
    assert (region.attrs["hx-get"], region.attrs["hx-trigger"]) == (path, POLL_TRIGGER)
    assert [m.attrs["content"] for m in refresh_metas_in(page.text)] == [NOSCRIPT_REFRESH]
    assert "data-nojs-only" in page.text
    assert "<html" not in fragment.text
    assert run_region_in(fragment.text) is not None and "hx-trigger" in fragment.text


async def test_a_finished_rule_run_stops_polling_and_asks_for_no_reload(
    admin_client: AsyncClient, world: World, rule: int
) -> None:
    await admin_client.post(f"/automation/rules/{rule}/dry-run", data=csrf_form(admin_client))
    await world.app.state.scheduler.scheduler.idle()
    [run] = await rows(world.app, "SELECT MAX(id) AS id FROM job_run")
    path = f"/automation/rules/{rule}/runs/{run['id']}"

    page = await admin_client.get(path)
    fragment = await admin_client.get(path, headers={"HX-Request": "true"})

    for html in (page.text, fragment.text):
        region = run_region_in(html)
        assert region is not None
        assert region.attrs["aria-busy"] == "false"
        assert not {"hx-get", "hx-trigger"} & set(region.attrs)
    assert refresh_metas_in(page.text) == []
    assert "data-nojs-only" not in page.text
