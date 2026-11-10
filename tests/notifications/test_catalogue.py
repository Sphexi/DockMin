"""The event catalogue: which activity rows raise which event types."""

from pathlib import Path

import pytest

from app.db.repositories.activity_event import Outcome
from app.notifications import catalogue
from app.notifications.catalogue import DELIVERY_FAILED_ACTION, EventType, event_type, match

REFRESHER = "environment-snapshot-refresher"
UPDATER = "container-updater"
CHECKER = "container-health-checker"
SYNCER = "backup-syncer"
SENDER = "notification-sender"
RECEIVER = "webhook-receiver"

STARTING_KEYS = frozenset(
    {
        "automation_run_failed",
        "automation_disabled",
        "update_applied",
        "update_rolled_back",
        "webhook_deployed",
        "webhook_rolled_back",
        "restart_hold_placed",
        "restart_hold_released",
        "backup_sync_failed",
        "log_export_failed",
        "environment_unreachable",
        "environment_reachable",
        "dockmin_updated",
        "dockmin_update_rolled_back",
    }
)


@pytest.mark.parametrize(
    ("action", "result", "subsystem", "keys"),
    [
        ("automation_run_failed", "failure", "stack-restarter", ["automation_run_failed"]),
        ("automation_run_partial", "failure", "stack-restarter", ["automation_run_failed"]),
        ("scheduled_job_disabled", "success", "stack-restarter", ["automation_disabled"]),
        ("container_recreated", "success", UPDATER, ["update_applied"]),
        ("stack_redeployed", "success", UPDATER, ["update_applied"]),
        ("service_updated", "success", UPDATER, ["update_applied"]),
        ("container_update_rolled_back", "success", UPDATER, ["update_rolled_back"]),
        ("service_rolled_back", "failure", UPDATER, ["update_rolled_back"]),
        ("container_recreated", "success", RECEIVER, ["webhook_deployed"]),
        ("stack_redeployed", "success", RECEIVER, ["webhook_deployed"]),
        ("service_updated", "success", RECEIVER, ["webhook_deployed"]),
        ("service_force_updated", "success", RECEIVER, ["webhook_deployed"]),
        ("container_update_rolled_back", "success", RECEIVER, ["webhook_rolled_back"]),
        ("service_rolled_back", "failure", RECEIVER, ["webhook_rolled_back"]),
        ("container_restart_budget_exhausted", "success", CHECKER, ["restart_hold_placed"]),
        ("container_restart_budget_released", "success", None, ["restart_hold_released"]),
        ("backup_uploaded", "failure", SYNCER, ["backup_sync_failed"]),
        ("backup_remote_pruned", "failure", SYNCER, ["backup_sync_failed"]),
        ("activity_log_exported", "failure", "activity-log-exporter", ["log_export_failed"]),
        ("container_log_exported", "failure", "container-log-exporter", ["log_export_failed"]),
        ("environment_unreachable", "success", REFRESHER, ["environment_unreachable"]),
        ("environment_reachable", "success", REFRESHER, ["environment_reachable"]),
        ("stack_variable_revealed", "success", None, ["stack_variable_revealed"]),
        ("instance_updated", "success", None, ["dockmin_updated"]),
        ("instance_update_rolled_back", "success", None, ["dockmin_update_rolled_back"]),
    ],
)
def test_each_activity_row_raises_its_event_type(
    action: str, result: Outcome, subsystem: str | None, keys: list[str]
) -> None:
    raised = match(action, result, subsystem)

    assert [row.key for row in raised] == keys


@pytest.mark.parametrize(
    ("action", "result", "subsystem"),
    [
        pytest.param("automation_run_failed", "success", None, id="run-failed-but-success"),
        pytest.param("container_recreated", "failure", UPDATER, id="update-that-failed"),
        pytest.param("container_recreated", "success", "stack-restarter", id="other-subsystem"),
        pytest.param(
            "container_recreated", "failure", RECEIVER, id="webhook-deployment-that-failed"
        ),
        pytest.param("service_force_updated", "success", UPDATER, id="updater-never-force-updates"),
        pytest.param("container_recreated", "success", None, id="user-recreate"),
        pytest.param("backup_uploaded", "success", SYNCER, id="backup-that-worked"),
        pytest.param("activity_log_exported", "success", None, id="export-that-worked"),
        pytest.param("environment_unreachable", "success", None, id="user-acted"),
        pytest.param("instance_updated", "failure", None, id="update-that-failed"),
        pytest.param("stack_variable_revealed", "failure", None, id="reveal-that-was-refused"),
        pytest.param("container_started", "success", None, id="unlisted-action"),
    ],
)
def test_a_row_that_misses_an_actions_result_or_subsystem_raises_nothing(
    action: str, result: Outcome, subsystem: str | None
) -> None:
    assert match(action, result, subsystem) == ()


@pytest.mark.parametrize("result", ["success", "failure"])
def test_a_delivery_failure_raises_no_event_type(result: Outcome) -> None:
    assert match(DELIVERY_FAILED_ACTION, result, SENDER) == ()


def test_the_starting_event_types_are_all_catalogued() -> None:
    keys = {row.key for row in catalogue.CATALOGUE}

    assert keys >= STARTING_KEYS


def test_every_key_is_listed_once_and_no_row_lists_the_delivery_failure() -> None:
    keys = [row.key for row in catalogue.CATALOGUE]

    assert len(keys) == len(set(keys))
    assert all(DELIVERY_FAILED_ACTION not in row.actions for row in catalogue.CATALOGUE)


def test_an_event_type_is_found_by_its_key() -> None:
    assert event_type("update_applied") is not None
    assert event_type("not_catalogued") is None


def test_a_row_listing_the_delivery_failure_is_refused() -> None:
    with pytest.raises(ValueError, match="never raises"):
        EventType("loop", "Loop", frozenset({DELIVERY_FAILED_ACTION}))


@pytest.mark.parametrize("key", ["", "Upper", "with-hyphen", "digits1", "a" * 65])
def test_a_key_outside_the_stored_alphabet_is_refused(key: str) -> None:
    with pytest.raises(ValueError, match="key"):
        EventType(key, "Label", frozenset({"anything"}))


def test_a_row_listing_no_action_is_refused() -> None:
    with pytest.raises(ValueError, match="action"):
        EventType("empty", "Empty", frozenset())


def test_a_catalogue_with_a_repeated_key_cannot_be_imported() -> None:
    source = Path(catalogue.__file__).read_text(encoding="utf-8")
    repeated = source.replace('"automation_disabled",', '"automation_run_failed",', 1)
    assert repeated != source

    with pytest.raises(ValueError, match="listed once"):
        exec(compile(repeated, "repeated_catalogue", "exec"), {"__name__": "repeated"})


def test_a_row_added_to_the_catalogue_is_matched_and_found_without_another_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added = EventType(
        "disk_nearly_full", "Disk nearly full", frozenset({"disk_check"}), result="failure"
    )
    monkeypatch.setattr(catalogue, "CATALOGUE", (*catalogue.CATALOGUE, added))

    assert match("disk_check", "failure", None) == (added,)
    assert event_type("disk_nearly_full") == added
    assert match("disk_check", "success", None) == ()
