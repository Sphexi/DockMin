"""A rule owner's standing over a listing of containers: the batch, its cap and its cursor."""

import dataclasses
import logging
from collections.abc import Mapping

import pytest

from app.db.engine import Session
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.docker_engine.models import ContainerSummary
from app.services.assembly import AccessServices
from app.services.object_locks import ObjectKey, ObjectLockRegistry
from app.subsystems.base import (
    MAX_FALLBACK_CHECKS,
    FallbackCursors,
    OwnerGate,
    RunContext,
    Standings,
)
from tests.access_support import (
    ADMIN,
    ENV_A,
    ENV_B,
    STANDARD,
    FakeInspector,
    container_id,
    container_key,
    control,
    stack_key,
    team_of,
)
from tests.auth_support import STANDARD_ID, FakeClock
from tests.automation_support import authority_context
from tests.support import present

STACK = "shop"
COMPOSE_LABEL = "com.docker.compose.project"
TEAM = "platform"
ROWS_IN_A_LISTING = 100
CUT_ROWS = MAX_FALLBACK_CHECKS + 5
HALF_THE_ALLOWANCE = 20
LOG_NAME = "owner_rows_not_checked"
FIRST_RULE, SECOND_RULE, THIRD_RULE = 1, 2, 3


def listing_row(
    number: int, labels: Mapping[str, str] | None = None, *, cut: bool = False
) -> ContainerSummary:
    return ContainerSummary.model_validate(
        {
            "Id": container_id(number),
            "Names": [f"/box{number}"],
            "Image": "img",
            "Created": 0,
            "State": "running",
            "Labels": dict(labels or {}),
            "LabelsTruncated": cut,
        }
    )


def cut_rows(first: int, count: int) -> list[ContainerSummary]:
    return [listing_row(first + n, cut=True) for n in range(count)]


def owner_gate(access: AccessServices, *, current: bool = True) -> OwnerGate:
    async def still_owner() -> bool:
        return current

    return OwnerGate(STANDARD, access.authoriser, still_owner)


async def run_context(db: Session, clock: FakeClock, gate: OwnerGate) -> RunContext:
    context = await authority_context(db, clock, ObjectLockRegistry(clock), environment_id=ENV_A)
    context.rule = await AutomationRuleRepository(db, clock).get(present(context.actor.rule_id))
    context.owner_gate = gate
    return context


async def seed_every_kind_of_control(
    access: AccessServices, inspector: FakeInspector, environment_id: int
) -> list[ContainerSummary]:
    team_id = await team_of(access, TEAM, STANDARD)
    plain, written, read_only, by_team, inheriting, admins_only, stranger = range(1, 8)
    labels = {inheriting: {COMPOSE_LABEL: STACK}}
    for number in range(plain, stranger + 1):
        inspector.add(container_key(number, environment_id), labels.get(number))
    await control(
        access, container_key(written, environment_id), level="write", users=(STANDARD_ID,)
    )
    await control(
        access, container_key(read_only, environment_id), level="read", users=(STANDARD_ID,)
    )
    await control(access, container_key(by_team, environment_id), level="write", teams=(team_id,))
    await control(access, stack_key(STACK, environment_id), level="write", users=(STANDARD_ID,))
    await control(access, container_key(admins_only, environment_id), administrators_only=True)
    await control(access, container_key(stranger, environment_id), level="write")
    return [listing_row(number, labels.get(number)) for number in range(plain, stranger + 1)]


@pytest.mark.parametrize(
    ("environment_id", "kinds"),
    [
        pytest.param(ENV_A, {"writable", "readable", "hidden"}, id="remote"),
        pytest.param(ENV_B, {"readable", "hidden"}, id="local-socket-ceiling"),
    ],
)
async def test_a_batch_standing_equals_the_per_object_standing_for_every_kind_of_control(
    access: AccessServices,
    inspector: FakeInspector,
    environment_id: int,
    kinds: set[str],
) -> None:
    rows = await seed_every_kind_of_control(access, inspector, environment_id)
    gate = owner_gate(access)
    inspector.calls.clear()

    found = await gate.standings(environment_id, rows, after=None)

    assert inspector.calls == []
    expected = {
        row.id: await gate.standing(ObjectKey(environment_id, "container", row.id)) for row in rows
    }
    assert found.by_id == expected
    assert set(expected.values()) == kinds
    assert found.not_checked == ()


async def test_an_administrators_batch_is_all_writable_and_reads_the_owner_once(
    access: AccessServices,
) -> None:
    reads: list[None] = []

    async def current() -> bool:
        reads.append(None)
        return True

    rows = [listing_row(number) for number in range(1, ROWS_IN_A_LISTING + 1)]
    gate = OwnerGate(ADMIN, None, current)

    found = await gate.standings(ENV_A, rows, after=None)

    assert len(reads) == 1
    assert set(found.by_id.values()) == {"writable"}
    assert len(found.by_id) == ROWS_IN_A_LISTING


