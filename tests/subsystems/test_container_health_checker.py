"""container-health-checker: what it restarts, within which budget, and what it never touches."""

import json
from typing import Any

import pytest

from app.db.repositories.automation_rule import AutomationRuleRepository
from app.services.activity import ActivityRecorder, SubsystemActor, UserActor
from app.services.object_locks import LockHolder, ObjectKey
from app.subsystems import container_health_checker as checker
from tests.access_support import ADMIN, ENV_A, PEER_ID, STANDARD
from tests.automation_support import HEALTH, INTERVAL_SECONDS, Subject, cid, counts, detail_of
from tests.docker_engine.container_engine import Box
from tests.docker_engine.fake_engine import Refuse

PROJECT_LABEL = "com.docker.compose.project"
SWARM_LABEL = "com.docker.swarm.service.id"
TIGHT_BUDGET = {"max_restarts": 2, "window_seconds": 300}
ONE_RESTART = {"max_restarts": 1}
POLL_SECONDS = 61
SETTLE_SECONDS = 5
EXHAUSTED = "container_restart_budget_exhausted"
RELEASED = "container_restart_budget_released"
RESTARTED = "container_restarted"
CONTAINER_LIST = "/containers/json"
RESTART_BATCH_SIZE = 25
BULK = {"kind": "labels", "labels": [["app", "bulk"]]}
EVERYTHING = {"kind": "environment"}
WINDOW_SECONDS = 300
AUTO_RELEASE = {"max_restarts": 1, "window_seconds": WINDOW_SECONDS, "auto_release": True}
SHORT_INTERVAL = "60"
SHOP_STACK = {"kind": "stack", "names": ["shop"]}


def container(name: str) -> dict[str, Any]:
    return {"kind": "container", "names": [name]}


def key(number: int) -> ObjectKey:
    return ObjectKey(ENV_A, "container", cid(number))


def user_holder() -> LockHolder:
    return LockHolder(UserActor.of(ADMIN), "admin-user", "stop")


def add_unhealthy(health: Subject, number: int, name: str, **fields: Any) -> Box:
    box: Box = health.host.add(cid(number), name, health="unhealthy", **fields)
    return box


def add_bulk(health: Subject, count: int, first: int = 0x100, prefix: str = "b") -> list[str]:
    ids = [cid(first + n) for n in range(count)]
    for n, box_id in enumerate(ids):
        health.host.add(box_id, f"{prefix}{n:02d}", health="unhealthy", labels={"app": "bulk"})
    return ids


async def run_until_exhausted(health: Subject, job: int, restarts: int) -> None:
    for _ in range(restarts):
        assert (await health.run(job)).outcome == "success"
        health.advance(POLL_SECONDS)


