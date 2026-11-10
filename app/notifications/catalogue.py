"""The event types a notification channel can subscribe to, and the activity rows each matches."""

import re
from dataclasses import dataclass
from typing import Final

from app.db.repositories.activity_event import Outcome

AUTOMATION_RUN_FAILED_ACTION: Final = "automation_run_failed"
AUTOMATION_RUN_PARTIAL_ACTION: Final = "automation_run_partial"
ENVIRONMENT_UNREACHABLE_ACTION: Final = "environment_unreachable"
ENVIRONMENT_REACHABLE_ACTION: Final = "environment_reachable"
DELIVERY_FAILED_ACTION: Final = "notification_delivery_failed"

_KEY: Final = re.compile(r"[a-z_]{1,64}")


@dataclass(frozen=True, slots=True)
class EventType:
    """One subscribable event type and the activity rows that raise it.

    A row matches when its action is listed, its result equals `result` if that is set, and its
    actor subsystem equals `subsystem` if that is set. The key is stored in subscriptions and is
    never renamed.
    """

    key: str
    label: str
    actions: frozenset[str]
    result: Outcome | None = None
    subsystem: str | None = None

    def __post_init__(self) -> None:
        if _KEY.fullmatch(self.key) is None:
            raise ValueError("an event type key is 1 to 64 characters of a-z and underscore")
        if not self.actions:
            raise ValueError("an event type lists at least one action")
        if DELIVERY_FAILED_ACTION in self.actions:
            raise ValueError("a delivery failure never raises a notification")


CATALOGUE: Final[tuple[EventType, ...]] = (
    EventType(
        "automation_run_failed",
        "Automation run failed",
        frozenset({AUTOMATION_RUN_FAILED_ACTION, AUTOMATION_RUN_PARTIAL_ACTION}),
        result="failure",
    ),
    EventType(
        "automation_disabled",
        "Automation disabled after repeated failures",
        frozenset({"scheduled_job_disabled"}),
    ),
    EventType(
        "update_applied",
        "Update applied",
        frozenset({"container_recreated", "stack_redeployed", "service_updated"}),
        result="success",
        subsystem="container-updater",
    ),
    EventType(
        "update_rolled_back",
        "Update rolled back",
        frozenset({"container_update_rolled_back", "service_rolled_back"}),
        subsystem="container-updater",
    ),
    EventType(
        "webhook_deployed",
        "Webhook deployment applied",
        frozenset(
            {"container_recreated", "stack_redeployed", "service_updated", "service_force_updated"}
        ),
        result="success",
        subsystem="webhook-receiver",
    ),
    EventType(
        "webhook_rolled_back",
        "Webhook deployment rolled back",
        frozenset({"container_update_rolled_back", "service_rolled_back"}),
        subsystem="webhook-receiver",
    ),
    EventType(
        "restart_hold_placed",
        "Restart hold placed",
        frozenset({"container_restart_budget_exhausted"}),
        subsystem="container-health-checker",
    ),
    EventType(
        "restart_hold_released",
        "Restart hold released",
        frozenset({"container_restart_budget_released"}),
        result="success",
    ),
    EventType(
        "backup_sync_failed",
        "Backup or object storage sync failed",
        frozenset({"backup_uploaded", "backup_remote_pruned"}),
        result="failure",
        subsystem="backup-syncer",
    ),
    EventType(
        "log_export_failed",
        "Activity or container log export failed",
        frozenset({"activity_log_exported", "container_log_exported"}),
        result="failure",
    ),
    EventType(
        "environment_unreachable",
        "Environment unreachable",
        frozenset({ENVIRONMENT_UNREACHABLE_ACTION}),
        subsystem="environment-snapshot-refresher",
    ),
    EventType(
        "environment_reachable",
        "Environment reachable again",
        frozenset({ENVIRONMENT_REACHABLE_ACTION}),
        subsystem="environment-snapshot-refresher",
    ),
    EventType(
        "stack_variable_revealed",
        "Stack variable revealed",
        frozenset({"stack_variable_revealed"}),
        result="success",
    ),
    EventType(
        "dockmin_updated",
        "dockmin updated",
        frozenset({"instance_updated"}),
        result="success",
    ),
    EventType(
        "dockmin_update_rolled_back",
        "dockmin update rolled back",
        frozenset({"instance_update_rolled_back"}),
    ),
)

if len({row.key for row in CATALOGUE}) != len(CATALOGUE):
    raise ValueError("an event type key is listed once")


def event_type(key: str) -> EventType | None:
    """Return the catalogue row with this key, or None for a key that is not catalogued."""
    return next((row for row in CATALOGUE if row.key == key), None)


def match(action: str, result: Outcome, actor_subsystem: str | None) -> tuple[EventType, ...]:
    """Return the rows an activity event raises, in catalogue order."""
    return tuple(
        row
        for row in CATALOGUE
        if action in row.actions
        and (row.result is None or row.result == result)
        and (row.subsystem is None or row.subsystem == actor_subsystem)
    )