async def test_an_owner_who_no_longer_qualifies_may_only_read_every_row_and_nothing_is_checked(
    access: AccessServices, inspector: FakeInspector
) -> None:
    rows = [*cut_rows(1, 3), listing_row(4)]
    gate = owner_gate(access, current=False)

    found = await gate.standings(ENV_A, rows, after=None)

    assert set(found.by_id.values()) == {"readable"}
    assert (found.not_checked, found.fallbacks) == ((), 0)
    assert inspector.calls == []


async def test_a_standard_owners_batch_over_complete_listing_labels_inspects_nothing(
    access: AccessServices, inspector: FakeInspector
) -> None:
    await control(access, stack_key(STACK), level="write", users=(STANDARD_ID,))
    rows = [
        listing_row(number, {COMPOSE_LABEL: STACK}) for number in range(1, ROWS_IN_A_LISTING + 1)
    ]

    found = await owner_gate(access).standings(ENV_A, rows, after=None)

    assert set(found.by_id.values()) == {"writable"}
    assert len(found.by_id) == ROWS_IN_A_LISTING
    assert inspector.calls == []


async def test_the_fallback_allowance_is_shared_by_every_listing_checked_in_one_run(
    access: AccessServices, inspector: FakeInspector, db: Session, clock: FakeClock
) -> None:
    context = await run_context(db, clock, owner_gate(access))
    cursors = FallbackCursors()
    first_listing = cut_rows(1, HALF_THE_ALLOWANCE)
    second_listing = cut_rows(1 + HALF_THE_ALLOWANCE, HALF_THE_ALLOWANCE)

    first = await context.standings(ENV_A, first_listing, cursors=cursors)
    second = await context.standings(ENV_A, second_listing, cursors=cursors)

    assert (len(first.by_id), len(first.not_checked)) == (HALF_THE_ALLOWANCE, 0)
    assert len(second.by_id) == MAX_FALLBACK_CHECKS - HALF_THE_ALLOWANCE
    assert len(second.not_checked) == 2 * HALF_THE_ALLOWANCE - MAX_FALLBACK_CHECKS
    assert len(inspector.calls) == MAX_FALLBACK_CHECKS


async def test_thirty_cut_rows_give_twenty_five_fallbacks_and_the_next_run_reaches_the_rest(
    access: AccessServices, inspector: FakeInspector, db: Session, clock: FakeClock
) -> None:
    context = await run_context(db, clock, owner_gate(access))
    next_run = dataclasses.replace(context)
    cursors = FallbackCursors()
    rows = cut_rows(1, CUT_ROWS)

    first = await context.standings(ENV_A, rows, cursors=cursors)
    reached_first = {call.resource_id for call in inspector.calls}
    second = await next_run.standings(ENV_A, rows, cursors=cursors)

    assert (len(first.by_id), len(first.not_checked), first.fallbacks) == (
        MAX_FALLBACK_CHECKS,
        CUT_ROWS - MAX_FALLBACK_CHECKS,
        MAX_FALLBACK_CHECKS,
    )
    assert reached_first.isdisjoint(first.not_checked)
    assert set(first.not_checked) <= set(second.by_id)
    assert {call.resource_id for call in inspector.calls} == {row.id for row in rows}


def test_each_rule_has_its_own_cursor_and_a_run_that_checked_nothing_leaves_it_in_place() -> None:
    cursors = FallbackCursors()

    cursors.advance(FIRST_RULE, container_id(7))
    cursors.advance(SECOND_RULE, container_id(9))
    cursors.advance(FIRST_RULE, None)

    assert cursors.after(FIRST_RULE) == container_id(7)
    assert cursors.after(SECOND_RULE) == container_id(9)
    assert cursors.after(THIRD_RULE) is None


async def test_the_unchecked_count_is_reported_for_an_administrator_owner(
    access: AccessServices, db: Session, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)

    async def current() -> bool:
        return True

    context = await run_context(db, clock, OwnerGate(ADMIN, None, current))
    found = Standings({}, not_checked=("a", "b"))

    reported = context.reportable_not_checked(found)

    assert reported == len(found.not_checked)
    assert [r for r in caplog.records if r.getMessage() == LOG_NAME] == []


async def test_the_unchecked_count_is_logged_and_not_reported_for_a_standard_owner(
    access: AccessServices, db: Session, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    context = await run_context(db, clock, owner_gate(access))
    found = Standings({}, not_checked=("a", "b"))

    reported = context.reportable_not_checked(found)

    [logged] = [r for r in caplog.records if r.getMessage() == LOG_NAME]
    assert reported == 0
    assert (logged.count, logged.rule_id) == (2, context.actor.rule_id)  # type: ignore[attr-defined]