async def test_an_unhealthy_container_is_restarted_and_its_row_names_the_subsystem_rule_and_run(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    health.host.add(cid(2), "ok", health="healthy")
    rule_id, job = await health.rule(container("web"), name="my rule")

    result = await health.run(job)

    assert (result.outcome, result.attempted) == ("success", 1)
    assert counts(result) == {
        "restarted": 1,
        "targets": "web",
        "target_outcomes": "web: done",
        "matched": 1,
        "unhealthy": 1,
    }
    assert health.restarted() == [cid(1)]
    [row] = health.events(RESTARTED)
    assert (row["actor_type"], row["actor_subsystem"], row["result"]) == (
        "subsystem",
        HEALTH,
        "success",
    )
    assert (row["rule_id"], row["target_id"]) == (rule_id, cid(1))
    assert row["job_run_id"] is not None
    assert detail_of(row) == {
        "name": "web",
        "rule_id": rule_id,
        "rule_name": "my rule",
        "trigger": "unhealthy",
        "failing_streak": 3,
        "restarts_in_window": 1,
    }
    assert {event["actor_subsystem"] for event in health.events()} == {HEALTH}


async def test_only_running_containers_that_report_unhealthy_are_restarted(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "sick-a")
    add_unhealthy(health, 2, "sick-b")
    health.host.add(cid(3), "fine", health="healthy")
    health.host.add(cid(4), "booting", health="starting")
    health.host.add(cid(5), "plain")
    add_unhealthy(health, 6, "stopped", state="exited")
    add_unhealthy(health, 7, "frozen", state="paused")
    add_unhealthy(health, 8, "cycling", state="restarting")
    _, job = await health.rule(EVERYTHING)

    result = await health.run(job)

    assert sorted(health.restarted()) == [cid(1), cid(2)]
    assert counts(result) == {"restarted": 2, "matched": 2, "unhealthy": 2}


@pytest.mark.parametrize(
    ("selector", "expected_filters"),
    [
        (EVERYTHING, {"health": ["unhealthy"], "status": ["running"]}),
        (
            BULK,
            {"health": ["unhealthy"], "status": ["running"], "label": ["app=bulk"]},
        ),
        (
            {"kind": "labels", "labels": [["tier", None], ["app", "bulk"]]},
            {"health": ["unhealthy"], "status": ["running"], "label": ["app=bulk", "tier"]},
        ),
        (
            {"kind": "stack", "names": ["shop"]},
            {"health": ["unhealthy"], "status": ["running"], "label": [PROJECT_LABEL]},
        ),
    ],
)
async def test_the_engine_is_asked_for_running_unhealthy_containers_narrowed_by_the_selector(
    health: Subject, selector: dict[str, Any], expected_filters: dict[str, Any]
) -> None:
    health.scheduling.sql(
        "INSERT INTO stack (environment_id, name, kind, origin, project_directory, compose_file, "
        "compose_file_digest, created_at, updated_at) "
        "VALUES (?, 'shop', 'compose', 'created', ?, 'x', ?, 'x', 'x')",
        (ENV_A, "a" * 32, "b" * 64),
    )
    _, job = await health.rule(selector)

    await health.run(job)

    [listing] = health.host.calls("GET", CONTAINER_LIST)
    assert json.loads(listing.query["filters"]) == expected_filters


async def test_a_named_container_is_looked_up_by_exact_name_whatever_its_state(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    add_unhealthy(health, 2, "web-2")
    _, job = await health.rule(container("web"))

    await health.run(job)

    [listing] = health.host.calls("GET", CONTAINER_LIST)
    assert json.loads(listing.query["filters"]) == {"name": ["^/web$"]}
    assert health.restarted() == [cid(1)]


async def test_a_stack_selector_matches_the_project_label_ignoring_case(health: Subject) -> None:
    add_unhealthy(health, 1, "web", labels={PROJECT_LABEL: "shop"})
    add_unhealthy(health, 2, "db", labels={PROJECT_LABEL: "Shop"})
    add_unhealthy(health, 3, "other", labels={PROJECT_LABEL: "shop2"})
    add_unhealthy(health, 4, "bare")
    health.scheduling.sql(
        "INSERT INTO stack (environment_id, name, kind, origin, project_directory, compose_file, "
        "compose_file_digest, created_at, updated_at) "
        "VALUES (?, 'shop', 'compose', 'created', ?, 'x', ?, 'x', 'x')",
        (ENV_A, "a" * 32, "b" * 64),
    )
    _, job = await health.rule({"kind": "stack", "names": ["shop"]})

    result = await health.run(job)

    assert sorted(health.restarted()) == [cid(1), cid(2)]
    assert counts(result)["matched"] == 2


async def test_a_labels_selector_restarts_only_containers_carrying_every_term(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "both", labels={"app": "web", "tier": "front"})
    add_unhealthy(health, 2, "app-only", labels={"app": "web"})
    add_unhealthy(health, 3, "wrong-value", labels={"app": "api", "tier": "front"})
    _, job = await health.rule({"kind": "labels", "labels": [["app", "web"], ["tier", None]]})

    await health.run(job)

    assert health.restarted() == [cid(1)]


@pytest.mark.parametrize(
    ("selector", "stack_kind"),
    [
        (container("ghost"), None),
        ({"kind": "stack", "names": ["ghost"]}, None),
        ({"kind": "stack", "names": ["swarmy"]}, "swarm"),
    ],
)
async def test_an_explicit_target_that_is_gone_fails_the_run_as_target_missing(
    health: Subject, selector: dict[str, Any], stack_kind: str | None
) -> None:
    if stack_kind is not None:
        health.scheduling.sql(
            "INSERT INTO stack (environment_id, name, kind, origin, project_directory, "
            "compose_file, compose_file_digest, created_at, updated_at) "
            "VALUES (?, 'swarmy', ?, 'created', ?, 'x', ?, 'x', 'x')",
            (ENV_A, stack_kind, "a" * 32, "b" * 64),
        )
    _, job = await health.rule(selector)

    result = await health.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert health.restarted() == []


async def test_swarm_tasks_and_containers_with_truncated_labels_are_never_restarted(
    health: Subject,
) -> None:
    padding = "x" * (64 * 1024 - 20)
    add_unhealthy(
        health, 1, "task", labels={"aaa.padding": padding, SWARM_LABEL: "svc", "zzz": "1"}
    )
    add_unhealthy(health, 2, "plain-task", labels={SWARM_LABEL: "svc"})
    add_unhealthy(
        health, 3, "huge", labels={"aaa.padding": "x" * (64 * 1024 - 10), "bbb": "y" * 99}
    )
    add_unhealthy(health, 4, "fine")
    _, job = await health.rule(EVERYTHING)

    result = await health.run(job)

    assert health.restarted() == [cid(4)]
    assert counts(result) == {
        "restarted": 1,
        "matched": 4,
        "unhealthy": 1,
        "swarm_managed": 2,
        "unaddressable": 1,
    }


async def test_a_crash_looping_container_is_restarted_up_to_its_budget_then_held_and_reported(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    rule_id, job = await health.rule(container("web"), TIGHT_BUDGET)
    await run_until_exhausted(health, job, 2)

    exhausted = await health.run(job)
    health.advance(POLL_SECONDS)
    held = await health.run(job)
    health.advance(TIGHT_BUDGET["window_seconds"] * 3)
    still_held = await health.run(job)

    assert (exhausted.outcome, exhausted.reason) == ("failure", "restart_budget_exhausted")
    assert counts(exhausted)["exhausted"] == 1
    assert [counts(r) for r in (held, still_held)] == [
        {
            "restarted": 0,
            "held": 1,
            "targets": "web",
            "target_outcomes": "web: held",
            "matched": 1,
            "unhealthy": 1,
        }
    ] * 2
    assert (held.outcome, still_held.outcome) == ("success", "success")
    assert len(health.posts("restart")) == 2
    [row] = health.events(EXHAUSTED)
    assert (row["result"], row["actor_subsystem"], row["rule_id"]) == ("failure", HEALTH, rule_id)
    assert row["job_run_id"] is not None
    assert detail_of(row) == {
        "name": "web",
        "restarts": 2,
        "max_restarts": 2,
        "window_seconds": 300,
        "reason": "restart_budget_exhausted",
        "message": "The container is still unhealthy and its restart budget is used up.",
        "rule_id": rule_id,
        "rule_name": "Heal web",
    }


async def test_restarts_older_than_the_window_no_longer_count_against_the_budget(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    _, job = await health.rule(container("web"), TIGHT_BUDGET)
    await run_until_exhausted(health, job, 2)
    health.advance(TIGHT_BUDGET["window_seconds"])

    result = await health.run(job)

    assert result.outcome == "success"
    assert len(health.posts("restart")) == 3
    assert health.events(EXHAUSTED) == []


async def test_the_restart_counts_the_budget_reads_are_those_of_this_container_and_this_rule(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    add_unhealthy(health, 2, "api")
    _, web_job = await health.rule(container("web"), ONE_RESTART, name="web rule")
    _, api_job = await health.rule(container("api"), ONE_RESTART, name="api rule")
    await health.run(web_job)
    health.advance(POLL_SECONDS)

    web_again = await health.run(web_job)
    api_first = await health.run(api_job)

    assert web_again.reason == "restart_budget_exhausted"
    assert api_first.outcome == "success"
    assert health.restarted() == [cid(1), cid(2)]


async def test_a_rule_edit_releases_a_held_container_and_the_budget_starts_again(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    rule_id, job = await health.rule(container("web"), ONE_RESTART)
    await run_until_exhausted(health, job, 1)
    assert (await health.run(job)).reason == "restart_budget_exhausted"
    health.advance(SETTLE_SECONDS)
    async with health.database.session() as db:
        await AutomationRuleRepository(db, health.clock).set_enabled(rule_id, True)
    health.advance(SETTLE_SECONDS)

    resumed = await health.run(job)

    assert (resumed.outcome, counts(resumed)["restarted"]) == ("success", 1)
    assert len(health.posts("restart")) == 2


async def test_a_users_action_after_the_exhaustion_row_releases_the_hold_once_suppression_ends(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    _, job = await health.rule(container("web"), ONE_RESTART | {"suppression_seconds": 60})
    await run_until_exhausted(health, job, 1)
    await health.run(job)
    health.advance(SETTLE_SECONDS)
    await health.rig.services.actions.restart(ADMIN, ENV_A, cid(1))
    health.advance(SETTLE_SECONDS)

    suppressed = await health.run(job)
    health.advance(POLL_SECONDS)
    resumed = await health.run(job)

    assert (suppressed.outcome, suppressed.reason) == ("skipped", "suppressed")
    assert (resumed.outcome, counts(resumed)["restarted"]) == ("success", 1)
    assert len(health.events(EXHAUSTED)) == 1


async def test_a_recreated_container_has_a_budget_of_its_own(health: Subject) -> None:
    add_unhealthy(health, 1, "web")
    _, job = await health.rule(container("web"), ONE_RESTART)
    await run_until_exhausted(health, job, 1)
    assert (await health.run(job)).reason == "restart_budget_exhausted"
    del health.host.boxes[cid(1)]
    add_unhealthy(health, 2, "web")
    health.advance(POLL_SECONDS)

    result = await health.run(job)

    assert result.outcome == "success"
    assert health.restarted()[-1] == cid(2)


async def test_containers_that_recovered_since_the_list_was_read_are_not_restarted(
    health: Subject,
) -> None:
    boxes = [add_unhealthy(health, 1, "a"), add_unhealthy(health, 2, "b")]
    _, job = await health.rule(EVERYTHING)
    inspections: list[int] = []

    def recover_before_the_restarts() -> None:
        inspections.append(1)
        if len(inspections) == 3:
            for box in boxes:
                box.health = "healthy"

    health.host.on("inspect", recover_before_the_restarts)

    result = await health.run(job)

    assert health.posts("restart") == []
    assert counts(result) == {"restarted": 0, "matched": 2, "unhealthy": 0, "recovered": 2}


async def test_ten_containers_of_which_three_fail_to_restart_make_a_partial_run(
    health: Subject,
) -> None:
    add_bulk(health, 10)
    health.host.fail("restart", 500, skip=3, times=3)
    _, job = await health.rule(BULK)

    result = await health.run(job)

    assert (result.outcome, result.attempted) == ("partial", 10)
    assert counts(result)["restarted"] == 7
    assert counts(result)["failed"] == 3
    rows = health.events(RESTARTED)
    assert [row["result"] for row in rows].count("success") == 7
    assert [row["result"] for row in rows].count("failure") == 3
    assert {row["rule_id"] for row in rows} == {1}


async def test_at_most_twenty_five_restarts_are_made_per_run_and_the_rest_are_deferred(
    health: Subject,
) -> None:
    add_bulk(health, 30)
    _, job = await health.rule(BULK)

    result = await health.run(job)

    assert len(health.posts("restart")) == RESTART_BATCH_SIZE
    assert counts(result)["restarted"] == RESTART_BATCH_SIZE
    assert counts(result)["deferred"] == 5


async def test_containers_whose_budget_is_used_up_do_not_count_toward_the_per_run_cap(
    health: Subject,
) -> None:
    add_bulk(health, 20)
    _, job = await health.rule(BULK, ONE_RESTART)
    await health.run(job)
    health.advance(POLL_SECONDS)
    add_bulk(health, 10, first=0x200, prefix="n")

    result = await health.run(job)

    assert counts(result)["restarted"] == 10
    assert counts(result)["exhausted"] == 20
    assert "deferred" not in counts(result)


async def test_containers_the_owner_cannot_change_do_not_use_up_the_evaluation_cap(
    health: Subject, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checker, "MAX_EVALUATED_PER_RUN", 2)
    for number, name in ((1, "aaa-1"), (2, "aaa-2"), (3, "zzz-mine")):
        add_unhealthy(health, number, name)
    await health.grant(1, PEER_ID, is_public=True, access_level="read")
    await health.grant(2, PEER_ID, is_public=True, access_level="read")
    await health.grant(3, STANDARD.user_id)
    _, job = await health.rule(EVERYTHING, owner=STANDARD.user_id)

    result = await health.run(job)

    assert health.restarted() == [cid(3)]
    assert counts(result)["not_permitted"] == 2


async def test_the_evaluation_cap_moves_its_start_point_so_every_container_is_reached(
    health: Subject, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checker, "MAX_EVALUATED_PER_RUN", 2)
    add_bulk(health, 4)
    _, job = await health.rule(BULK, {"max_restarts": 20})

    for _ in range(4):
        await health.run(job)
        health.advance(POLL_SECONDS)

    assert set(health.restarted()) == {cid(0x100 + n) for n in range(4)}


async def test_a_standard_owner_acts_only_on_what_they_may_change_and_learns_nothing_of_the_rest(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "mine", labels={"team.token": "guess-1"})
    add_unhealthy(health, 2, "hidden", labels={"team.token": "s3cret"})
    add_unhealthy(health, 3, "hidden-task", labels={"team.token": "s3cret", SWARM_LABEL: "svc"})
    add_unhealthy(health, 4, "read-only", labels={"team.token": "s3cret"})
    await health.grant(1, STANDARD.user_id)
    await health.grant(2, PEER_ID)
    await health.grant(4, PEER_ID, is_public=True, access_level="read")
    _, wrong = await health.rule(
        {"kind": "labels", "labels": [["team.token", "nope"]]}, name="wrong", owner=STANDARD.user_id
    )
    _, right = await health.rule(
        {"kind": "labels", "labels": [["team.token", "s3cret"]]},
        name="right",
        owner=STANDARD.user_id,
    )

    miss = await health.run(wrong, dry=True)
    hit = await health.run(right, dry=True)

    assert counts(miss) == {
        "would_restart_count": 0,
        "would_restart": "",
        "matched": 0,
        "unhealthy": 0,
    }
    assert counts(hit) == counts(miss) | {"matched": 1, "not_permitted": 1}
    assert health.posts("restart") == []


async def test_a_standard_owner_has_their_own_container_restarted(health: Subject) -> None:
    add_unhealthy(health, 1, "mine", labels={"app": "web"})
    add_unhealthy(health, 2, "theirs", labels={"app": "web"})
    await health.grant(1, STANDARD.user_id)
    await health.grant(2, PEER_ID, is_public=True, access_level="read")
    _, job = await health.rule(
        {"kind": "labels", "labels": [["app", "web"]]}, owner=STANDARD.user_id
    )

    result = await health.run(job)

    assert health.restarted() == [cid(1)]
    assert counts(result)["not_permitted"] == 1
    assert result.outcome == "success"


async def test_a_rule_whose_named_container_its_owner_may_not_change_fails_the_run_as_denied(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "theirs")
    await health.grant(1, PEER_ID, is_public=True, access_level="read")
    _, job = await health.rule(container("theirs"), owner=STANDARD.user_id)

    result = await health.run(job)

    assert (result.outcome, result.reason) == ("failure", "permission_denied")
    assert health.posts("restart") == []
    assert health.subsystem_events() == []


async def test_an_owner_demoted_during_the_run_loses_the_containers_after_that_point(
    health: Subject,
) -> None:
    add_bulk(health, 3)
    _, job = await health.rule(BULK)
    demoted: list[bool] = []

    async def demote_after_the_first_restart() -> None:
        if demoted:
            return
        demoted.append(True)
        async with health.database.session() as other:
            await other.execute("UPDATE user SET role = 'standard' WHERE id = 1")

    health.host.on("restart", demote_after_the_first_restart)

    result = await health.run(job)

    assert len(health.posts("restart")) == 1
    assert counts(result)["restarted"] == 1
    assert counts(result)["not_permitted"] == 2


async def test_a_dry_run_reports_what_it_would_restart_and_writes_nothing(health: Subject) -> None:
    add_unhealthy(health, 1, "web")
    add_unhealthy(health, 2, "api")
    health.host.add(cid(3), "fine", health="healthy")
    _, job = await health.rule(EVERYTHING, enabled=False)

    result = await health.run(job, dry=True)

    assert result.outcome == "success"
    assert counts(result) == {
        "would_restart_count": 2,
        "would_restart": "api, web",
        "matched": 2,
        "unhealthy": 2,
    }
    assert health.host.mutations() == []
    assert health.events() == []
    assert len(health.rig.engine.locks) == 0


async def test_a_dry_run_forecasts_exhaustion_holds_and_busy_containers(health: Subject) -> None:
    add_unhealthy(health, 1, "spent")
    add_unhealthy(health, 2, "busy")
    _, job = await health.rule(EVERYTHING, ONE_RESTART)
    await health.run(job)
    health.advance(POLL_SECONDS)
    add_unhealthy(health, 3, "fresh")
    async with health.rig.engine.locks.hold(key(2), user_holder()):
        result = await health.run(job, dry=True)

    assert counts(result) == {
        "would_restart_count": 1,
        "would_restart": "fresh",
        "matched": 3,
        "unhealthy": 2,
        "exhausted": 1,
        "busy": 1,
    }


async def test_a_container_a_user_acted_on_recently_is_left_alone_until_the_window_ends(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    add_unhealthy(health, 2, "api")
    _, many = await health.rule(EVERYTHING, name="many")
    _, one = await health.rule(container("web"), name="one")
    await health.rig.services.actions.restart(ADMIN, ENV_A, cid(1))
    health.host.requests.clear()

    named = await health.run(one)
    dynamic = await health.run(many)
    health.advance(6 * 60)
    later = await health.run(one)

    assert (named.outcome, named.reason) == ("skipped", "suppressed")
    assert counts(dynamic)["suppressed"] == 1
    assert health.restarted() == [cid(2), cid(1)]
    assert later.outcome == "success"


async def test_a_container_locked_by_a_user_is_reported_busy_without_a_row(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    add_unhealthy(health, 2, "api")
    health.subsystem.lock_wait = 0.05  # type: ignore[misc]
    _, many = await health.rule(EVERYTHING, name="many")
    _, one = await health.rule(container("web"), name="one")

    async with health.rig.engine.locks.hold(key(1), user_holder()):
        dynamic = await health.run(many)
        named = await health.run(one)

    assert counts(dynamic)["busy"] == 1
    assert counts(dynamic)["restarted"] == 1
    assert (named.outcome, named.reason) == ("skipped", "object_busy")
    assert health.restarted() == [cid(2)]
    assert [row["target_id"] for row in health.events(RESTARTED)] == [cid(2)]


async def test_an_unreachable_host_skips_the_run_and_a_faulty_one_fails_it(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    _, job = await health.rule(EVERYTHING)
    health.host.route("GET", CONTAINER_LIST, Refuse())

    unreachable = await health.run(job)
    health.host.clear_faults()
    health.host.health_listing_status = 500
    faulty = await health.run(job)

    assert (unreachable.outcome, unreachable.reason) == ("skipped", "environment_unreachable")
    assert (faulty.outcome, faulty.reason) == ("failure", "engine_error")
    assert health.posts("restart") == []


async def test_a_held_container_that_has_recovered_counts_held_not_recovered_or_unhealthy(
    health: Subject,
) -> None:
    box = add_unhealthy(health, 1, "web")
    _, job = await health.rule(container("web"), ONE_RESTART)
    await run_until_exhausted(health, job, 1)
    await health.run(job)
    box.health = "healthy"
    health.advance(POLL_SECONDS)

    result = await health.run(job)

    assert (result.outcome, counts(result)) == (
        "success",
        {
            "restarted": 0,
            "held": 1,
            "targets": "web",
            "target_outcomes": "web: held",
            "matched": 1,
            "unhealthy": 0,
        },
    )
    assert "recovered" not in counts(result)


async def test_a_container_the_owner_cannot_see_is_a_target_that_was_not_found(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    _, job = await health.rule(container("web"), owner=STANDARD.user_id)

    result = await health.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert health.posts("restart") == []
    assert health.events() == []


async def test_a_rule_without_an_environment_is_refused_before_anything_is_read(
    health: Subject,
) -> None:
    _, job = await health.rule(EVERYTHING)
    health.scheduling.sql("UPDATE automation_rule SET environment_id = NULL")

    result = await health.run(job)

    assert (result.outcome, result.reason) == ("failure", "environment_missing")
    assert health.host.calls("GET", CONTAINER_LIST) == []


async def test_a_host_that_stops_answering_while_a_container_is_examined_skips_the_run(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    _, job = await health.rule(container("web"))
    health.host.route("GET", f"/containers/{cid(1)}/json", Refuse())

    result = await health.run(job)

    assert (result.outcome, result.reason) == ("skipped", "environment_unreachable")
    assert health.posts("restart") == []


async def test_a_stored_service_selector_is_refused_at_run_time_and_touches_nothing(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    _, job = await health.rule({"kind": "service", "name": "web"})

    result = await health.run(job)

    assert (result.outcome, result.reason) == ("failure", "selector_invalid")
    assert health.restarted() == [] and health.events() == []


async def hold(health: Subject, rule_id: int, number: int, name: str) -> None:
    async with health.database.session() as db:
        await ActivityRecorder(db, health.clock).record_failure(
            SubsystemActor(HEALTH, rule_id, None, "Heal web"),
            action=EXHAUSTED,
            target_type="container",
            target_id=cid(number),
            environment_id=ENV_A,
            reason="restart_budget_exhausted",
            detail={"name": name},
        )


def add_shop_stack(health: Subject) -> None:
    health.scheduling.sql(
        "INSERT INTO stack (environment_id, name, kind, origin, project_directory, compose_file, "
        "compose_file_digest, created_at, updated_at) "
        "VALUES (?, 'shop', 'compose', 'created', ?, 'x', ?, 'x', 'x')",
        (ENV_A, "a" * 32, "b" * 64),
    )


async def exhausted(
    health: Subject,
    parameters: dict[str, Any],
    *,
    restarts: int = 1,
    expression: str = INTERVAL_SECONDS,
) -> tuple[int, int]:
    """Run a rule on the unhealthy container "web" until its budget is used up and it is held."""
    rule_id, job = await health.rule(container("web"), parameters, expression=expression)
    await run_until_exhausted(health, job, restarts)
    assert (await health.run(job)).reason == "restart_budget_exhausted"
    return rule_id, job


async def test_a_held_container_that_is_not_in_the_unhealthy_listing_counts_held_not_recovered(
    health: Subject,
) -> None:
    box = add_unhealthy(health, 1, "web")
    _, job = await health.rule(EVERYTHING, ONE_RESTART)
    await run_until_exhausted(health, job, 1)
    await health.run(job)
    box.health = "healthy"
    health.advance(POLL_SECONDS)

    result = await health.run(job)

    assert counts(result) == {"restarted": 0, "held": 1, "matched": 0, "unhealthy": 0}


async def test_a_held_container_still_unhealthy_counts_held_and_unhealthy_and_is_not_restarted(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    _, job = await health.rule(EVERYTHING, ONE_RESTART)
    await run_until_exhausted(health, job, 1)
    await health.run(job)
    health.advance(POLL_SECONDS)

    result = await health.run(job)

    assert counts(result) == {"restarted": 0, "held": 1, "matched": 1, "unhealthy": 1}
    assert len(health.posts("restart")) == 1


async def test_a_held_container_of_a_stack_the_rule_names_is_counted_held_for_that_stack(
    health: Subject,
) -> None:
    add_shop_stack(health)
    box = add_unhealthy(health, 1, "web", labels={PROJECT_LABEL: "shop"})
    rule_id, job = await health.rule(SHOP_STACK, ONE_RESTART)
    await hold(health, rule_id, 1, "web")
    box.health = "healthy"
    health.advance(POLL_SECONDS)

    result = await health.run(job)

    assert counts(result)["held"] == 1
    assert counts(result)["target_outcomes"] == "shop: held"
    assert "recovered" not in counts(result)


async def test_a_held_container_of_a_stack_the_rule_no_longer_finds_is_dropped_without_error(
    health: Subject,
) -> None:
    add_shop_stack(health)
    health.host.add(cid(1), "kept", health="unhealthy", labels={PROJECT_LABEL: "shop"})
    ghost = health.host.add(cid(2), "orphan", health="healthy", labels={PROJECT_LABEL: "ghost"})
    rule_id, job = await health.rule({"kind": "stack", "names": ["shop", "ghost"]}, ONE_RESTART)
    await hold(health, rule_id, 2, "orphan")
    health.advance(POLL_SECONDS)

    result = await health.run(job)

    assert ghost.health == "healthy"
    assert result.outcome == "success"
    assert counts(result)["not_found_names"] == "ghost"
    assert counts(result)["restarted"] == 1
    assert "held" not in counts(result)


async def test_a_rule_naming_one_stack_the_owner_cannot_see_fails_as_target_missing(
    health: Subject,
) -> None:
    add_shop_stack(health)
    add_unhealthy(health, 1, "web", labels={PROJECT_LABEL: "shop"})
    _, job = await health.rule(SHOP_STACK, owner=STANDARD.user_id)

    result = await health.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert health.posts("restart") == []


@pytest.mark.parametrize(
    "labels",
    [{"app": "web", SWARM_LABEL: "svc"}, {"app": "other"}],
    ids=["swarm-managed", "no-longer-matched"],
)
async def test_a_held_container_the_rule_no_longer_acts_on_is_dropped_not_counted(
    health: Subject, labels: dict[str, str]
) -> None:
    health.host.add(cid(1), "web", health="healthy", labels=labels)
    rule_id, job = await health.rule({"kind": "labels", "labels": [["app", "web"]]}, ONE_RESTART)
    await hold(health, rule_id, 1, "web")
    health.advance(POLL_SECONDS)

    result = await health.run(job)

    assert counts(result) == {"restarted": 0, "matched": 0, "unhealthy": 0}


async def test_a_held_container_that_was_removed_is_released_as_gone(health: Subject) -> None:
    rule_id, job = await health.rule({"kind": "labels", "labels": [["app", "web"]]}, ONE_RESTART)
    await hold(health, rule_id, 1, "web")
    health.advance(POLL_SECONDS)

    result = await health.run(job)

    assert counts(result) == {"restarted": 0, "matched": 0, "unhealthy": 0, "released_gone": 1}
    [row] = health.events(RELEASED)
    assert (row["actor_subsystem"], row["rule_id"], row["target_id"]) == (HEALTH, rule_id, cid(1))
    assert detail_of(row) == {
        "name": "web",
        "released": "gone",
        "rule_id": rule_id,
        "rule_name": "Heal web",
    }


async def test_a_held_container_the_owner_cannot_see_is_dropped_not_counted(
    health: Subject,
) -> None:
    health.host.add(cid(1), "web", health="healthy", labels={"app": "web"})
    rule_id, job = await health.rule(
        {"kind": "labels", "labels": [["app", "web"]]}, ONE_RESTART, owner=STANDARD.user_id
    )
    await hold(health, rule_id, 1, "web")
    health.advance(POLL_SECONDS)

    result = await health.run(job)

    assert counts(result) == {"restarted": 0, "matched": 0, "unhealthy": 0}
    assert health.events(RELEASED) == []


async def test_a_held_container_healthy_for_a_full_window_is_released_by_the_subsystem(
    health: Subject,
) -> None:
    box = add_unhealthy(health, 1, "web")
    rule_id, job = await exhausted(health, AUTO_RELEASE, expression=SHORT_INTERVAL)
    box.health = "healthy"
    health.advance(WINDOW_SECONDS - 1)
    early = await health.run(job)
    health.advance(1)

    due = await health.run(job)

    assert (counts(early)["held"], "released" in counts(early)) == (1, False)
    assert (counts(due)["released"], "held" in counts(due)) == (1, False)
    [row] = health.events(RELEASED)
    assert (row["actor_type"], row["actor_subsystem"], row["result"]) == (
        "subsystem",
        HEALTH,
        "success",
    )
    assert (row["rule_id"], row["target_id"]) == (rule_id, cid(1))
    assert row["job_run_id"] is not None
    assert detail_of(row) == {
        "name": "web",
        "released": "automatic",
        "rule_id": rule_id,
        "rule_name": "Heal web",
    }


async def test_after_an_automatic_release_the_container_goes_through_its_restarts_again(
    health: Subject,
) -> None:
    box = add_unhealthy(health, 1, "web")
    params = AUTO_RELEASE | {"max_restarts": 2}
    _, job = await exhausted(health, params, restarts=2, expression=SHORT_INTERVAL)
    box.health = "healthy"
    health.advance(WINDOW_SECONDS)
    await health.run(job)
    box.health = "unhealthy"
    health.advance(POLL_SECONDS)

    first = await health.run(job)
    health.advance(POLL_SECONDS)
    second = await health.run(job)
    health.advance(POLL_SECONDS)
    third = await health.run(job)

    assert [counts(r).get("restarted") for r in (first, second)] == [1, 1]
    assert (third.outcome, third.reason) == ("failure", "restart_budget_exhausted")
    assert len(health.posts("restart")) == 4
    assert len(health.events(EXHAUSTED)) == 2
    assert len(health.events(RELEASED)) == 1


async def test_a_held_container_is_not_released_while_the_interval_is_longer_than_the_window(
    health: Subject,
) -> None:
    box = add_unhealthy(health, 1, "web")
    _, job = await exhausted(health, AUTO_RELEASE)
    box.health = "healthy"
    health.advance(WINDOW_SECONDS)
    within_interval = await health.run(job)
    health.advance(int(INTERVAL_SECONDS) - WINDOW_SECONDS)

    after_interval = await health.run(job)

    assert counts(within_interval)["held"] == 1
    assert "released" not in counts(within_interval)
    assert counts(after_interval)["released"] == 1
    assert len(health.events(RELEASED)) == 1


async def test_a_sighting_while_held_moves_the_release_a_full_window_after_it(
    health: Subject,
) -> None:
    box = add_unhealthy(health, 1, "web")
    _, job = await exhausted(health, AUTO_RELEASE, expression=SHORT_INTERVAL)
    health.advance(200)
    seen = await health.run(job)
    box.health = "healthy"
    health.advance(100)
    too_soon = await health.run(job)
    health.advance(WINDOW_SECONDS - 101)
    just_short = await health.run(job)
    health.advance(1)

    released = await health.run(job)

    assert (counts(seen)["held"], counts(seen)["unhealthy"]) == (1, 1)
    assert [counts(r).get("held") for r in (too_soon, just_short)] == [1, 1]
    assert counts(released)["released"] == 1


async def test_nothing_is_released_automatically_unless_the_rule_asks_for_it(
    health: Subject,
) -> None:
    box = add_unhealthy(health, 1, "web")
    _, job = await exhausted(health, ONE_RESTART | {"window_seconds": WINDOW_SECONDS})
    box.health = "healthy"
    health.advance(WINDOW_SECONDS * 10)

    result = await health.run(job)

    assert counts(result)["held"] == 1
    assert "released" not in counts(result)
    assert health.events(RELEASED) == []


async def test_a_dry_run_counts_the_release_it_would_make_and_writes_nothing(
    health: Subject,
) -> None:
    box = add_unhealthy(health, 1, "web")
    _, job = await exhausted(health, AUTO_RELEASE, expression=SHORT_INTERVAL)
    box.health = "healthy"
    health.advance(WINDOW_SECONDS)

    planned = await health.run(job, dry=True)
    real = await health.run(job)

    assert counts(planned)["released"] == 1
    assert counts(real)["released"] == 1
    assert len(health.events(RELEASED)) == 1


async def test_a_failed_user_action_after_the_exhaustion_row_does_not_end_the_hold(
    health: Subject,
) -> None:
    add_unhealthy(health, 1, "web")
    _, job = await exhausted(health, ONE_RESTART)
    health.advance(SETTLE_SECONDS)
    async with health.database.session() as db:
        await ActivityRecorder(db, health.clock).record_failure(
            UserActor.of(ADMIN),
            action="container_restarted",
            target_type="container",
            target_id=cid(1),
            environment_id=ENV_A,
            reason="engine_error",
        )
    health.advance(SETTLE_SECONDS)

    result = await health.run(job)

    assert counts(result)["held"] == 1
    assert len(health.posts("restart")) == 1


async def test_the_run_detail_stops_adding_counts_at_the_key_limit(
    health: Subject, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checker, "MAX_RUN_DETAIL_KEYS", 4)
    add_unhealthy(health, 1, "web")
    _, job = await health.rule(container("web"))

    result = await health.run(job)

    assert set(counts(result)) == {"restarted", "targets", "target_outcomes", "matched"}
