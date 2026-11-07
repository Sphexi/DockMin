"""Age-based retention: what stays, one pruning row per batch, the bounds and the clock guard."""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

import app.services.activity_retention as retention_module
from app.db.repositories.clocked import format_timestamp
from app.services.activity import ActivityRecorder
from app.services.activity_retention import PRUNED_ACTION, PRUNER, ActivityRetention
from tests.activity.retention_support import DAYS, NOW, OLD_AGE, UPDATER, YOUNG_AGE, World
from tests.scheduler.support import Rig

RECREATED = "container_recreated"
STOPPED = "container_stopped"
HOUR = timedelta(hours=1)
JUMP_DAYS = 3
HALF_HOUR_IN_DAYS = 0.5 / 24
MINUTE_IN_DAYS = 1 / 1440
SMALL_BATCH = 5
SMALL_SCAN = 20
BATCH_COUNT = 3
SEEDED_OLD_ROWS = 40
RULE_LOG = "app.services.activity_retention"


@dataclass(frozen=True, slots=True)
class Seed:
    """One event to record; rule-bearing seeds are the updater rule's own."""

    label: str
    age: float
    options: dict[str, Any] = field(default_factory=dict)


def seed(label: str, age: float, **options: Any) -> Seed:
    return Seed(label, age, options)


def by_rule(label: str, age: float, action: str = RECREATED, **options: Any) -> Seed:
    return seed(
        label, age, action=action, category="automation", subsystem=UPDATER, rule=True, **options
    )


STACK = ("stack", "Web")
SCENARIOS = [
    pytest.param([seed("old", OLD_AGE), seed("young", YOUNG_AGE)], False, ["young"], id="age-cut"),
    pytest.param(
        [seed("login", OLD_AGE, category="authentication")], False, [], id="old-authentication-row"
    ),
    pytest.param(
        [by_rule("older", OLD_AGE + 5), by_rule("anchor", OLD_AGE)],
        False,
        ["anchor"],
        id="newest-of-a-rule-action-on-a-target-is-kept-and-its-older-sibling-is-not",
    ),
    pytest.param(
        [by_rule("recreated", OLD_AGE), by_rule("stopped", OLD_AGE + 1, action=STOPPED)],
        False,
        ["recreated", "stopped"],
        id="one-anchor-per-action",
    ),
    pytest.param(
        [
            by_rule("anchor", OLD_AGE),
            seed("after", OLD_AGE - 1),
            seed("before", OLD_AGE + 4),
        ],
        False,
        ["anchor", "after"],
        id="a-user-action-on-the-target-after-the-anchor-is-kept-and-one-before-it-is-not",
    ),
    pytest.param(
        [by_rule("anchor", OLD_AGE), seed("elsewhere", OLD_AGE - 1, target=("container", "c9"))],
        False,
        ["anchor"],
        id="a-user-action-on-another-target-is-not-evidence",
    ),
    pytest.param(
        [by_rule("anchor", OLD_AGE), seed("outside", OLD_AGE - 1, environment=False)],
        False,
        ["anchor"],
        id="a-user-action-outside-the-environment-is-not-evidence",
    ),
    pytest.param(
        [
            by_rule("anchor", OLD_AGE),
            seed("recorded", OLD_AGE - 1, category="automation"),
        ],
        False,
        ["anchor"],
        id="an-automation-row-after-the-anchor-is-not-evidence",
    ),
    pytest.param(
        [
            by_rule("anchor", OLD_AGE, target=STACK),
            seed("after", OLD_AGE - 1, target=("stack", "web")),
        ],
        False,
        ["anchor", "after"],
        id="stack-evidence-matches-the-name-with-case-folded",
    ),
    pytest.param(
        [
            by_rule("anchor", OLD_AGE, target=STACK),
            seed("service", OLD_AGE - 1, target=("service", "web_app")),
            seed("service-before", OLD_AGE + 3, target=("service", "web_app")),
        ],
        False,
        ["anchor", "service"],
        id="a-service-action-after-a-stack-anchor-is-evidence",
    ),
    pytest.param(
        [by_rule("a", OLD_AGE), by_rule("b", OLD_AGE - 1), seed("user", OLD_AGE - 2)],
        True,
        [],
        id="rows-of-a-deleted-rule-and-what-they-would-have-released-are-removed",
    ),
]


