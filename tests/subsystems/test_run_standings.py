"""How many reads a run spends on its rule owner's access, in the updater and the health checker."""

import logging
import re
from typing import Any

import pytest

from app.db.repositories.user import UserRepository
from app.subsystems.base import MAX_FALLBACK_CHECKS
from tests.access_support import STANDARD, container_id, control, stack_key
from tests.automation_support import Subject, cid, counts
from tests.provisioning_support import OLD
from tests.updater_support import World, labelled

STACK = "shop"
COMPOSE_LABEL = "com.docker.compose.project"
CONTAINERS_IN_A_LISTING = 100
CUT_CONTAINERS = MAX_FALLBACK_CHECKS + 5
REPORT = {"action": "report"}
EVERYTHING = {"kind": "environment"}
PADDING_BYTES = 64 * 1024 - 10
OWNER_READS_PER_RUN = 2
NOT_CHECKED_LOG = "owner_rows_not_checked"
INSPECT_PATH = re.compile(r"(/v[0-9.]+)?/containers/(?P<id>[0-9a-f]{64})/json")


def inspected_ids(host: Any) -> list[str]:
    matched = (INSPECT_PATH.fullmatch(r.path) for r in host.requests if r.method == "GET")
    return [found["id"] for found in matched if found]


def add_listing(world: World, labels: dict[str, str]) -> None:
    for number in range(1, CONTAINERS_IN_A_LISTING + 1):
        world.host.add(container_id(0x500 + number), f"box{number:03d}", labels=labels)


async def test_a_standard_owners_run_over_complete_labels_makes_no_inspect_for_access(
    world: World,
) -> None:
    await control(world.rig.access, stack_key(STACK), level="write", users=(STANDARD.user_id,))
    add_listing(world, {"tier": "front", COMPOSE_LABEL: STACK})
    _, job = await world.rule(labelled(), REPORT, owner=STANDARD.user_id)

    result = await world.run(job)

    detail = counts(result)
    assert result.outcome == "success"
    assert (detail["matched"], detail["compose_managed"]) == (
        CONTAINERS_IN_A_LISTING,
        CONTAINERS_IN_A_LISTING,
    )
    assert inspected_ids(world.host) == []


async def test_an_administrators_run_over_a_large_listing_reads_the_owner_once_for_its_access(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.host.boxes.pop(OLD)
    add_listing(world, {"tier": "front", COMPOSE_LABEL: STACK})
    _, job = await world.rule(labelled(), REPORT)
    reads: list[int] = []
    original = UserRepository.get

    async def counting(self: UserRepository, user_id: int) -> Any:
        reads.append(user_id)
        return await original(self, user_id)

    monkeypatch.setattr(UserRepository, "get", counting)

    result = await world.run(job)

    assert counts(result)["matched"] == CONTAINERS_IN_A_LISTING
    assert len(reads) == OWNER_READS_PER_RUN
    assert inspected_ids(world.host) == []


async def test_thirty_cut_label_containers_cost_twenty_five_inspects_and_the_next_run_the_rest(
    health: Subject, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    padding = {"app.padding": "x" * PADDING_BYTES}
    for number in range(CUT_CONTAINERS):
        health.host.add(cid(0x600 + number), f"cut{number:02d}", health="unhealthy", labels=padding)
    _, job = await health.rule(EVERYTHING, owner=STANDARD.user_id)

    first = await health.run(job)
    inspected_first = inspected_ids(health.host)
    second = await health.run(job)

    logged = [r for r in caplog.records if r.getMessage() == NOT_CHECKED_LOG]
    assert len(inspected_first) == MAX_FALLBACK_CHECKS
    assert set(inspected_ids(health.host)) == {cid(0x600 + n) for n in range(CUT_CONTAINERS)}
    assert [r.count for r in logged] == [CUT_CONTAINERS - MAX_FALLBACK_CHECKS] * 2  # type: ignore[attr-defined]
    assert "not_checked" not in counts(first)
    assert "not_checked" not in counts(second)
    assert health.restarted() == []
