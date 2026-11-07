"""stack-restarter: scheduled restarts of one container or one Compose stack."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app.db.repositories.resource_control import canonical_stack_key
from app.services.activity import ActivityRecorder, UserActor
from app.services.object_locks import LockHolder, ObjectKey
from tests.access_support import ADMIN, ENV_A, PEER_ID, STANDARD
from tests.automation_support import STACK_RESTARTER, Subject, counts, detail_of
from tests.docker_engine.fake_engine import Refuse
from tests.stack_support import project_box, read_records, seed_stack_row

SHOP = "shop"
ALPHA = "alpha"
ALPHA_DB_BOX, ALPHA_WEB_BOX = 3, 4
WEB_ID = f"{0xB0001:064x}"
DB_ID = f"{0xB0002:064x}"
CACHE_ID = f"{0xB0003:064x}"
DB_BOX, WEB_BOX = 1, 2
SETTLE_SECONDS = 5
EXPECTED_ORDER = [["stop", "db", "web"], ["start", "db", "web"]]
STACK_RESTARTED = "stack_restarted"
RESTARTED = "container_restarted"


def container(*names: str) -> dict[str, Any]:
    return {"kind": "container", "names": list(names)}


def stack(*names: str) -> dict[str, Any]:
    return {"kind": "stack", "names": list(names or (SHOP,))}


def shop_file(record: Path, *, start_fails: bool = False) -> str:
    failure = "  start: fail\n" if start_fails else ""
    return (
        "services:\n"
        "  db:\n    image: nginx:1\n"
        "  web:\n    image: nginx:1\n    depends_on: [db]\n"
        f"x-fake:\n  record: {record}\n{failure}"
    )


def verbs(record: Path) -> list[list[str]]:
    """Return the Compose command and service arguments of each stop and start the stub saw."""
    found = []
    for call in read_records(record):
        argv = call["argv"]
        start = argv.index("--env-file") + 2
        if argv[start] == "--profile":
            start += 2
        if argv[start] in {"stop", "start", "restart"}:
            found.append(argv[start:])
    return found


async def seed_shop(
    restarter: Subject, record: Path, *, state: str = "running", **options: Any
) -> None:
    rig = restarter.rig
    await seed_stack_row(rig, ENV_A, SHOP, shop_file(record), **options)
    project_box(rig.host, DB_BOX, SHOP, "db", state=state)
    project_box(rig.host, WEB_BOX, SHOP, "web", state=state)


@pytest.fixture
def record(tmp_path: Path) -> Path:
    return tmp_path / "compose-record.jsonl"


def member_key(restarter: Subject, number: int) -> ObjectKey:
    box = next(b for b in restarter.host.boxes.values() if b.name.endswith(f"-{number}"))
    return ObjectKey(ENV_A, "container", box.id)


async def test_a_container_target_is_restarted_by_the_subsystem_under_its_rule(
    restarter: Subject,
) -> None:
    restarter.host.add(WEB_ID, "web")
    rule_id, job = await restarter.rule(container("web"), name="Nightly web")

    result = await restarter.run(job)

    assert (result.outcome, result.attempted) == ("success", 1)
    assert counts(result) == {
        "restarted": 1,
        "target_kind": "container",
        "targets": "web",
        "target_outcomes": "web: done",
    }
    assert restarter.host.verbs() == ["restart"]
    [row] = restarter.events(RESTARTED)
    assert (row["actor_type"], row["actor_subsystem"], row["rule_id"]) == (
        "subsystem",
        STACK_RESTARTER,
        rule_id,
    )
    assert row["job_run_id"] is not None
    assert detail_of(row)["trigger"] == "schedule"
    assert detail_of(row)["rule_name"] == "Nightly web"


async def test_a_stack_is_restarted_by_stopping_then_starting_its_running_services(
    restarter: Subject, record: Path
) -> None:
    await seed_shop(restarter, record)
    rule_id, job = await restarter.rule(stack(), name="Nightly shop")

    result = await restarter.run(job)

    assert (result.outcome, result.attempted) == ("success", 1)
    assert counts(result) == {
        "restarted": 1,
        "target_kind": "stack",
        "services": 2,
        "targets": SHOP,
        "target_outcomes": f"{SHOP}: done",
    }
    assert verbs(record) == EXPECTED_ORDER
    assert {box.state for box in restarter.host.boxes.values()} == {"running"}
    [row] = restarter.events(STACK_RESTARTED)
    assert (row["actor_subsystem"], row["rule_id"], row["target_id"], row["result"]) == (
        STACK_RESTARTER,
        rule_id,
        SHOP,
        "success",
    )
    detail = detail_of(row)
    assert detail["method"] == "stop_start"
    assert detail["services"] == "db,web"
    assert (detail["stop_exit_code"], detail["exit_code"]) == (0, 0)
    assert detail["rule_name"] == "Nightly shop"
    assert "phase" not in detail
    assert {event["actor_subsystem"] for event in restarter.events()} == {STACK_RESTARTER}


async def test_only_the_services_that_run_are_named_to_compose(
    restarter: Subject, record: Path
) -> None:
    await seed_shop(restarter, record)
    next(b for b in restarter.host.boxes.values() if b.name.endswith(f"-{DB_BOX}")).state = "exited"
    _, job = await restarter.rule(stack())

    await restarter.run(job)

    assert verbs(record) == [["stop", "web"], ["start", "web"]]
    assert all("restart" not in call for call in verbs(record))


@pytest.mark.parametrize("selector", [container("web"), stack()])
async def test_a_target_that_is_not_running_is_left_alone(
    restarter: Subject, record: Path, selector: dict[str, Any]
) -> None:
    restarter.host.add(WEB_ID, "web", state="exited")
    await seed_shop(restarter, record, state="exited")
    _, job = await restarter.rule(selector)

    result = await restarter.run(job)

    assert (result.outcome, result.attempted) == ("success", 0)
    assert counts(result)["not_running"] == 1
    assert restarter.host.mutations() == []
    assert read_records(record) == []
    assert restarter.subsystem_events() == []


@pytest.mark.parametrize(
    ("selector", "stopped"),
    [
        (container("web"), ObjectKey(ENV_A, "container", WEB_ID)),
        (stack(), ObjectKey(ENV_A, "stack", canonical_stack_key(SHOP))),
    ],
)
async def test_a_target_a_user_just_stopped_is_skipped_as_suppressed_not_reported_as_not_running(
    restarter: Subject, record: Path, selector: dict[str, Any], stopped: ObjectKey
) -> None:
    restarter.host.add(WEB_ID, "web", state="exited")
    await seed_shop(restarter, record, state="exited")
    restarter.suppressions.note(stopped)
    _, job = await restarter.rule(selector)

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("skipped", "suppressed")
    assert restarter.host.mutations() == []


async def test_a_failed_start_is_recorded_with_its_phase_and_the_next_run_starts_what_was_left(
    restarter: Subject, record: Path
) -> None:
    await seed_shop(restarter, record)
    _, job = await restarter.rule(stack())
    restarter.scheduling.sql(
        "UPDATE stack SET compose_file = ?", (shop_file(record, start_fails=True),)
    )

    failed = await restarter.run(job)
    restarter.scheduling.sql("UPDATE stack SET compose_file = ?", (shop_file(record),))
    restarter.advance(SETTLE_SECONDS)
    resumed = await restarter.run(job)

    assert failed.outcome == "failure"
    [first, second] = restarter.events(STACK_RESTARTED)
    assert first["result"] == "failure"
    assert {key: detail_of(first)[key] for key in ("phase", "method", "stop_exit_code")} == {
        "phase": "start",
        "method": "stop_start",
        "stop_exit_code": 0,
    }
    assert (detail_of(first)["exit_code"], detail_of(first)["unstarted"]) == (1, "db,web")
    assert resumed.outcome == "success"
    assert second["result"] == "success"
    assert detail_of(second)["services"] == "db,web"
    assert {box.state for box in restarter.host.boxes.values()} == {"running"}


async def test_a_users_action_on_the_stack_ends_the_obligation_to_start_what_was_left(
    restarter: Subject, record: Path
) -> None:
    await seed_shop(restarter, record)
    _, job = await restarter.rule(stack())
    restarter.scheduling.sql(
        "UPDATE stack SET compose_file = ?", (shop_file(record, start_fails=True),)
    )
    await restarter.run(job)
    restarter.advance(SETTLE_SECONDS)
    async with restarter.database.session() as db:
        await ActivityRecorder(db, restarter.clock).record_event(
            UserActor.of(ADMIN),
            action="stack_stopped",
            target_type="stack",
            target_id=SHOP,
            environment_id=ENV_A,
            result="success",
        )
    restarter.advance(6 * 60)

    result = await restarter.run(job)

    assert counts(result)["not_running"] == 1
    assert len(restarter.events(STACK_RESTARTED)) == 1


async def test_a_restart_the_host_refuses_is_a_failure_with_a_row_for_the_container(
    restarter: Subject,
) -> None:
    restarter.host.add(WEB_ID, "web")
    restarter.host.fail("restart", 500)
    _, job = await restarter.rule(container("web"))

    result = await restarter.run(job)

    assert (result.outcome, result.attempted) == ("failure", 1)
    [row] = restarter.events(RESTARTED)
    assert row["result"] == "failure"


@pytest.mark.parametrize("dry", [True, False])
async def test_a_stack_target_without_compose_fails_the_run(
    restarter: Subject, record: Path, dry: bool
) -> None:
    await seed_shop(restarter, record)
    _, job = await restarter.rule(stack())
    restarter.rig.engine.compose = None

    result = await restarter.run(job, dry=dry)

    assert (result.outcome, result.reason) == ("failure", "compose_unavailable")
    assert restarter.subsystem_events() == []


@pytest.mark.parametrize(
    ("selector", "kind"),
    [(container("ghost"), None), (stack("ghost"), None), (stack(SHOP), "swarm")],
)
async def test_a_target_that_is_gone_or_is_not_a_compose_stack_fails_as_target_missing(
    restarter: Subject, record: Path, selector: dict[str, Any], kind: str | None
) -> None:
    if kind is not None:
        await seed_shop(restarter, record)
        restarter.scheduling.sql("UPDATE stack SET kind = ?", (kind,))
    _, job = await restarter.rule(selector)

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert restarter.host.mutations() == []


async def test_a_swarm_task_container_is_never_restarted(restarter: Subject) -> None:
    restarter.host.add(WEB_ID, "web", labels={"com.docker.swarm.service.id": "svc"})
    _, job = await restarter.rule(container("web"))

    result = await restarter.run(job)

    assert (result.outcome, result.attempted) == ("success", 0)
    assert counts(result)["swarm_managed"] == 1
    assert restarter.host.mutations() == []


@pytest.mark.parametrize("target", ["container", "stack"])
async def test_a_dry_run_reports_the_restart_and_changes_nothing(
    restarter: Subject, record: Path, target: str
) -> None:
    restarter.host.add(WEB_ID, "web")
    await seed_shop(restarter, record)
    selector = container("web") if target == "container" else stack()
    _, job = await restarter.rule(selector, enabled=False)
    expected = "web" if target == "container" else f"stack {SHOP}"

    result = await restarter.run(job, dry=True)

    assert result.outcome == "success"
    assert counts(result)["would_restart"] == expected
    assert counts(result)["would_restart_count"] == 1
    assert restarter.host.mutations() == []
    assert read_records(record) == []
    assert restarter.events() == []


async def test_a_dry_run_forecasts_a_target_a_user_holds(restarter: Subject) -> None:
    restarter.host.add(WEB_ID, "web")
    _, job = await restarter.rule(container("web"))
    holder = LockHolder(UserActor.of(ADMIN), "admin-user", "stop")

    async with restarter.rig.engine.locks.hold(ObjectKey(ENV_A, "container", WEB_ID), holder):
        result = await restarter.run(job, dry=True)

    assert (result.outcome, result.reason) == ("skipped", "object_busy")


async def test_a_recent_user_action_on_the_container_or_a_member_of_the_stack_skips_the_run(
    restarter: Subject, record: Path
) -> None:
    restarter.host.add(WEB_ID, "web")
    await seed_shop(restarter, record)
    _, container_job = await restarter.rule(container("web"), name="container")
    _, stack_job = await restarter.rule(stack(), name="stack")
    restarter.suppressions.note(ObjectKey(ENV_A, "container", WEB_ID))
    restarter.suppressions.note(member_key(restarter, WEB_BOX))

    first = await restarter.run(container_job)
    second = await restarter.run(stack_job)

    assert [(r.outcome, r.reason) for r in (first, second)] == [("skipped", "suppressed")] * 2
    assert restarter.host.mutations() == []
    assert restarter.events() == []


async def test_a_member_a_user_acts_on_while_the_run_waits_for_the_stack_lock_ends_the_run(
    restarter: Subject, record: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_shop(restarter, record)
    _, job = await restarter.rule(stack())
    locks = restarter.rig.engine.locks
    real_hold = locks.hold
    member = member_key(restarter, WEB_BOX)

    @asynccontextmanager
    async def user_acts_during_the_wait(
        key: ObjectKey, holder: LockHolder, *, wait: float = 0.0
    ) -> AsyncIterator[None]:
        async with real_hold(key, holder, wait=wait):
            if key.object_type == "stack":
                restarter.suppressions.note(member)
            yield

    monkeypatch.setattr(locks, "hold", user_acts_during_the_wait)

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("skipped", "suppressed")
    assert read_records(record) == []
    assert restarter.events() == []


async def test_a_stack_a_user_holds_past_the_wait_skips_the_run_without_a_row(
    restarter: Subject, record: Path
) -> None:
    await seed_shop(restarter, record)
    _, job = await restarter.rule(stack())
    restarter.subsystem.lock_wait = 0.05  # type: ignore[misc]
    holder = LockHolder(UserActor.of(ADMIN), "admin-user", "redeploy")

    async with restarter.rig.engine.locks.hold(ObjectKey(ENV_A, "stack", SHOP), holder):
        result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("skipped", "object_busy")
    assert counts(result)["holder_action"] == "redeploy"
    assert restarter.events() == []


async def test_a_container_the_owner_cannot_see_is_reported_as_missing_and_left_alone(
    restarter: Subject,
) -> None:
    restarter.host.add(WEB_ID, "web")
    _, job = await restarter.rule(container("web"), owner=STANDARD.user_id)

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert restarter.host.mutations() == []
    assert restarter.subsystem_events() == []


async def test_a_container_the_owner_can_only_read_fails_as_permission_denied(
    restarter: Subject,
) -> None:
    restarter.host.add(WEB_ID, "web")
    await restarter.grant(WEB_ID, PEER_ID, access_level="read", user_ids=(STANDARD.user_id,))
    _, job = await restarter.rule(container("web"), owner=STANDARD.user_id)

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("failure", "permission_denied")
    assert restarter.host.mutations() == []


async def test_a_disabled_owner_cannot_have_anything_restarted(restarter: Subject) -> None:
    restarter.host.add(WEB_ID, "web")
    _, job = await restarter.rule(container("web"))
    await restarter.rig.db.execute("UPDATE user SET is_enabled = 0 WHERE id = ?", (ADMIN.user_id,))

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("failure", "owner_access_lost")
    assert restarter.host.mutations() == []


async def test_a_standard_owner_is_refused_a_stack_with_a_member_they_may_not_change(
    restarter: Subject, record: Path
) -> None:
    await seed_shop(restarter, record, owner_user_id=STANDARD.user_id)
    await restarter.grant(member_key(restarter, WEB_BOX).object_id, PEER_ID)
    _, job = await restarter.rule(stack(), owner=STANDARD.user_id)

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("failure", "permission_denied")
    assert read_records(record) == []
    assert restarter.subsystem_events() == []


async def test_an_unreachable_host_skips_the_run(restarter: Subject) -> None:
    restarter.host.add(WEB_ID, "web")
    restarter.host.route("GET", "/containers/json", Refuse())
    _, job = await restarter.rule(container("web"))

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("skipped", "environment_unreachable")


@pytest.mark.parametrize("selector", [container("web"), stack()])
async def test_a_host_that_answers_with_an_error_fails_the_run_under_one_reason(
    restarter: Subject, record: Path, selector: dict[str, Any]
) -> None:
    await seed_shop(restarter, record)
    restarter.host.add(WEB_ID, "web")
    restarter.host.listing_status = 500
    _, job = await restarter.rule(selector)

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("failure", "engine_error")
    assert restarter.host.mutations() == []


async def test_a_stored_service_selector_is_refused_at_run_time_and_touches_nothing(
    restarter: Subject,
) -> None:
    _, job = await restarter.rule({"kind": "service", "names": ["web"]})

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("failure", "selector_invalid")
    assert restarter.events() == [] and restarter.host.mutations() == []


async def test_several_containers_are_restarted_in_list_order_and_a_missing_name_is_reported(
    restarter: Subject,
) -> None:
    restarter.host.add(WEB_ID, "web")
    restarter.host.add(DB_ID, "db")
    _, job = await restarter.rule(container("web", "ghost", "db"))

    result = await restarter.run(job)

    assert (result.outcome, result.attempted) == ("success", 2)
    assert counts(result) == {
        "restarted": 2,
        "target_kind": "container",
        "targets": "db, web",
        "target_outcomes": "db: done, web: done",
        "not_found": 1,
        "not_found_names": "ghost",
    }
    assert restarter.restarted() == [DB_ID, WEB_ID]
    assert [row["target_id"] for row in restarter.events(RESTARTED)] == [DB_ID, WEB_ID]


async def test_a_container_that_fails_to_restart_does_not_stop_the_others(
    restarter: Subject,
) -> None:
    restarter.host.add(WEB_ID, "web")
    restarter.host.add(DB_ID, "db")
    restarter.host.fail("restart", 500, target=DB_ID)
    _, job = await restarter.rule(container("db", "web"))

    result = await restarter.run(job)

    assert (result.outcome, result.attempted) == ("partial", 2)
    detail = counts(result)
    assert (detail["restarted"], detail["failed"]) == (1, 1)
    assert detail["target_outcomes"].startswith("db: failed (")
    assert detail["target_outcomes"].endswith(", web: done")
    assert [row["result"] for row in restarter.events(RESTARTED)] == ["failure", "success"]


async def test_a_suppressed_container_among_several_is_counted_and_the_others_are_restarted(
    restarter: Subject,
) -> None:
    restarter.host.add(WEB_ID, "web")
    restarter.host.add(DB_ID, "db")
    restarter.suppressions.note(ObjectKey(ENV_A, "container", DB_ID))
    _, job = await restarter.rule(container("db", "web"))

    result = await restarter.run(job)

    assert (result.outcome, result.attempted) == ("success", 1)
    detail = counts(result)
    assert (detail["restarted"], detail["suppressed"]) == (1, 1)
    assert detail["target_outcomes"] == "db: suppressed, web: done"
    assert restarter.restarted() == [WEB_ID]


async def test_a_dry_run_over_several_containers_lists_the_ones_found_and_changes_nothing(
    restarter: Subject,
) -> None:
    restarter.host.add(WEB_ID, "web")
    restarter.host.add(DB_ID, "db")
    _, job = await restarter.rule(container("db", "ghost", "web"))

    result = await restarter.run(job, dry=True)

    assert result.outcome == "success"
    detail = counts(result)
    assert (detail["would_restart_count"], detail["would_restart"]) == (2, "db, web")
    assert (detail["not_found"], detail["not_found_names"]) == (1, "ghost")
    assert restarter.host.mutations() == []
    assert restarter.events() == []


async def test_a_standard_owners_targets_are_each_settled_by_what_the_owner_may_do(
    restarter: Subject,
) -> None:
    restarter.host.add(WEB_ID, "web")
    restarter.host.add(DB_ID, "db")
    restarter.host.add(CACHE_ID, "cache")
    await restarter.grant(WEB_ID, STANDARD.user_id)
    await restarter.grant(DB_ID, PEER_ID, access_level="read", user_ids=(STANDARD.user_id,))
    await restarter.grant(CACHE_ID, PEER_ID)
    _, job = await restarter.rule(container("cache", "db", "ghost", "web"), owner=STANDARD.user_id)

    result = await restarter.run(job)

    assert (result.outcome, result.attempted) == ("partial", 1)
    detail = counts(result)
    assert detail["target_outcomes"] == "db: failed (permission_denied), web: done"
    assert (detail["not_found"], detail["not_found_names"]) == (2, "cache, ghost")
    assert restarter.restarted() == [WEB_ID]


async def test_several_stacks_are_restarted_one_at_a_time_in_list_order(
    restarter: Subject, record: Path
) -> None:
    await seed_shop(restarter, record)
    await seed_stack_row(restarter.rig, ENV_A, ALPHA, shop_file(record))
    project_box(restarter.rig.host, ALPHA_DB_BOX, ALPHA, "db")
    project_box(restarter.rig.host, ALPHA_WEB_BOX, ALPHA, "web")
    _, job = await restarter.rule(stack(SHOP, "ghost", ALPHA))

    result = await restarter.run(job)

    assert (result.outcome, result.attempted) == ("success", 2)
    assert counts(result) == {
        "restarted": 2,
        "target_kind": "stack",
        "services": 4,
        "targets": f"{ALPHA}, {SHOP}",
        "target_outcomes": f"{ALPHA}: done, {SHOP}: done",
        "not_found": 1,
        "not_found_names": "ghost",
    }
    assert [row["target_id"] for row in restarter.events(STACK_RESTARTED)] == [ALPHA, SHOP]
    assert verbs(record) == EXPECTED_ORDER * 2


async def test_every_name_missing_fails_the_run_as_target_missing(restarter: Subject) -> None:
    _, job = await restarter.rule(container("ghost", "phantom"))

    result = await restarter.run(job)

    assert (result.outcome, result.reason) == ("failure", "target_missing")
    assert restarter.host.mutations() == []