@pytest.fixture
async def world(rig: Rig) -> World:
    return await World(rig).with_rule()


async def apply_retention(world: World, days: int = DAYS) -> int:
    result = await ActivityRetention(world.rig.database, clock=world.rig.clock, days=days).apply(
        NOW
    )
    return result.removed


async def record(world: World, seeds: list[Seed]) -> None:
    for item in seeds:
        options = dict(item.options)
        if options.pop("rule", False):
            options["rule_id"] = world.rule_id
        await world.event(item.label, item.age, **options)


@pytest.mark.parametrize(("seeds", "delete_rule", "kept"), SCENARIOS)
async def test_retention_keeps_anchors_and_release_evidence_and_the_young_and_removes_the_rest(
    world: World, seeds: list[Seed], delete_rule: bool, kept: list[str]
) -> None:
    await record(world, seeds)
    if delete_rule:
        world.rig.sql("DELETE FROM automation_rule WHERE id = ?", (world.rule_id,))

    await apply_retention(world)

    assert world.labels() == kept


async def test_a_pass_records_one_row_naming_the_pruner_and_what_it_removed(world: World) -> None:
    await world.event("first", OLD_AGE + 2)
    await world.event("last", OLD_AGE)
    await world.event("young", YOUNG_AGE)
    first, last = world.id_of("first"), world.id_of("last")

    removed = await apply_retention(world)

    [row] = world.pruned_rows()
    assert removed == 2
    assert (row["category"], row["actor_type"], row["actor_subsystem"], row["result"]) == (
        "automation",
        "subsystem",
        PRUNER,
        "success",
    )
    assert (row["rule_id"], row["job_run_id"], row["environment_id"]) == (None, None, None)
    assert row["target_type"] == "activity_log"
    assert row["detail"] == {
        "removed": 2,
        "retention_days": DAYS,
        "cutoff": format_timestamp(NOW - timedelta(days=DAYS)),
        "first_removed_id": first,
        "last_removed_id": last,
        "oldest_removed_at": format_timestamp(NOW - timedelta(days=OLD_AGE + 2)),
        "newest_removed_at": format_timestamp(NOW - timedelta(days=OLD_AGE)),
        "kept_for_automation": 0,
        "export_watermark": None,
    }


async def test_a_pass_with_nothing_to_remove_writes_nothing(world: World) -> None:
    await world.event("young", YOUNG_AGE)

    removed = await apply_retention(world)

    assert (removed, world.count()) == (0, 1)


async def test_a_second_pass_over_the_same_log_removes_nothing_more(world: World) -> None:
    await world.event("old", OLD_AGE)
    await apply_retention(world)
    rows_after_first = world.count()

    removed = await apply_retention(world)

    assert (removed, world.count()) == (0, rows_after_first)


