"""Reading rules: the overview, one rule with its runs, and the rules acting on one object."""

import asyncio
import json

import pytest
from fastapi import FastAPI

from app.auth.errors import ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.config import Settings
from app.db.engine import DatabaseError
from app.db.repositories.automation_rule import AutomationRule
from app.db.repositories.environment_access import EnvironmentAccessRepository
from app.services.assembly import JobServices
from app.services.automation_rules import PANEL_RULES_SHOWN
from tests.auth_support import STANDARD_ID
from tests.conftest import AttachApp
from tests.job_support import (
    ADMIN,
    JOB_SUBSYSTEMS,
    RESTARTER,
    STANDARD,
    ScriptedRestarter,
    execute,
    job_services,
    rule_arguments,
)
from tests.web.environment_support import World, seed_world

OTHER_STANDARD = Principal(user_id=STANDARD_ID + 50, username="other", role="standard")
IDLE_SECONDS = 20
CONTAINER_SELECTOR = json.dumps({"kind": "container", "names": ["web"]})
LABELS_SELECTOR = json.dumps({"kind": "labels", "labels": [["app", "web"], ["tier", None]]})
ENVIRONMENT_SELECTOR = json.dumps({"kind": "environment"})
STACK_SELECTOR = json.dumps({"kind": "stack", "names": ["shop"]})
SERVICE_SELECTOR = json.dumps({"kind": "service", "names": ["web"]})
OTHER_SERVICE_SELECTOR = json.dumps({"kind": "service", "names": ["web-2"]})
BOTH_STACK_KINDS = frozenset({"compose", "swarm"})


@pytest.fixture
def app(settings: Settings, attach_app: AttachApp) -> FastAPI:
    return attach_app(settings, JOB_SUBSYSTEMS)


@pytest.fixture
async def seeded(started_app: FastAPI) -> World:
    world = await seed_world(started_app)
    async with started_app.state.database.session() as db:
        await EnvironmentAccessRepository(db).add_user("environment", world.remote_id, STANDARD_ID)
    return world


async def idle(app: FastAPI) -> None:
    async with asyncio.timeout(IDLE_SECONDS):
        await app.state.scheduler.scheduler.idle()


async def four_rules(services: JobServices, world: World) -> list[AutomationRule]:
    """A standard user's container rule and three administrator rules of other kinds."""
    remote = world.remote_id
    return [
        await services.rules.create(
            who, **rule_arguments(remote, name=name, target_selector=selector)
        )
        for who, name, selector in (
            (STANDARD, "a", CONTAINER_SELECTOR),
            (ADMIN, "b", LABELS_SELECTOR),
            (ADMIN, "c", ENVIRONMENT_SELECTOR),
            (ADMIN, "d", STACK_SELECTOR),
        )
    ]


