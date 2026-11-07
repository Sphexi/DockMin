"""Detail the scheduler adds to the events it writes about a job."""

from app.db.engine import Session
from app.db.repositories.activity_event import DetailValue
from app.db.repositories.automation_rule import AutomationRuleRepository
from app.db.repositories.clocked import Clock
from app.db.repositories.scheduled_job import ScheduledJob


async def rule_detail(db: Session, clock: Clock, job: ScheduledJob) -> dict[str, DetailValue]:
    """Return the rule's id and name, which an event keeps after the rule is deleted."""
    if job.rule_id is None:
        return {}
    detail: dict[str, DetailValue] = {"rule_id": job.rule_id}
    rule = await AutomationRuleRepository(db, clock).get(job.rule_id)
    if rule is not None:
        detail["rule_name"] = rule.name
    return detail
