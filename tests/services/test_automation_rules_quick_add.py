"""Adding an object to an enabled automation: the offer, the review and the confirmed write."""

import json
from typing import Any

import pytest
from fastapi import FastAPI

from app.auth.errors import PermissionDenied, ResourceNotFound, ValidationFailed
from app.auth.principal import Principal
from app.services.access import COMPOSE_PROJECT_LABEL
from app.services.assembly import JobServices
from app.services.authorisation import Authoriser
from app.services.automation_quick_add import QuickAddObject
from app.services.automation_rules import (
    CHANGED_BEFORE_ADD,
    NO_WRITE_ACCESS,
    OBJECT_RENAMED,
    PRUNE_NEEDS_ADMINISTRATOR,
    PanelRules,
)
from app.services.container_derivation import SWARM_SERVICE_LABEL
from app.subsystems.base import Subsystem
from tests.access_support import service_id
from tests.auth_support import ADMIN_ID, STANDARD_ID
from tests.container_support import CACHE, DB, JOB, STACK, WORKER
from tests.docker_engine.container_engine import ContainerEngine
from tests.job_support import (
    ADMIN,
    STANDARD,
    events,
    execute,
    job_of_rule,
    job_services_on_hosts,
    last_event_id,
    rows,
)
from tests.quick_add_support import (
    EXPORTER,
    HEALTH,
    NEVER,
    RESTARTER,
    UPDATER,
    age_rule,
    insert_rule,
    object_of,
    review,
    seed_compose_stack,
    seed_standard_user,
    selector_json,
)
from tests.web.environment_support import World

UPDATED = "automation_rule_updated"
FULL_NAMES = [f"name{number:02d}" for number in range(20)]
TASK_SERVICE = service_id(1)
LABELLED = {COMPOSE_PROJECT_LABEL: STACK}
WORKER_SELECTOR = selector_json("container", "worker")
JOB_SELECTOR = selector_json("container", "job")
RACING_SELECTOR = selector_json("container", "job", "worker")
UPDATE_AND_PRUNE = '{"action": "update", "prune_replaced": true}'


async def panel_for(
    services: JobServices, viewer: Principal, obj: QuickAddObject, *, stack: str | None = None
) -> PanelRules:
    return await services.rules.for_object(
        viewer,
        obj.environment_id,
        name=obj.name,
        labels=obj.labels,
        stack=stack,
        kind=obj.kind,
        key=obj.key,
    )


async def seed(
    app: FastAPI, name: str, subsystem: str, selector: str, environment_id: int, **changes: Any
) -> int:
    return await insert_rule(
        app,
        subsystem=subsystem,
        selector=selector,
        name=name,
        environment_id=environment_id,
        **changes,
    )


async def refusal_of(
    app: FastAPI, services: JobServices, rule_id: int, obj: QuickAddObject
) -> tuple[str, bool]:
    """Return the reason a confirmed add is refused for, and whether its review showed a refusal."""
    reviewed = await review(services, ADMIN, rule_id, obj)
    preview = await services.rules.quick_add_preview(ADMIN, rule_id, obj)
    mark = await last_event_id(app)
    with pytest.raises(ValidationFailed):
        await services.rules.quick_add(ADMIN, rule_id, obj, **reviewed)
    return await failure_reason(app, mark), preview.refusal is not None and preview.target is None


def offered(panel: PanelRules) -> list[int]:
    return [option.rule_id for option in panel.quick_add]


async def failure_reason(app: FastAPI, mark: int) -> str:
    [failure] = await events(app, since=mark)
    assert (failure["action"], failure["result"]) == (UPDATED, "failure")
    return str(json.loads(failure["detail"])["reason"])