async def test_the_overview_lists_a_users_own_rules_and_every_rule_to_an_administrator(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        a, b, c, d = await four_rules(services, seeded)
        await services.rules.enable(STANDARD, a.id)
        await services.rules.run_now(STANDARD, a.id, dry_run=True)
        await idle(started_app)
        await services.rules.run_now(STANDARD, a.id)
        await idle(started_app)

        mine = await services.rules.overview(STANDARD, limit=25)
        first_page = await services.rules.overview(ADMIN, limit=2)
        second_page = await services.rules.overview(
            ADMIN, limit=25, cursor=first_page.next_cursor, subsystem=RESTARTER
        )
        elsewhere = await services.rules.overview(ADMIN, limit=25, environment_id=seeded.local_id)

    assert [view.rule.id for view in mine.items] == [a.id]
    view = mine.items[0]
    assert (view.state, view.target, view.environment_name, view.owner) == (
        "scheduled",
        "Container web",
        "remote-box",
        None,
    )
    assert view.last_run is not None
    assert (view.last_run.trigger, view.last_run.outcome) == ("manual", "success")
    assert [run.trigger for run in view.recent] == ["manual", "dry_run"]
    assert view.last_dry_run_id is None
    assert [v.rule.id for v in first_page.items] == [a.id, b.id]
    assert first_page.next_cursor is not None
    assert [v.rule.id for v in second_page.items] == [c.id, d.id]
    assert first_page.items[0].owner == "standard-user"
    assert elsewhere.items == []


async def test_the_overview_refuses_an_unknown_subsystem_filter(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ValidationFailed):
            await services.rules.overview(ADMIN, limit=5, subsystem="nope")


async def test_one_rule_shows_its_last_dry_run_and_only_its_own_runs(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        a, b, *_ = await four_rules(services, seeded)
        await services.rules.enable(STANDARD, a.id)
        await services.rules.run_now(STANDARD, a.id, dry_run=True)
        await idle(started_app)
        await services.rules.run_now(STANDARD, a.id)
        await idle(started_app)

        detail = await services.rules.detail(STANDARD, a.id)
        runs = await services.rules.runs(STANDARD, a.id, limit=10)
        assert detail.last_dry_run_id is not None
        view, run = await services.rules.run(STANDARD, a.id, detail.last_dry_run_id)
        with pytest.raises(ResourceNotFound):
            await services.rules.run(ADMIN, b.id, detail.last_dry_run_id)
        with pytest.raises(ResourceNotFound):
            await services.rules.run(ADMIN, a.id, 0)

    assert [r.trigger for r in runs.items] == ["manual", "dry_run"]
    assert (run.trigger, view.rule.id) == ("dry_run", a.id)


async def test_every_view_pairs_a_rule_with_its_own_job_and_dry_run_and_a_rule_without_a_job_fails(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        a, b, c, d = await four_rules(services, seeded)
        await services.rules.run_now(STANDARD, a.id, dry_run=True)
        await idle(started_app)
        overview = await services.rules.overview(ADMIN, limit=25)
        detail_a = await services.rules.detail(ADMIN, a.id)
        detail_b = await services.rules.detail(ADMIN, b.id)
        runs_of_a = await services.rules.runs(ADMIN, a.id, limit=5)
        await execute(started_app, "DELETE FROM scheduled_job WHERE rule_id = ?", (c.id,))
        with pytest.raises(DatabaseError, match="no job"):
            await services.rules.overview(ADMIN, limit=25)
        with pytest.raises(DatabaseError, match="no job"):
            await services.rules.detail(ADMIN, c.id)
        with pytest.raises(DatabaseError, match="no job"):
            await services.rules.for_object(
                ADMIN, seeded.remote_id, name="web", labels={}, stack=None, kind="container"
            )

    assert [(v.rule.id, v.job.rule_id) for v in overview.items] == [
        (rule.id, rule.id) for rule in (a, b, c, d)
    ]
    assert len({v.job.id for v in overview.items}) == len(overview.items)
    [dry_run] = runs_of_a.items
    assert detail_a.last_dry_run_id == dry_run.id
    assert detail_b.last_dry_run_id is None


async def test_the_panel_names_a_rule_only_to_its_owner_and_to_administrators(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        a, *_ = await four_rules(services, seeded)
        await services.rules.enable(STANDARD, a.id)
        await services.rules.run_now(STANDARD, a.id, dry_run=True)
        await idle(started_app)
        await services.rules.run_now(STANDARD, a.id)
        await idle(started_app)

        as_owner = await services.rules.for_object(
            STANDARD,
            seeded.remote_id,
            name="web",
            labels={"app": "web", "tier": "x"},
            stack="shop",
            kind="container",
        )
        as_administrator = await services.rules.for_object(
            ADMIN, seeded.remote_id, name="web", labels={}, stack=None, kind="container"
        )

    assert (as_owner.more, as_owner.can_add, len(as_owner.rules)) == (0, True, 4)
    own = [p for p in as_owner.rules if p.manageable]
    others = [p for p in as_owner.rules if not p.manageable]
    assert [p.name for p in own] == ["a"]
    assert (own[0].rule_id, own[0].target, own[0].last_outcome) == (
        a.id,
        "Container web",
        "success",
    )
    assert len(others) == 3
    assert all(
        (p.name, p.target, p.rule_id, p.last_run_id) == (None, None, None, None) for p in others
    )
    assert all(p.subsystem == RESTARTER and p.schedule for p in others)
    assert sorted(p.name or "" for p in as_administrator.rules) == ["a", "c"]


async def test_the_panel_matches_a_stack_by_its_name_whatever_the_case_and_by_the_whole_environment(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        await four_rules(services, seeded)

        panel = await services.rules.for_object(
            ADMIN, seeded.remote_id, name="SHOP", labels={}, stack=None, kind="stack"
        )

    assert sorted(p.name or "" for p in panel.rules) == ["c", "d"]


async def test_the_panel_shows_ten_rules_and_counts_the_rest(
    started_app: FastAPI, seeded: World
) -> None:
    extra = 3
    async with job_services(started_app) as services:
        for number in range(PANEL_RULES_SHOWN + extra):
            await services.rules.create(
                ADMIN,
                **rule_arguments(
                    seeded.remote_id, name=f"r{number:02d}", target_selector=ENVIRONMENT_SELECTOR
                ),
            )

        panel = await services.rules.for_object(
            ADMIN, seeded.remote_id, name="web", labels={}, stack=None, kind="container"
        )

    assert (len(panel.rules), panel.more) == (PANEL_RULES_SHOWN, extra)


async def test_the_panel_is_not_found_for_a_user_without_access_to_the_environment(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        with pytest.raises(ResourceNotFound):
            await services.rules.for_object(
                OTHER_STANDARD,
                seeded.remote_id,
                name="web",
                labels={},
                stack=None,
                kind="container",
            )


async def test_the_service_panel_lists_rules_naming_the_service_or_its_stack_and_no_others(
    started_app: FastAPI, seeded: World
) -> None:
    selectors = {
        "by-name": SERVICE_SELECTOR,
        "other-service": OTHER_SERVICE_SELECTOR,
        "container": CONTAINER_SELECTOR,
        "labels": LABELS_SELECTOR,
        "environment": ENVIRONMENT_SELECTOR,
        "its-stack": STACK_SELECTOR,
    }
    async with job_services(started_app) as services:
        for name, selector in selectors.items():
            await services.rules.create(
                ADMIN,
                **rule_arguments(seeded.remote_id, name=name, target_selector=selector),
            )

        in_stack = await services.rules.for_object(
            ADMIN, seeded.remote_id, name="web", labels={}, stack="Shop", kind="service"
        )
        alone = await services.rules.for_object(
            ADMIN, seeded.remote_id, name="web", labels={}, stack=None, kind="service"
        )

    assert sorted(p.name or "" for p in in_stack.rules) == ["by-name", "its-stack"]
    assert sorted(p.name or "" for p in alone.rules) == ["by-name"]


async def test_the_swarm_stack_panel_lists_only_stack_rules_of_automations_acting_on_swarm_stacks(
    started_app: FastAPI, seeded: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with job_services(started_app) as services:
        for name, selector in (("stack", STACK_SELECTOR), ("environment", ENVIRONMENT_SELECTOR)):
            await services.rules.create(
                ADMIN,
                **rule_arguments(seeded.remote_id, name=name, target_selector=selector),
            )

        compose = await services.rules.for_object(
            ADMIN, seeded.remote_id, name="shop", labels={}, stack=None, kind="stack"
        )
        compose_only = await services.rules.for_object(
            ADMIN, seeded.remote_id, name="shop", labels={}, stack=None, kind="swarm_stack"
        )
        monkeypatch.setattr(ScriptedRestarter, "stack_kinds", BOTH_STACK_KINDS)
        both = await services.rules.for_object(
            ADMIN, seeded.remote_id, name="shop", labels={}, stack=None, kind="swarm_stack"
        )

    assert sorted(p.name or "" for p in compose.rules) == ["environment", "stack"]
    assert compose_only.rules == ()
    assert sorted(p.name or "" for p in both.rules) == ["stack"]


async def test_a_viewer_sees_another_users_service_rule_without_its_name_target_or_id(
    started_app: FastAPI, seeded: World
) -> None:
    async with job_services(started_app) as services:
        await services.rules.create(
            ADMIN,
            **rule_arguments(seeded.remote_id, name="Secret", target_selector=SERVICE_SELECTOR),
        )

        panel = await services.rules.for_object(
            STANDARD, seeded.remote_id, name="web", labels={}, stack=None, kind="service"
        )

    [shown] = panel.rules
    assert (shown.manageable, shown.rule_id, shown.name, shown.target) == (False, None, None, None)
    assert shown.subsystem == RESTARTER
