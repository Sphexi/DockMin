"""Restart holds: what a health-checker rule holds, since when, and when last seen unhealthy."""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.db.engine import Session
from app.db.repositories.activity_event import ActivityEventRepository, HeldTarget
from app.db.repositories.automation_rule import AutomationRule, AutomationRuleRepository
from app.db.repositories.clocked import Clock, parse_timestamp
from app.db.repositories.pagination import MAX_PAGE_SIZE
from app.db.repositories.restart_hold import RestartHoldRepository

HEALTH_CHECKER: Final = "container-health-checker"
EXHAUSTED_ACTION: Final = "container_restart_budget_exhausted"
RELEASED_ACTION: Final = "container_restart_budget_released"
MAX_HELD_LOOKUP: Final = 500


@dataclass(frozen=True, slots=True)
class Hold:
    """A container a rule holds: since when, and when the rule last saw it unhealthy."""

    rule_id: int
    container_id: str
    name: str
    held_since: str
    last_unhealthy_at: str


async def exhaustions(
    db: Session,
    clock: Clock,
    rule_id: int,
    environment_id: int,
    *,
    container_ids: list[str] | None = None,
    limit: int = MAX_HELD_LOOKUP,
) -> list[HeldTarget]:
    """Return the rule's containers with an exhaustion row, newest first, held or not."""
    return await ActivityEventRepository(db, clock).held_targets(
        environment_id,
        HEALTH_CHECKER,
        rule_id,
        EXHAUSTED_ACTION,
        limit,
        release_action=RELEASED_ACTION,
        target_ids=container_ids,
    )


def is_held(rule: AutomationRule, target: HeldTarget) -> bool:
    """Report whether the exhaustion row still holds the container.

    A rule write, a successful user action on the container or an automatic release after the
    row ends it.
    """
    at = parse_timestamp(target.occurred_at)
    if parse_timestamp(rule.updated_at) > at:
        return False
    return all(
        later is None or parse_timestamp(later) <= at
        for later in (target.acted_at, target.released_at)
    )


def released_at(target: HeldTarget) -> datetime | None:
    """Return when the rule released the container after its exhaustion row, else None."""
    if target.released_at is None:
        return None
    at = parse_timestamp(target.released_at)
    return at if at > parse_timestamp(target.occurred_at) else None


def last_seen_unhealthy(exhausted: str, sighting: str | None) -> str:
    """Return the later of the exhaustion time and a sighting that is not older than it."""
    return sighting if sighting is not None and sighting >= exhausted else exhausted


async def rule_holds(
    db: Session, clock: Clock, rule: AutomationRule, *, limit: int = MAX_HELD_LOOKUP
) -> list[Hold]:
    """Return the containers one health-checker rule holds now, newest hold first.

    A rule of another automation, or with no environment, holds nothing.
    """
    environment_id = rule.environment_id
    if rule.subsystem != HEALTH_CHECKER or environment_id is None:
        return []
    held = [
        target
        for target in await exhaustions(db, clock, rule.id, environment_id, limit=limit)
        if is_held(rule, target)
    ]
    sightings = await RestartHoldRepository(db, clock).sightings(
        rule.id, environment_id, [target.target_id for target in held]
    )
    return [
        Hold(
            rule_id=rule.id,
            container_id=target.target_id,
            name=target.name or target.target_id[:12],
            held_since=target.occurred_at,
            last_unhealthy_at=last_seen_unhealthy(
                target.occurred_at, sightings.get(target.target_id)
            ),
        )
        for target in held
    ]


async def environment_holds(
    db: Session, clock: Clock, environment_id: int, *, owner_id: int | None
) -> list[Hold]:
    """Return what the health-checker rules of one environment hold; `owner_id` limits the rules.

    A container held by several rules appears once per rule. The caller keeps only the
    containers its viewer may read.
    """
    rules = await AutomationRuleRepository(db, clock).page(
        limit=MAX_PAGE_SIZE,
        subsystem=HEALTH_CHECKER,
        owner_id=owner_id,
        environment_id=environment_id,
    )
    holds: list[Hold] = []
    for rule in rules.items:
        holds.extend(await rule_holds(db, clock, rule))
    return holds
