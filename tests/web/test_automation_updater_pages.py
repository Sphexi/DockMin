"""The Automation pages for container-updater: parameters, confirmations, overview and run pages."""

import json
import re
from html import unescape
from typing import Any

import pytest
from httpx import AsyncClient, Response

from app.services.object_inspector import EnvironmentUnavailable
from app.subsystems.base import TargetPreview
from app.subsystems.container_updater import ContainerUpdater
from tests.auth_support import csrf_form
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import rows
from tests.web.environment_support import World
from tests.web.helpers import hidden_inputs, inline_code_in, parse_tags, selected_option
from tests.web.test_automation_pages import location, rule_id_of, typed_names

OK = 200
SEE_OTHER = 303
UNPROCESSABLE = 422
UPDATER = "container-updater"
INTERVAL = "900"
DIGEST_LENGTH = 64
UPDATE_MODE = [
    ("param_action", "update"),
    ("param_verify_seconds", "45"),
    ("param_require_healthy", "false"),
]
RUN_TIME = "2026-01-01T00:00:00Z"
HEALTH = "container-health-checker"
MANAGED_REFUSAL = (
    "db is managed by the Compose project shop. Choose the stack shop as the target instead."
)
RECREATE_SENTENCE = (
    "Matching containers are recreated from their own configuration with the newer image; "
    "containers that match later are included."
)
ROLLBACK_SENTENCE = "A container that fails verification is restored to its previous image."
KEEP_SENTENCE = "Nothing is removed except the container each update replaces. Old images are kept."
STACK_SENTENCE = (
    "The stack is redeployed from its deployed file; only containers whose image changed are "
    "recreated; a failed redeploy is not rolled back."
)


def visible(html: str) -> str:
    body = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S)
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", body)).split())


def form(client: AsyncClient, world: World, **changes: str) -> dict[str, Any]:
    data = {
        "subsystem": UPDATER,
        "environment_id": str(world.remote_id),
        "name": "Keep apps current",
        "target_kind": "labels",
        "target_labels": "role=app",
        "schedule_kind": "interval",
        "schedule_expression": INTERVAL,
        "time_zone": "UTC",
    }
    return csrf_form(client, **(data | changes))


def with_pairs(
    data: dict[str, str | list[str]], pairs: list[tuple[str, str]]
) -> dict[str, str | list[str]]:
    """Add form fields; a repeated name becomes a list, as a browser would send it."""
    merged = dict(data)
    for key, value in pairs:
        if key in merged:
            current = merged[key]
            merged[key] = [*(current if isinstance(current, list) else [current]), value]
        else:
            merged[key] = value
    return merged


def named(pairs: list[tuple[str, str]], *names: str) -> list[tuple[str, str]]:
    """Add one `target_name` field per name to the form pairs."""
    return [*pairs, *(("target_name", name) for name in names)]


class Maker:
    """Creates rules with a fresh name each time."""

    def __init__(self, client: AsyncClient, world: World) -> None:
        self.client = client
        self.world = world
        self.count = 0

    async def __call__(self, pairs: list[tuple[str, str]], **changes: str) -> int:
        self.count += 1
        changes.setdefault("name", f"Rule {self.count}")
        response = await self.client.post(
            "/automation/rules", data=with_pairs(form(self.client, self.world, **changes), pairs)
        )
        return rule_id_of(location(response))


@pytest.fixture
def make(admin_client: AsyncClient, world: World) -> Maker:
    return Maker(admin_client, world)


def radios(html: str) -> list[tuple[str, bool]]:
    return [
        (tag.attrs["value"], "checked" in tag.attrs)
        for tag in parse_tags(html)
        if tag.name == "input" and tag.attrs.get("name") == "param_action"
    ]


def checkboxes(html: str) -> list[bool]:
    return [
        "checked" in tag.attrs
        for tag in parse_tags(html)
        if tag.name == "input" and tag.attrs.get("type") == "checkbox"
    ]


async def enable(client: AsyncClient, rule_id: int) -> Response:
    """Review a rule and confirm enabling it with exactly what the review page carries."""
    page = await client.get(f"/automation/rules/{rule_id}/enable")
    fields = {k: v for k, v in hidden_inputs(page.text).items() if k != "csrf_token"}
    return await client.post(
        f"/automation/rules/{rule_id}/enable", data=csrf_form(client, **fields)
    )


