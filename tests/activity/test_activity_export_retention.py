"""Retention with an exporter: unexported events are held, and the export cursor moves only up."""

import logging
from datetime import timedelta
from typing import Any

import pytest

import app.services.activity_retention as retention_module
from app.db.engine import DatabaseError, WriteResult
from app.db.repositories.activity_event import ActivityEventRepository
from app.db.repositories.activity_export_cursor import ActivityExportCursorRepository
from app.db.repositories.clocked import format_timestamp
from app.db.repositories.errors import MissingReferenceError
from app.services.activity_retention import PRUNER, ActivityRetention
from tests.activity.retention_support import DAYS, NOW, OLD_AGE, YOUNG_AGE, World
from tests.scheduler.support import Rig

RETENTION_LOG = "app.services.activity_retention"
ROWS = 30
SMALL_BATCH = 5
EXPORTED = 12
OBJECT_KEY = "exports/rule-1/2026/01/01/00000000000000000001.csv.gz"
PAST_CUTOFF = format_timestamp(NOW - timedelta(days=DAYS))
NOW_STAMP = format_timestamp(NOW)
UNKNOWN_RULE = 12345

WATERMARKS = [
    pytest.param([], None, id="no-exporter-rule"),
    pytest.param([(None, True)], 0, id="a-rule-that-has-exported-nothing"),
    pytest.param([(None, True), (50, True)], 0, id="one-rule-behind"),
    pytest.param([(10, True), (50, True)], 10, id="the-lowest-cursor"),
    pytest.param([(30, False)], 30, id="a-disabled-rule-still-counts"),
    pytest.param([(30, False), (None, True)], 0, id="a-disabled-rule-and-a-new-one"),
]


@pytest.fixture
async def world(rig: Rig) -> World:
    return await World(rig).with_rule()


async def apply_retention(world: World) -> int:
    result = await ActivityRetention(world.rig.database, clock=world.rig.clock, days=DAYS).apply(
        NOW
    )
    return result.removed


