"""container-health-checker ends the hold on a container the host no longer has."""

from typing import Any

import pytest

from app.db.repositories.automation_rule import AutomationRuleRepository
from app.services.activity import ActivityRecorder, SubsystemActor
from app.services.restart_holds import rule_holds
from tests.access_support import ENV_A, STANDARD
from tests.auth_support import ADMIN_ID
from tests.automation_support import HEALTH, Subject, cid, counts, detail_of

EXHAUSTED = "container_restart_budget_exhausted"
RELEASED = "container_restart_budget_released"
GONE_HOLD = 1
RECREATED = 2
POLL_SECONDS = 61
NAMED = {"kind": "container", "names": ["web"]}
LABELLED = {"kind": "labels", "labels": [["app", "web"]]}
ONE_RESTART = {"max_restarts": 1}
OWNERS = [
    pytest.param(ADMIN_ID, id="administrator"),
    pytest.param(STANDARD.user_id, id="standard"),
]
TARGETS = [pytest.param(NAMED, id="named"), pytest.param(LABELLED, id="labels")]


async def hold_on_removed_container(health: Subject, rule_id: int) -> None:
    async with health.database.session() as db:
        await ActivityRecorder(db, health.clock).record_failure(
            SubsystemActor(HEALTH, rule_id, None, "Heal web"),
            action=EXHAUSTED,
            target_type="container",
            target_id=cid(GONE_HOLD),
            environment_id=ENV_A,
            reason="restart_budget_exhausted",
            detail={"name": "web"},
        )


async def held_ids(health: Subject, rule_id: int) -> list[str]:
    async with health.database.session() as db:
        rule = await AutomationRuleRepository(db, health.clock).get(rule_id)
        assert rule is not None
        return [hold.container_id for hold in await rule_holds(db, health.clock, rule)]


async def rule_with_gone_hold(
    health: Subject, selector: dict[str, Any], owner: int
) -> tuple[int, int]:
    """Make the replacement of the held container `web` visible to the owner and hold the old id."""
    health.host.add(cid(RECREATED), "web", labels={"app": "web"})
    await health.grant(RECREATED, owner)
    rule_id, job = await health.rule(selector, ONE_RESTART, owner=owner)
    await hold_on_removed_container(health, rule_id)
    health.advance(POLL_SECONDS)
    return rule_id, job


@pytest.mark.parametrize("selector", TARGETS)
@pytest.mark.parametrize("owner", OWNERS)
async def test_a_run_finding_a_held_container_gone_releases_it_once_and_the_next_run_writes_nothing(
    health: Subject, owner: int, selector: dict[str, Any]
) -> None:
    rule_id, job = await rule_with_gone_hold(health, selector, owner)

    first = await health.run(job)
    health.advance(POLL_SECONDS)
    second = await health.run(job)

    [released] = health.events(RELEASED)
    assert counts(first)["released_gone"] == 1
    assert "released_gone" not in counts(second)
    assert (released["actor_subsystem"], released["rule_id"], released["target_id"]) == (
        HEALTH,
        rule_id,
        cid(GONE_HOLD),
    )
    assert detail_of(released)["released"] == "gone"
    assert await held_ids(health, rule_id) == []


@pytest.mark.parametrize("owner", OWNERS)
async def test_a_dry_run_counts_the_gone_release_it_would_make_and_writes_nothing(
    health: Subject, owner: int
) -> None:
    rule_id, job = await rule_with_gone_hold(health, LABELLED, owner)

    forecast = await health.run(job, dry=True)

    assert counts(forecast)["would_release_gone"] == 1
    assert "released_gone" not in counts(forecast)
    assert health.events(RELEASED) == []
    assert await held_ids(health, rule_id) == [cid(GONE_HOLD)]


@pytest.mark.parametrize("owner", OWNERS)
async def test_a_single_name_rule_whose_only_container_is_gone_releases_the_hold_once(
    health: Subject, owner: int
) -> None:
    rule_id, job = await health.rule(NAMED, ONE_RESTART, owner=owner)
    await hold_on_removed_container(health, rule_id)
    health.advance(POLL_SECONDS)

    first = await health.run(job)
    health.advance(POLL_SECONDS)
    second = await health.run(job)

    [released] = health.events(RELEASED)
    assert (first.outcome, second.outcome) == ("failure", "failure")
    assert (released["actor_subsystem"], released["rule_id"], released["target_id"]) == (
        HEALTH,
        rule_id,
        cid(GONE_HOLD),
    )
    assert detail_of(released)["released"] == "gone"
    assert await held_ids(health, rule_id) == []