async def add_run(world: World, rule_id: int, detail: dict[str, object]) -> int:
    job = (await rows(world.app, "SELECT id FROM scheduled_job WHERE rule_id = ?", (rule_id,)))[0]
    async with world.app.state.database.session() as db:
        await db.execute(
            "INSERT INTO job_run (scheduled_job_id, trigger, started_at, finished_at, outcome, "
            "detail) VALUES (?, 'manual', ?, ?, 'success', ?)",
            (job["id"], RUN_TIME, RUN_TIME, json.dumps(detail)),
        )
    return int((await rows(world.app, "SELECT max(id) AS id FROM job_run"))[0]["id"])


async def test_the_create_form_offers_the_modes_and_labels_every_field(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    page = await admin_client.get(
        f"/automation/rules/new?subsystem={UPDATER}&environment_id={world.remote_id}"
    )

    labels = re.findall(r'<label[^>]*for="([^"]+)"', page.text)
    assert page.status_code == OK
    assert radios(page.text) == [("report", True), ("update", False)]
    assert checkboxes(page.text) == [True, False]
    for needed in (
        "field-param_action-report",
        "field-param_action-update",
        "field-param_verify_seconds",
        "field-param_require_healthy-true",
    ):
        assert needed in labels
    assert "Watch each recreated container" in page.text
    assert inline_code_in(page.text) == []


async def test_the_parameters_round_trip_through_save_edit_and_a_browsers_checkbox_encoding(
    admin_client: AsyncClient, world: World, make: Maker
) -> None:
    rule_id = await make(UPDATE_MODE)
    stored = await rows(
        world.app, "SELECT parameters FROM automation_rule WHERE id = ?", (rule_id,)
    )
    edit = await admin_client.get(f"/automation/rules/{rule_id}/edit")
    browser_sends = with_pairs(
        form(admin_client, world),
        [
            ("param_action", "report"),
            ("param_verify_seconds", "10"),
            ("param_require_healthy", "false"),
            ("param_require_healthy", "true"),
        ],
    )
    for fixed in ("subsystem", "environment_id"):
        browser_sends.pop(fixed)
    saved = await admin_client.post(f"/automation/rules/{rule_id}", data=browser_sends)
    after = await rows(world.app, "SELECT parameters FROM automation_rule WHERE id = ?", (rule_id,))

    parameters = json.loads(stored[0]["parameters"])
    assert (parameters["action"], parameters["verify_seconds"], parameters["require_healthy"]) == (
        "update",
        45,
        False,
    )
    assert radios(edit.text) == [("report", False), ("update", True)]
    assert checkboxes(edit.text) == [False, False] and 'value="45"' in edit.text
    assert saved.status_code == SEE_OTHER
    assert json.loads(after[0]["parameters"])["require_healthy"] is True


@pytest.mark.parametrize(
    ("pairs", "shown"),
    [
        (
            [("param_action", "update"), ("param_verify_seconds", "999")],
            "These parameters are not valid: verify_seconds.",
        ),
        ([("param_action", "bogus")], "These parameters are not valid: action."),
    ],
    ids=["window-too-long", "unknown-mode"],
)
async def test_invalid_parameters_show_the_form_again_with_the_values_and_the_reason(
    admin_client: AsyncClient, world: World, pairs: list[tuple[str, str]], shown: str
) -> None:
    response = await admin_client.post(
        "/automation/rules", data=with_pairs(form(admin_client, world), pairs)
    )

    assert response.status_code == UNPROCESSABLE
    assert shown in visible(response.text)
    if "999" in str(pairs):
        assert 'value="999"' in response.text
        assert radios(response.text) == [("report", False), ("update", True)]


async def test_the_enable_confirmation_describes_what_each_mode_does_and_binds_a_digest(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine, make: Maker
) -> None:
    shared_host.add("a1" * 32, "app-one", image="nginx:1", labels={"role": "app"})
    shared_host.add("a2" * 32, "app-two", image="nginx:1", labels={"role": "app"})
    report = await make([("param_action", "report")])
    update = await make(UPDATE_MODE)
    stacked = await make(named(UPDATE_MODE, "shop"), target_kind="stack", target_labels="")

    reporting = await admin_client.get(f"/automation/rules/{report}/enable")
    updating = await admin_client.get(f"/automation/rules/{update}/enable")
    stack = await admin_client.get(f"/automation/rules/{stacked}/enable")

    report_text, update_text = visible(reporting.text), visible(updating.text)
    assert "app-one" in report_text and "app-two" in report_text
    assert "recreated from their own configuration" not in report_text
    assert len(hidden_inputs(reporting.text)["expected_digest"]) == DIGEST_LENGTH
    for sentence in (RECREATE_SENTENCE, ROLLBACK_SENTENCE, KEEP_SENTENCE):
        assert sentence in update_text
    assert "The stack is redeployed" not in update_text and "Dry run first" in update_text
    assert len(hidden_inputs(updating.text)["expected_digest"]) == DIGEST_LENGTH
    assert STACK_SENTENCE in visible(stack.text)
    assert inline_code_in(updating.text) == []


async def test_enabling_is_refused_on_a_wrong_digest_and_after_a_save_in_the_same_second(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine, make: Maker
) -> None:
    update = await make(UPDATE_MODE)
    report = await make([("param_action", "report")])
    page = await admin_client.get(f"/automation/rules/{update}/enable")
    fields = {k: v for k, v in hidden_inputs(page.text).items() if k != "csrf_token"}
    reviewed = await admin_client.get(f"/automation/rules/{report}/enable")
    reviewed_fields = {k: v for k, v in hidden_inputs(reviewed.text).items() if k != "csrf_token"}

    wrong = await admin_client.post(
        f"/automation/rules/{update}/enable",
        data=csrf_form(admin_client, **{**fields, "expected_digest": "0" * DIGEST_LENGTH}),
    )
    saved = with_pairs(form(admin_client, world, name="Rule 2"), [("param_action", "update")])
    saved = {k: v for k, v in saved.items() if k not in ("subsystem", "environment_id")}
    await admin_client.post(f"/automation/rules/{report}", data=saved)
    stale = await admin_client.post(
        f"/automation/rules/{report}/enable", data=csrf_form(admin_client, **reviewed_fields)
    )
    right = await enable(admin_client, update)

    assert wrong.status_code == UNPROCESSABLE
    assert "changed since you reviewed it" in visible(wrong.text)
    assert stale.status_code == UNPROCESSABLE
    assert right.status_code == SEE_OTHER


async def test_the_run_now_confirmation_offers_a_dry_run_instead_only_in_update_mode(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine, make: Maker
) -> None:
    update = await make(UPDATE_MODE)
    report = await make([])
    enabled = await enable(admin_client, update)

    updating = visible((await admin_client.get(f"/automation/rules/{update}/run")).text)
    reporting = visible((await admin_client.get(f"/automation/rules/{report}/run")).text)

    assert enabled.status_code == SEE_OTHER
    assert "recreated from their own configuration" in updating and "Dry run instead" in updating
    assert "recreated from their own configuration" not in reporting
    assert "Dry run instead" not in reporting


@pytest.mark.parametrize(
    ("preview", "expected", "absent"),
    [
        (
            TargetPreview(tuple(f"name-{i}" for i in range(20)), 3, "7 more are not listed."),
            ("name-19", "and 3 more.", "7 more are not listed."),
            (),
        ),
        (TargetPreview((), 0), ("Nothing matches the target now",), ()),
        (TargetPreview(("web",), 0), ("web",), ("and 0 more", "Nothing matches")),
    ],
    ids=["overflow", "nothing", "one"],
)
async def test_the_confirmation_lists_what_the_viewer_can_see_and_says_how_much_it_cannot(
    admin_client: AsyncClient,
    make: Maker,
    monkeypatch: pytest.MonkeyPatch,
    preview: TargetPreview,
    expected: tuple[str, ...],
    absent: tuple[str, ...],
) -> None:
    async def fixed(*arguments: object) -> TargetPreview:
        return preview

    monkeypatch.setattr(ContainerUpdater, "preview_targets", fixed)
    update = await make(UPDATE_MODE)

    text = visible((await admin_client.get(f"/automation/rules/{update}/enable")).text)

    for needle in expected:
        assert needle in text
    for needle in absent:
        assert needle not in text


async def test_an_unreachable_host_is_said_so_on_the_confirmation_not_reported_as_no_match(
    admin_client: AsyncClient, make: Maker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(*arguments: object) -> TargetPreview:
        raise EnvironmentUnavailable("unreachable")

    monkeypatch.setattr(ContainerUpdater, "preview_targets", unreachable)
    update = await make(UPDATE_MODE)

    text = visible((await admin_client.get(f"/automation/rules/{update}/enable")).text)

    assert "could not be listed" in text
    assert "matches nothing" not in text.lower() and "Nothing matches the target now" not in text


async def test_the_overview_summarises_the_last_run_of_each_rule_in_the_updates_column(
    admin_client: AsyncClient, world: World, make: Maker
) -> None:
    reported = await make([("param_action", "report")])
    updated = await make(UPDATE_MODE)
    only_unknown = await make([("param_action", "report")])
    nothing = await make([("param_action", "report")])
    await make([])
    never_run = visible((await admin_client.get("/automation")).text)
    await add_run(world, reported, {"mode": "report", "update_available": 3})
    await add_run(world, updated, {"mode": "update", "updated": 2, "rolled_back": 1, "unknown": 4})
    await add_run(world, only_unknown, {"mode": "report", "update_available": 0, "unknown": 2})
    await add_run(world, nothing, {"mode": "report", "update_available": 0, "unknown": 0})

    page = await admin_client.get("/automation")
    fragment = await admin_client.get("/automation", headers={"HX-Request": "true"})

    text = visible(page.text)
    assert "Updates" in page.text and "Not run yet" in never_run
    for summary in ("3 updates available", "2 updated", "1 rolled back", "4 unknown", "2 unknown"):
        assert summary in text
    assert text.count("None available") == 1
    assert "<html" not in fragment.text and "3 updates available" in fragment.text


async def test_the_run_page_lists_the_updaters_counts_and_names(
    admin_client: AsyncClient, world: World, make: Maker
) -> None:
    update = await make(UPDATE_MODE)
    detail = {
        "mode": "update",
        "matched": 3,
        "references": 2,
        "updated": 1,
        "rolled_back": 1,
        "would_pull": "a:1, b:2",
        "update_available_refs": "a:1 and 2 more",
    }
    run = await add_run(world, update, detail)

    text = visible((await admin_client.get(f"/automation/rules/{update}/runs/{run}")).text)

    assert "rolled_back" in text and "a:1 and 2 more" in text


async def test_a_compose_container_opens_the_updater_form_on_its_stack_and_other_forms_on_it(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    query = f"environment_id={world.remote_id}&target_kind=container&target=db&stack=shop"

    chooser = await admin_client.get(f"/automation/rules/new?{query}")
    updater = await admin_client.get(f"/automation/rules/new?subsystem={UPDATER}&{query}")
    health = await admin_client.get(f"/automation/rules/new?subsystem={HEALTH}&{query}")

    assert hidden_inputs(chooser.text)["stack"] == "shop"
    assert (selected_option(updater.text, "target_kind"), typed_names(updater.text)) == (
        "stack",
        ["shop"],
    )
    assert (selected_option(health.text, "target_kind"), typed_names(health.text)) == (
        "container",
        ["db"],
    )


async def test_a_compose_managed_container_is_refused_as_an_updater_target_and_its_stack_offered(
    admin_client: AsyncClient, world: World, shared_host: ContainerEngine
) -> None:
    data = with_pairs(
        form(admin_client, world, target_kind="container", target_labels=""),
        named(UPDATE_MODE, "db"),
    )

    response = await admin_client.post("/automation/rules", data=data)

    assert response.status_code == UNPROCESSABLE
    assert MANAGED_REFUSAL in visible(response.text)
    assert await rows(world.app, "SELECT id FROM automation_rule") == []


async def test_the_run_page_lists_the_targets_their_outcomes_and_what_acts_on_nothing(
    admin_client: AsyncClient, world: World, make: Maker
) -> None:
    update = await make(UPDATE_MODE)
    detail = {
        "mode": "update",
        "targets": "composed, web",
        "target_outcomes": "composed: skipped (compose_managed), web: done",
        "not_found": 1,
        "not_found_names": "ghost",
        "acts_on_nothing": "composed is managed by the Compose project shop",
    }
    run = await add_run(world, update, detail)

    text = visible((await admin_client.get(f"/automation/rules/{update}/runs/{run}")).text)

    for value in detail.values():
        assert str(value) in text