class CountingPrune:
    """Counts the batches a pass asks the repository for."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        original = ActivityEventRepository.prune_batch
        counter = self

        async def counting(self: ActivityEventRepository, **kwargs: Any) -> Any:
            counter.calls += 1
            return await original(self, **kwargs)

        monkeypatch.setattr(ActivityEventRepository, "prune_batch", counting)


async def with_exporter(world: World, cursor: int | None) -> int:
    rule_id = await world.exporter_rule()
    if cursor is not None:
        world.move_cursor(rule_id, cursor)
    return rule_id


async def add_exporters(world: World, rules: list[tuple[int | None, bool]]) -> None:
    for number, (cursor, enabled) in enumerate(rules):
        rule_id = await world.rig.rule(
            f"export-{number}",
            owner=world.user_id,
            subsystem="activity-log-exporter",
            enabled=enabled,
        )
        if cursor is not None:
            world.move_cursor(rule_id, cursor)


@pytest.mark.parametrize(("rules", "expected"), WATERMARKS)
async def test_the_watermark_is_the_lowest_cursor_of_every_exporter_rule_in_any_state(
    world: World, rules: list[tuple[int | None, bool]], expected: int | None
) -> None:
    await add_exporters(world, rules)

    async with world.rig.database.session() as db:
        watermark = await ActivityEventRepository(db).export_watermark()
        counted = await ActivityEventRepository(db).exporter_rule_count()

    assert (watermark, counted) == (expected, len(rules))


async def test_events_above_the_watermark_stay_however_old_and_the_exported_ones_go(
    world: World,
) -> None:
    world.bulk(ROWS, OLD_AGE)
    await with_exporter(world, EXPORTED)

    removed = await apply_retention(world)

    assert removed == EXPORTED
    assert world.labels() == [f"bulk{n}" for n in range(EXPORTED, ROWS)]


async def test_an_exporter_rule_that_has_exported_nothing_holds_every_event(world: World) -> None:
    world.bulk(ROWS, OLD_AGE)
    await with_exporter(world, None)

    removed = await apply_retention(world)

    assert (removed, world.count()) == (0, ROWS)


async def test_a_disabled_exporter_rule_still_holds_what_it_has_not_exported(
    world: World,
) -> None:
    world.bulk(ROWS, OLD_AGE)
    rule_id = await world.rig.rule(
        "export", owner=world.user_id, subsystem="activity-log-exporter", enabled=False
    )
    world.move_cursor(rule_id, EXPORTED)

    removed = await apply_retention(world)

    assert removed == EXPORTED


async def test_the_next_pass_removes_what_a_later_export_has_taken(world: World) -> None:
    world.bulk(ROWS, OLD_AGE)
    rule_id = await with_exporter(world, EXPORTED)
    await apply_retention(world)

    world.move_cursor(rule_id, ROWS)
    removed = await apply_retention(world)

    assert removed == ROWS - EXPORTED
    assert world.labels() == []


async def test_deleting_the_exporter_rule_releases_the_hold_and_its_cursor_goes_with_it(
    world: World,
) -> None:
    world.bulk(ROWS, OLD_AGE)
    rule_id = await with_exporter(world, EXPORTED)

    world.rig.sql("DELETE FROM automation_rule WHERE id = ?", (rule_id,))
    removed = await apply_retention(world)

    cursors = world.rig.sql("SELECT count(*) FROM activity_export_cursor")[0][0]
    assert (cursors, removed) == (0, ROWS)


async def test_the_pruners_own_rows_never_count_as_unexported_or_become_candidates(
    world: World,
) -> None:
    await world.event(
        "earlier-pruning",
        OLD_AGE,
        action="activity_log_pruned",
        category="automation",
        subsystem=PRUNER,
        environment=False,
        target=("activity_log", None),
    )
    world.bulk(SMALL_BATCH, OLD_AGE)
    await with_exporter(world, 1)

    removed = await apply_retention(world)

    assert removed == 0
    assert world.count() == SMALL_BATCH + 1


async def test_a_pass_held_by_the_exporter_stops_after_one_batch_and_says_why(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=RETENTION_LOG)
    world.bulk(ROWS, OLD_AGE)
    await with_exporter(world, None)
    prune = CountingPrune(monkeypatch)

    removed = await apply_retention(world)

    assert (removed, prune.calls, world.count()) == (0, 1, ROWS)
    assert "retention_held_by_export" in [record.getMessage() for record in caplog.records]


async def test_a_partly_exported_log_loses_the_exported_part_in_bounded_batches_then_stops(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.bulk(ROWS, OLD_AGE)
    await with_exporter(world, EXPORTED)
    monkeypatch.setattr(retention_module, "BATCH_ROWS", SMALL_BATCH)
    prune = CountingPrune(monkeypatch)

    removed = await apply_retention(world)

    assert removed == EXPORTED
    assert prune.calls == 3
    assert [row["detail"]["removed"] for row in world.pruned_rows()] == [5, 5, 2]


async def test_a_batch_reports_the_hold_when_the_oldest_unexported_event_is_past_the_cutoff(
    world: World,
) -> None:
    world.bulk(ROWS, OLD_AGE)
    await with_exporter(world, EXPORTED)

    async with world.rig.database.session() as db, db.transaction():
        batch = await ActivityEventRepository(db).prune_batch(
            cutoff=NOW_STAMP,
            anchors=[],
            resume_after=None,
            scan=100,
            limit=100,
        )

    assert (batch.removed, batch.watermark, batch.blocked_by_export) == (EXPORTED, EXPORTED, True)


async def test_a_pruners_row_just_above_the_watermark_does_not_end_the_hold(
    world: World,
) -> None:
    await world.event(
        "earlier-pruning",
        OLD_AGE,
        action="activity_log_pruned",
        category="automation",
        subsystem=PRUNER,
        environment=False,
        target=("activity_log", None),
    )
    world.bulk(SMALL_BATCH, OLD_AGE)
    await with_exporter(world, None)

    async with world.rig.database.session() as db, db.transaction():
        batch = await ActivityEventRepository(db).prune_batch(
            cutoff=NOW_STAMP,
            anchors=[],
            resume_after=None,
            scan=100,
            limit=100,
        )

    assert (batch.removed, batch.blocked_by_export) == (0, True)


async def test_a_batch_with_no_unexported_event_past_the_cutoff_reports_no_hold(
    world: World,
) -> None:
    world.bulk(SMALL_BATCH, OLD_AGE)
    world.bulk(SMALL_BATCH, YOUNG_AGE, prefix="young")
    await with_exporter(world, SMALL_BATCH)

    async with world.rig.database.session() as db, db.transaction():
        batch = await ActivityEventRepository(db).prune_batch(
            cutoff=PAST_CUTOFF, anchors=[], resume_after=None, scan=100, limit=100
        )

    assert batch.blocked_by_export is False


async def test_a_batch_outside_a_transaction_is_refused(world: World) -> None:
    async with world.rig.database.session() as db:
        with pytest.raises(RuntimeError, match="inside a transaction"):
            await ActivityEventRepository(db).prune_batch(
                cutoff=PAST_CUTOFF, anchors=[], resume_after=None, scan=10, limit=10
            )


async def test_a_batch_whose_delete_removes_a_different_number_of_rows_rolls_back(
    world: World,
) -> None:
    world.bulk(SMALL_BATCH, OLD_AGE)

    async with world.rig.database.session() as db:
        repository = ActivityEventRepository(db)
        write = repository._execute

        async def one_short(sql: str, params: tuple[Any, ...] = ()) -> WriteResult:
            written = await write(sql, params)
            return WriteResult(written.rowcount - 1, written.last_row_id)

        repository._execute = one_short  # type: ignore[assignment]
        with pytest.raises(DatabaseError, match="changed under the batch"):
            async with db.transaction():
                await repository.prune_batch(
                    cutoff=NOW_STAMP, anchors=[], resume_after=None, scan=100, limit=100
                )

    assert world.count() == SMALL_BATCH


@pytest.mark.parametrize(("limit", "scan"), [(0, 10), (10, 0)])
async def test_a_batch_with_no_room_to_scan_or_delete_is_refused(
    world: World, limit: int, scan: int
) -> None:
    async with world.rig.database.session() as db, db.transaction():
        with pytest.raises(ValueError, match="at least 1"):
            await ActivityEventRepository(db).prune_batch(
                cutoff=PAST_CUTOFF, anchors=[], resume_after=None, scan=scan, limit=limit
            )


async def test_a_cursor_moves_forward_and_records_the_object_that_holds_it(world: World) -> None:
    rule_id = await world.exporter_rule()

    async with world.rig.database.session() as db:
        cursors = ActivityExportCursorRepository(db, world.rig.clock)
        before = await cursors.get(rule_id)
        await cursors.move_export_cursor(rule_id, last_event_id=EXPORTED, object_key=OBJECT_KEY)
        after = await cursors.get(rule_id)

    assert before is None
    assert (after.last_event_id, after.object_key) == (EXPORTED, OBJECT_KEY)  # type: ignore[union-attr]


async def test_a_cursor_never_moves_back_but_the_same_id_rewrites_its_object(
    world: World,
) -> None:
    rule_id = await world.exporter_rule()

    async with world.rig.database.session() as db:
        cursors = ActivityExportCursorRepository(db, world.rig.clock)
        await cursors.move_export_cursor(rule_id, last_event_id=EXPORTED, object_key="first")
        await cursors.move_export_cursor(rule_id, last_event_id=EXPORTED - 1, object_key="lower")
        kept = await cursors.get(rule_id)
        await cursors.move_export_cursor(rule_id, last_event_id=EXPORTED, object_key="same-id")
        rewritten = await cursors.get(rule_id)

    assert (kept.last_event_id, kept.object_key) == (EXPORTED, "first")  # type: ignore[union-attr]
    assert rewritten.object_key == "same-id"  # type: ignore[union-attr]


async def test_moving_the_cursor_of_a_rule_that_does_not_exist_is_a_missing_reference(
    world: World,
) -> None:
    async with world.rig.database.session() as db:
        with pytest.raises(MissingReferenceError):
            await ActivityExportCursorRepository(db, world.rig.clock).move_export_cursor(
                UNKNOWN_RULE, last_event_id=1, object_key=OBJECT_KEY
            )


async def test_the_sequential_reads_count_and_list_in_id_order_within_their_caps(
    world: World,
) -> None:
    world.bulk(ROWS, OLD_AGE)

    async with world.rig.database.session() as db:
        events = ActivityEventRepository(db)
        listed = await events.after_id(EXPORTED, limit=SMALL_BATCH)
        first = await events.first_after(EXPORTED)
        capped = await events.count_after(EXPORTED, cap=SMALL_BATCH)
        uncapped = await events.count_after(EXPORTED, cap=ROWS)
        between = await events.count_between(SMALL_BATCH, EXPORTED, cap=ROWS)
        between_capped = await events.count_between(SMALL_BATCH, EXPORTED, cap=2)

    assert [event.id for event in listed] == list(range(EXPORTED + 1, EXPORTED + 1 + SMALL_BATCH))
    assert first.id == EXPORTED + 1  # type: ignore[union-attr]
    assert (capped, uncapped) == (SMALL_BATCH, ROWS - EXPORTED)
    assert (between, between_capped) == (EXPORTED - SMALL_BATCH, 2)


@pytest.mark.parametrize("call", ["count_after", "count_between"])
async def test_a_count_with_no_cap_is_refused(world: World, call: str) -> None:
    async with world.rig.database.session() as db:
        events = ActivityEventRepository(db)
        arguments = (0,) if call == "count_after" else (0, 1)

        with pytest.raises(ValueError, match="cap must be at least 1"):
            await getattr(events, call)(*arguments, cap=0)


@pytest.mark.parametrize("limit", [0, 2001])
async def test_a_sequential_read_outside_its_limit_is_refused(world: World, limit: int) -> None:
    async with world.rig.database.session() as db:
        with pytest.raises(ValueError, match="limit must be between"):
            await ActivityEventRepository(db).after_id(0, limit=limit)