async def test_every_batch_that_removes_rows_records_its_own_row(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.bulk(SEEDED_OLD_ROWS, OLD_AGE)
    monkeypatch.setattr(retention_module, "BATCH_ROWS", SMALL_BATCH)
    monkeypatch.setattr(retention_module, "BATCHES_PER_PASS", BATCH_COUNT)
    monkeypatch.setattr(retention_module, "SCAN_ROWS", SMALL_SCAN)

    removed = await apply_retention(world)

    rows = world.pruned_rows()
    assert removed == SMALL_BATCH * BATCH_COUNT
    assert [row["detail"]["removed"] for row in rows] == [SMALL_BATCH] * BATCH_COUNT
    assert world.count() == SEEDED_OLD_ROWS - removed + BATCH_COUNT


async def test_a_later_pass_finishes_what_a_bounded_pass_left(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.bulk(SEEDED_OLD_ROWS, OLD_AGE)
    monkeypatch.setattr(retention_module, "BATCH_ROWS", SMALL_BATCH)
    monkeypatch.setattr(retention_module, "BATCHES_PER_PASS", BATCH_COUNT)
    await apply_retention(world)
    monkeypatch.undo()

    await apply_retention(world)

    assert world.labels() == []


async def test_a_batch_whose_row_cannot_be_recorded_removes_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.bulk(SMALL_BATCH, OLD_AGE)

    async def refuse(self: ActivityRecorder, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the row cannot be written")

    monkeypatch.setattr(ActivityRecorder, "record_event", refuse)

    with pytest.raises(RuntimeError, match="cannot be written"):
        await apply_retention(world)

    assert world.count() == SMALL_BATCH


async def test_the_pruners_own_rows_are_never_removed(world: World) -> None:
    await world.event(
        "earlier-pruning",
        OLD_AGE,
        action=PRUNED_ACTION,
        category="automation",
        subsystem=PRUNER,
        environment=False,
        target=("activity_log", None),
    )
    await world.event("old", OLD_AGE)

    await apply_retention(world)

    labels = [row["detail"].get("label") for row in world.pruned_rows()]
    assert labels == ["earlier-pruning", None]


async def test_more_anchors_than_the_limit_remove_nothing_and_say_so(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=RULE_LOG)
    await record(
        world,
        [
            by_rule("a", OLD_AGE, target=("container", "a")),
            by_rule("b", OLD_AGE, target=("container", "b")),
        ],
    )
    await world.event("old", OLD_AGE)
    monkeypatch.setattr(retention_module, "MAX_ANCHORS", 1)

    removed = await apply_retention(world)

    assert removed == 0
    assert "retention_anchor_limit" in [r.getMessage() for r in caplog.records]
    assert world.labels() == ["a", "b", "old"]


async def test_a_pass_that_spends_its_whole_scan_budget_on_kept_rows_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=RULE_LOG)
    anchors = [by_rule(f"a{n}", OLD_AGE, target=("container", f"c{n}")) for n in range(20)]
    await record(world, anchors)
    monkeypatch.setattr(retention_module, "BATCHES_PER_PASS", BATCH_COUNT)
    monkeypatch.setattr(retention_module, "SCAN_ROWS", SMALL_BATCH)

    removed = await apply_retention(world)

    assert removed == 0
    assert "retention_scan_exhausted" in [r.getMessage() for r in caplog.records]


async def test_a_pass_that_reaches_the_end_of_the_candidates_does_not_report_exhaustion(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=RULE_LOG)
    await record(world, [by_rule("anchor", OLD_AGE)])

    await apply_retention(world)

    assert "retention_scan_exhausted" not in [r.getMessage() for r in caplog.records]


def jump_warnings(caplog: pytest.LogCaptureFixture) -> int:
    return [record.getMessage() for record in caplog.records].count("retention_clock_jump")


async def test_a_normal_hourly_advance_keeps_removing_what_falls_past_the_cap(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=RULE_LOG)
    retention = ActivityRetention(world.rig.database, clock=world.rig.clock, days=DAYS)
    await world.event("first", OLD_AGE)
    await retention.apply(NOW)
    await world.event("second", DAYS - HALF_HOUR_IN_DAYS)

    await retention.apply(NOW + HOUR)

    assert world.labels() == []
    assert jump_warnings(caplog) == 0


async def test_a_forward_jump_removes_nothing_on_every_pass_until_the_process_restarts(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=RULE_LOG)
    jump = NOW + timedelta(days=JUMP_DAYS)
    retention = ActivityRetention(world.rig.database, clock=world.rig.clock, days=DAYS)
    await retention.apply(NOW)
    await world.event("old-after-the-jump", DAYS - JUMP_DAYS + MINUTE_IN_DAYS)

    first = await retention.apply(jump)
    second = await retention.apply(jump + HOUR)

    assert (first.removed, second.removed) == (0, 0)
    assert world.labels() == ["old-after-the-jump"]
    assert jump_warnings(caplog) == 2


async def test_a_restarted_process_accepts_any_gap_since_the_last_pass(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=RULE_LOG)
    jump = NOW + timedelta(days=JUMP_DAYS)
    await ActivityRetention(world.rig.database, clock=world.rig.clock, days=DAYS).apply(NOW)
    await world.event("old-after-the-gap", DAYS - JUMP_DAYS + MINUTE_IN_DAYS)
    restarted = ActivityRetention(world.rig.database, clock=world.rig.clock, days=DAYS)

    result = await restarted.apply(jump)

    assert result.removed == 1
    assert world.labels() == []
    assert jump_warnings(caplog) == 0


def test_a_retention_of_less_than_one_day_is_refused(rig: Rig) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ActivityRetention(rig.database, clock=rig.clock, days=0)