async def test_the_offer_for_an_administrator_lists_what_can_take_the_container_and_the_task(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    remote, local = world.remote_id, world.local_id
    ids = {
        "health": await seed(started_app, "health", HEALTH, WORKER_SELECTOR, remote),
        "users": await seed(
            started_app, "users", UPDATER, WORKER_SELECTOR, remote, owner=STANDARD_ID
        ),
        "export": await seed(started_app, "export", EXPORTER, WORKER_SELECTOR, remote),
        "paused": await seed(
            started_app, "paused", HEALTH, JOB_SELECTOR, remote, paused_until=NEVER
        ),
        "failing": await seed(
            started_app, "failing", RESTARTER, JOB_SELECTOR, remote, job_disabled=True
        ),
        "off": await seed(started_app, "off", HEALTH, WORKER_SELECTOR, remote, enabled=False),
        "elsewhere": await seed(started_app, "elsewhere", HEALTH, WORKER_SELECTOR, local),
        "holds": await seed(
            started_app, "holds", HEALTH, selector_json("container", "cache"), remote
        ),
        "full": await seed(
            started_app, "full", HEALTH, selector_json("container", *FULL_NAMES), remote
        ),
        "labels": await seed(started_app, "labels", HEALTH, selector_json("labels"), remote),
        "all": await seed(started_app, "all", HEALTH, selector_json("environment"), remote),
        "stack": await seed(
            started_app, "stack", RESTARTER, selector_json("stack", "shop"), remote
        ),
        "service": await seed(
            started_app, "service", UPDATER, selector_json("service", "web"), remote
        ),
    }
    cache = object_of(remote, "container", CACHE, "cache")
    task = object_of(remote, "container", WORKER, "task", {SWARM_SERVICE_LABEL: TASK_SERVICE})
    requests_before = len(shared_host.requests)

    async with job_services_on_hosts(started_app) as services:
        plain = await panel_for(services, ADMIN, cache)
        on_task = await panel_for(services, ADMIN, task)

    assert offered(plain) == [
        ids[name] for name in ("health", "users", "export", "paused", "failing")
    ]
    assert plain.can_add
    assert [(o.name, o.subsystem, o.target) for o in plain.quick_add][:2] == [
        ("health", HEALTH, "Container worker"),
        ("users", UPDATER, "Container worker"),
    ]
    assert offered(on_task) == []
    assert on_task.quick_add_help.swarm_service_id == TASK_SERVICE
    assert len(shared_host.requests) == requests_before


async def test_the_offer_for_a_standard_user_is_their_own_automations_and_names_the_stack_instead(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    remote = world.remote_id
    await seed_compose_stack(started_app, remote, STACK)
    mine = {
        "health": await seed(
            started_app, "health", HEALTH, JOB_SELECTOR, remote, owner=STANDARD_ID
        ),
        "restart": await seed(
            started_app, "restart", RESTARTER, JOB_SELECTOR, remote, owner=STANDARD_ID
        ),
        "update": await seed(
            started_app, "update", UPDATER, JOB_SELECTOR, remote, owner=STANDARD_ID
        ),
        "export": await seed(
            started_app, "export", EXPORTER, JOB_SELECTOR, remote, owner=STANDARD_ID
        ),
        "off": await seed(
            started_app, "off", HEALTH, JOB_SELECTOR, remote, owner=STANDARD_ID, enabled=False
        ),
        "theirs": await seed(started_app, "theirs", HEALTH, JOB_SELECTOR, remote),
    }
    db = object_of(remote, "container", DB, "db", LABELLED)

    async with job_services_on_hosts(started_app) as services:
        panel = await panel_for(services, STANDARD, db, stack=STACK)

    assert offered(panel) == [mine["health"], mine["restart"]]
    assert panel.quick_add_help.compose_left_out
    assert (panel.quick_add_help.compose_project, panel.quick_add_help.compose_stack) == (
        STACK,
        STACK,
    )


async def test_no_offer_is_made_without_create_access_write_access_or_a_matching_automation(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    remote, local = world.remote_id, world.local_id
    await seed(started_app, "on-remote", HEALTH, WORKER_SELECTOR, remote, owner=STANDARD_ID)
    await seed(started_app, "on-local", HEALTH, WORKER_SELECTOR, local, owner=STANDARD_ID)
    await seed(started_app, "by-label", HEALTH, selector_json("labels"), remote)
    on_local = object_of(local, "container", DB, "db", LABELLED)
    ungranted = object_of(remote, "container", CACHE, "cache")
    read_only = object_of(remote, "container", JOB, "job")
    unmatched = object_of(remote, "service", service_id(7), "web")

    async with job_services_on_hosts(started_app) as services:
        local_panel = await panel_for(services, STANDARD, on_local)
        ungranted_panel = await panel_for(services, STANDARD, ungranted)
        read_only_panel = await panel_for(services, STANDARD, read_only)
        unmatched_panel = await panel_for(services, ADMIN, unmatched)

    assert (local_panel.can_add, offered(local_panel)) == (False, [])
    assert offered(ungranted_panel) == []
    assert offered(read_only_panel) == []
    assert (unmatched_panel.can_add, offered(unmatched_panel)) == (True, [])


async def test_a_confirmed_add_appends_one_name_and_leaves_the_automation_running(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    rule_id = await seed(
        started_app,
        "health",
        HEALTH,
        WORKER_SELECTOR,
        world.remote_id,
        owner=STANDARD_ID,
        paused_until=NEVER,
    )
    await age_rule(started_app, rule_id)
    failing = await seed(
        started_app, "failing", RESTARTER, JOB_SELECTOR, world.remote_id, job_disabled=True
    )
    cache = object_of(world.remote_id, "container", CACHE, "cache")
    job_before = await job_of_rule(started_app, rule_id)

    async with job_services_on_hosts(started_app) as services:
        preview = await services.rules.quick_add_preview(ADMIN, rule_id, cache)
        after_failures = await services.rules.quick_add_preview(ADMIN, failing, cache)
        mark = await last_event_id(started_app)
        saved = await services.rules.quick_add(
            ADMIN, rule_id, cache, **await review(services, ADMIN, rule_id, cache)
        )

    assert preview.refusal is None and after_failures.refusal is None
    assert (preview.view.state, after_failures.view.state) == ("paused", "auto_disabled")
    assert (preview.names_before, preview.names_after) == (("worker",), ("cache", "worker"))
    assert preview.owner_is_other
    assert json.loads(saved.target_selector) == {"kind": "container", "names": ["cache", "worker"]}
    assert (saved.is_enabled, saved.paused_until) == (True, NEVER)
    assert saved.name == "health" and saved.parameters == "{}"
    assert saved.updated_at != preview.view.rule.updated_at
    assert await job_of_rule(started_app, rule_id) == job_before
    [recorded] = await events(started_app, since=mark)
    assert (recorded["action"], recorded["result"], recorded["actor_type"]) == (
        UPDATED,
        "success",
        "user",
    )
    assert recorded["actor_user_id"] == ADMIN_ID
    assert recorded["target_id"] == str(rule_id)
    assert recorded["environment_id"] == world.remote_id
    detail: dict[str, Any] = json.loads(recorded["detail"])
    assert detail["target_changed"] is True
    assert detail["target_added"] == "cache"
    assert [detail[key] for key in ("name_changed", "parameters_changed", "schedule_changed")] == [
        False,
        False,
        False,
    ]
    assert detail["overrun_changed"] is False
    assert "rule_disabled" not in detail


async def test_an_add_is_refused_after_the_automation_or_the_object_changed_since_the_review(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    rule_id = await seed(started_app, "health", HEALTH, WORKER_SELECTOR, world.remote_id)
    await age_rule(started_app, rule_id)
    cache = object_of(world.remote_id, "container", CACHE, "cache")
    renamed = object_of(world.remote_id, "container", CACHE, "renamed")
    selector_before = (await rows(started_app, "SELECT target_selector FROM automation_rule"))[0]
    edit: dict[str, Any] = {
        "name": "Renamed",
        "target_selector": selector_json("container", "worker"),
        "parameters": {},
        "schedule_kind": "interval",
        "schedule_expression": "3600",
        "time_zone": "UTC",
        "overrun_policy": None,
    }

    async with job_services_on_hosts(started_app) as services:
        reviewed = await review(services, ADMIN, rule_id, cache)
        mark = await last_event_id(started_app)
        await services.rules.pause(ADMIN, rule_id, duration="1h")
        with pytest.raises(ValidationFailed) as paused:
            await services.rules.quick_add(ADMIN, rule_id, cache, **reviewed)
        await services.rules.resume(ADMIN, rule_id)
        with pytest.raises(ValidationFailed) as resumed:
            await services.rules.quick_add(ADMIN, rule_id, cache, **reviewed)
        await services.rules.update(ADMIN, rule_id, **edit)
        with pytest.raises(ValidationFailed) as saved:
            await services.rules.quick_add(ADMIN, rule_id, cache, **reviewed)
        await services.rules.disable(ADMIN, rule_id)
        with pytest.raises(ValidationFailed) as disabled:
            await services.rules.quick_add(ADMIN, rule_id, cache, **reviewed)
        await services.rules.enable(ADMIN, rule_id)
        fresh = await review(services, ADMIN, rule_id, cache)
        with pytest.raises(ValidationFailed) as rename:
            await services.rules.quick_add(ADMIN, rule_id, renamed, **fresh)
        after = await rows(started_app, "SELECT target_selector FROM automation_rule")

    stale = {"rule": CHANGED_BEFORE_ADD}
    assert [e.value.fields for e in (paused, resumed, saved, disabled)] == [stale] * 4
    assert rename.value.fields == {"object": OBJECT_RENAMED}
    failures = [e for e in await events(started_app, since=mark) if e["result"] == "failure"]
    reasons = [json.loads(failure["detail"])["reason"] for failure in failures]
    assert [failure["action"] for failure in failures] == [UPDATED] * 5
    assert reasons == ["changed_since_review"] * 5
    added = [json.loads(failure["detail"])["target_added"] for failure in failures]
    assert added == ["cache"] * 4 + ["renamed"]
    assert after == [selector_before]


async def test_an_add_is_refused_when_the_automation_no_longer_qualifies(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    remote, local = world.remote_id, world.local_id
    cache = object_of(remote, "container", CACHE, "cache")
    managed = object_of(remote, "container", DB, "db", LABELLED)
    disabled = await seed(started_app, "off", HEALTH, JOB_SELECTOR, remote, enabled=False)
    elsewhere = await seed(started_app, "elsewhere", HEALTH, JOB_SELECTOR, local)
    holds = await seed(started_app, "holds", HEALTH, selector_json("container", "cache"), remote)
    full = await seed(started_app, "full", HEALTH, selector_json("container", *FULL_NAMES), remote)
    by_label = await seed(started_app, "by-label", HEALTH, selector_json("labels"), remote)
    updater = await seed(started_app, "update", UPDATER, JOB_SELECTOR, remote)
    selectors_before = await rows(started_app, "SELECT id, target_selector FROM automation_rule")

    async with job_services_on_hosts(started_app) as services:
        refused = {
            "rule_disabled": await refusal_of(started_app, services, disabled, cache),
            "other_environment": await refusal_of(started_app, services, elsewhere, cache),
            "already_target": await refusal_of(started_app, services, holds, cache),
            "target_full": await refusal_of(started_app, services, full, cache),
            "not_offered": await refusal_of(started_app, services, by_label, cache),
            "compose_managed": await refusal_of(started_app, services, updater, managed),
        }

    assert refused == {reason: (reason, True) for reason in refused}
    assert (
        await rows(started_app, "SELECT id, target_selector FROM automation_rule")
        == selectors_before
    )


async def test_a_hidden_rule_is_not_found_and_a_read_only_object_is_refused_with_no_audit_row(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    remote = world.remote_id
    theirs = await seed(started_app, "theirs", HEALTH, selector_json("container", "db"), remote)
    mine = await seed(
        started_app, "mine", HEALTH, selector_json("container", "db"), remote, owner=STANDARD_ID
    )
    read_only = object_of(remote, "container", JOB, "job")
    mark = await last_event_id(started_app)

    async with job_services_on_hosts(started_app) as services:
        with pytest.raises(ResourceNotFound):
            await services.rules.quick_add_preview(STANDARD, theirs, read_only)
        with pytest.raises(ResourceNotFound):
            await services.rules.quick_add(
                STANDARD,
                theirs,
                read_only,
                expected_updated_at="",
                expected_digest="",
                expected_name="job",
            )
        hidden_rows = await events(started_app, since=mark)
        reviewed = await review(services, STANDARD, mine, read_only)
        preview = await services.rules.quick_add_preview(STANDARD, mine, read_only)
        with pytest.raises(PermissionDenied):
            await services.rules.quick_add(STANDARD, mine, read_only, **reviewed)

    assert hidden_rows == []
    assert (preview.refusal, preview.target) == (NO_WRITE_ACCESS, None)
    assert not preview.owner_is_other
    assert await events(started_app, since=mark) == []
    stored = await rows(
        started_app, "SELECT target_selector FROM automation_rule WHERE id = ?", (mine,)
    )
    assert json.loads(stored[0]["target_selector"])["names"] == ["db"]


def spy_on(monkeypatch: pytest.MonkeyPatch, method: str) -> list[str]:
    original = getattr(Authoriser, method)
    calls: list[str] = []

    async def counted(self: Authoriser, *args: Any, **kwargs: Any) -> Any:
        calls.append(method)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Authoriser, method, counted)
    return calls


async def test_an_add_to_a_pruning_automation_a_standard_user_owns_is_refused_and_recorded(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    rule_id = await seed(
        started_app,
        "update",
        UPDATER,
        WORKER_SELECTOR,
        world.remote_id,
        owner=STANDARD_ID,
        parameters=UPDATE_AND_PRUNE,
    )
    cache = object_of(world.remote_id, "container", CACHE, "cache")
    selector_before = (await rows(started_app, "SELECT target_selector FROM automation_rule"))[0]

    async with job_services_on_hosts(started_app) as services:
        reviewed = await review(services, ADMIN, rule_id, cache)
        preview = await services.rules.quick_add_preview(ADMIN, rule_id, cache)
        mark = await last_event_id(started_app)
        with pytest.raises(ValidationFailed) as refused:
            await services.rules.quick_add(ADMIN, rule_id, cache, **reviewed)

    assert (preview.refusal, preview.target) == (PRUNE_NEEDS_ADMINISTRATOR, None)
    assert refused.value.fields == {"prune_replaced": PRUNE_NEEDS_ADMINISTRATOR}
    assert await rows(started_app, "SELECT target_selector FROM automation_rule") == [
        selector_before
    ]
    [failure] = await events(started_app, since=mark)
    assert (failure["action"], failure["result"]) == (UPDATED, "failure")
    assert json.loads(failure["detail"])["target_added"] == "cache"


async def test_the_standing_of_an_owner_other_than_the_viewer_is_resolved_against_the_object(
    started_app: FastAPI, world: World, shared_host: ContainerEngine
) -> None:
    remote = world.remote_id
    disabled = await seed_standard_user(started_app, "disabled-owner", enabled=False)
    ungranted = await seed_standard_user(started_app, "ungranted-owner")
    in_stack = object_of(remote, "container", DB, "db", LABELLED)
    read_only = object_of(remote, "container", JOB, "job")
    unseen = object_of(remote, "container", CACHE, "cache")
    cases = {
        "write": (HEALTH, STANDARD_ID, in_stack),
        "read": (HEALTH, STANDARD_ID, read_only),
        "hidden": (HEALTH, STANDARD_ID, unseen),
        "owner disabled": (HEALTH, disabled, in_stack),
        "owner without the environment": (HEALTH, ungranted, in_stack),
        "administrators-only automation": (EXPORTER, STANDARD_ID, in_stack),
        "viewer owns it": (HEALTH, ADMIN_ID, in_stack),
    }
    rule_ids = {
        name: await seed(started_app, name, subsystem, WORKER_SELECTOR, remote, owner=owner)
        for name, (subsystem, owner, _) in cases.items()
    }

    async with job_services_on_hosts(started_app) as services:
        standing = {
            name: (
                await services.rules.quick_add_preview(ADMIN, rule_ids[name], obj)
            ).owner_standing
            for name, (_, _, obj) in cases.items()
        }

    assert standing == {
        "write": "write",
        "read": "read",
        "hidden": "hidden",
        "owner disabled": "lost",
        "owner without the environment": "lost",
        "administrators-only automation": "lost",
        "viewer owns it": None,
    }


async def test_the_panel_checks_create_and_write_access_only_when_an_automation_qualifies(
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = world.remote_id
    await seed(started_app, "beat", HEALTH, WORKER_SELECTOR, remote)
    cache = object_of(remote, "container", CACHE, "cache")
    stack = object_of(remote, "stack", STACK, STACK)
    may_create = spy_on(monkeypatch, "may_create")
    require_create = spy_on(monkeypatch, "require_create")
    require_access = spy_on(monkeypatch, "require_access")

    async with job_services_on_hosts(started_app) as services:
        unqualified = await panel_for(services, ADMIN, stack)
        after_unqualified = (len(may_create), len(require_create), len(require_access))
        qualified = await panel_for(services, ADMIN, cache)
        after_qualified = (len(may_create), len(require_create), len(require_access))

    assert offered(unqualified) == []
    assert len(offered(qualified)) == 1
    assert after_unqualified == (1, 0, 0)
    assert after_qualified == (2, 0, 1)


async def test_an_add_is_refused_when_the_automation_changes_after_the_access_check_passed(
    started_app: FastAPI,
    world: World,
    shared_host: ContainerEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule_id = await seed(started_app, "health", HEALTH, WORKER_SELECTOR, world.remote_id)
    cache = object_of(world.remote_id, "container", CACHE, "cache")
    handler = started_app.state.scheduler.handlers.get(HEALTH)
    assert isinstance(handler, Subsystem)
    authorise = handler.authorise_target

    async with job_services_on_hosts(started_app) as services:
        reviewed = await review(services, ADMIN, rule_id, cache)

        async def edited_first(*args: Any) -> None:
            await execute(
                started_app,
                "UPDATE automation_rule SET target_selector = ?, updated_at = ? WHERE id = ?",
                (RACING_SELECTOR, NEVER, rule_id),
            )
            await authorise(*args)

        monkeypatch.setattr(handler, "authorise_target", edited_first)
        mark = await last_event_id(started_app)
        with pytest.raises(ValidationFailed) as raced:
            await services.rules.quick_add(ADMIN, rule_id, cache, **reviewed)
        stored = await rows(started_app, "SELECT target_selector FROM automation_rule")

    assert raced.value.fields == {"rule": CHANGED_BEFORE_ADD}
    [failure] = await events(started_app, since=mark)
    detail = json.loads(failure["detail"])
    assert (failure["action"], failure["result"]) == (UPDATED, "failure")
    assert (detail["reason"], detail["target_added"]) == ("changed_since_review", "cache")
    assert stored == [{"target_selector": RACING_SELECTOR}]
