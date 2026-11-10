"""Quick add qualification: which automations take which object, and the target they would hold."""

import itertools
import json
from typing import Any, cast

import pytest

from app.auth.errors import ValidationFailed
from app.db.repositories.automation_rule import AutomationRule
from app.services.access import COMPOSE_PROJECT_LABEL
from app.services.automation_quick_add import (
    Addition,
    Declined,
    ObjectKind,
    QuickAddObject,
    names_in,
    names_services,
    qualify,
)
from app.services.container_derivation import SWARM_SERVICE_LABEL
from app.subsystems.base import Subsystem, SubsystemDeps
from app.subsystems.container_health_checker import ContainerHealthChecker
from app.subsystems.container_log_exporter import ContainerLogExporter
from app.subsystems.container_updater import ContainerUpdater
from app.subsystems.stack_restarter import StackRestarter
from app.subsystems.webhook_receiver import WebhookReceiver

ENVIRONMENT = 1
STAMP = "2000-01-01T00:00:00Z"
OTHER_ENVIRONMENT = 2
UPDATER = "container-updater"
WEBHOOK = "webhook-receiver"
HEALTH = "container-health-checker"
RESTARTER = "stack-restarter"
EXPORTER = "container-log-exporter"
SUBSYSTEM_NAMES = (UPDATER, WEBHOOK, HEALTH, RESTARTER, EXPORTER)
SELECTOR_KINDS = ("container", "stack", "service", "labels", "environment", "service_labels")
OBJECT_KINDS: tuple[ObjectKind, ...] = ("container", "stack", "swarm_stack", "service")
SELECTOR_OF_OBJECT = {
    "container": "container",
    "stack": "stack",
    "swarm_stack": "stack",
    "service": "service",
}
TAKERS = {
    "container": SUBSYSTEM_NAMES,
    "stack": SUBSYSTEM_NAMES,
    "swarm_stack": (UPDATER, WEBHOOK, EXPORTER),
    "service": (UPDATER, WEBHOOK, EXPORTER),
}
OFFERED = frozenset(
    (kind, subsystem, SELECTOR_OF_OBJECT[kind])
    for kind, subsystems in TAKERS.items()
    for subsystem in subsystems
)
MATRIX = list(itertools.product(OBJECT_KINDS, SUBSYSTEM_NAMES, SELECTOR_KINDS))
EXISTING = "web"
ADDED = "api"
SERVICE_ID = "s" * 25
FULL_NAMES = [f"name{number:02d}" for number in range(20)]


def build(factory: type[Subsystem]) -> Subsystem:
    return factory(cast(SubsystemDeps, None))


@pytest.fixture(scope="module")
def handlers() -> dict[str, Subsystem]:
    return {
        UPDATER: build(ContainerUpdater),
        WEBHOOK: build(WebhookReceiver),
        HEALTH: build(ContainerHealthChecker),
        RESTARTER: build(StackRestarter),
        EXPORTER: build(ContainerLogExporter),
    }


def selector_of(kind: str, *names: str) -> str:
    body: dict[str, Any] = {"kind": kind}
    if kind in ("container", "stack", "service"):
        body["names"] = list(names or (EXISTING,))
    if kind in ("labels", "service_labels"):
        body["labels"] = [["app", "web"]]
    return json.dumps(body, separators=(",", ":"))


CONTAINERS = selector_of("container")
STACKS = selector_of("stack")
SERVICES = selector_of("service")


def rule_of(
    subsystem: str,
    selector: str,
    *,
    environment_id: int | None = ENVIRONMENT,
    enabled: bool = True,
) -> AutomationRule:
    return AutomationRule(
        id=1,
        name="Rule",
        subsystem=subsystem,
        environment_id=environment_id,
        target_selector=selector,
        parameters="{}",
        is_enabled=enabled,
        paused_until=None,
        created_by_user_id=1,
        created_at=STAMP,
        updated_at=STAMP,
    )


def object_of(
    kind: ObjectKind, name: str = ADDED, labels: dict[str, str] | None = None
) -> QuickAddObject:
    return QuickAddObject(ENVIRONMENT, kind, name, name, labels or {})


def reason_of(verdict: Addition | Declined) -> str | None:
    return verdict.reason if isinstance(verdict, Declined) else None


@pytest.mark.parametrize(("kind", "subsystem", "selector_kind"), MATRIX)
def test_an_automation_is_offered_only_for_its_matching_selector_kind_and_subsystem_kinds(
    handlers: dict[str, Subsystem], kind: ObjectKind, subsystem: str, selector_kind: str
) -> None:
    rule = rule_of(subsystem, selector_of(selector_kind))

    verdict = qualify(handlers[subsystem], rule, object_of(kind))

    assert isinstance(verdict, Addition) == ((kind, subsystem, selector_kind) in OFFERED)


@pytest.mark.parametrize(
    ("subsystem", "reason"),
    [
        (UPDATER, "compose_managed"),
        (WEBHOOK, "compose_managed"),
        (HEALTH, None),
        (RESTARTER, None),
        (EXPORTER, None),
    ],
)
def test_a_compose_managed_container_is_refused_by_the_updater_and_the_webhook_receiver_only(
    handlers: dict[str, Subsystem], subsystem: str, reason: str | None
) -> None:
    rule = rule_of(subsystem, selector_of("container"))
    managed = object_of("container", labels={COMPOSE_PROJECT_LABEL: "shop"})

    verdict = qualify(handlers[subsystem], rule, managed)

    assert reason_of(verdict) == reason


@pytest.mark.parametrize(
    ("selector", "rule_changes", "obj", "reason"),
    [
        (
            CONTAINERS,
            {"environment_id": OTHER_ENVIRONMENT},
            object_of("container"),
            "other_environment",
        ),
        (CONTAINERS, {"enabled": False}, object_of("container"), "rule_disabled"),
        (
            CONTAINERS,
            {},
            object_of("container", labels={SWARM_SERVICE_LABEL: SERVICE_ID}),
            "swarm_task",
        ),
        (CONTAINERS, {}, object_of("container", EXISTING), "already_target"),
        (selector_of("stack", "Shop"), {}, object_of("stack", "shop"), "already_target"),
        (selector_of("stack", "SHOP"), {}, object_of("swarm_stack", "shop"), "already_target"),
        (SERVICES, {}, object_of("service", EXISTING), "already_target"),
        (selector_of("container", *FULL_NAMES), {}, object_of("container"), "target_full"),
        (CONTAINERS, {}, object_of("container", "_bad"), "name_not_valid"),
        (STACKS, {}, object_of("stack", "shop.v2"), "name_not_valid"),
        (SERVICES, {}, object_of("service", "-web"), "name_not_valid"),
    ],
)
def test_an_automation_that_cannot_take_the_object_is_declined_with_the_reason(
    handlers: dict[str, Subsystem],
    selector: str,
    rule_changes: dict[str, Any],
    obj: QuickAddObject,
    reason: str,
) -> None:
    rule = rule_of(UPDATER, selector, **rule_changes)

    verdict = qualify(handlers[UPDATER], rule, obj)

    assert isinstance(verdict, Declined)
    assert verdict.reason == reason
    assert verdict.message


def test_a_target_with_nineteen_names_can_still_take_one_more(
    handlers: dict[str, Subsystem],
) -> None:
    nineteen = rule_of(UPDATER, selector_of("container", *FULL_NAMES[:19]))

    verdict = qualify(handlers[UPDATER], nineteen, object_of("container"))

    assert isinstance(verdict, Addition)
    assert len(verdict.after) == 20


def test_an_addition_holds_the_stored_names_and_the_new_one_in_canonical_order(
    handlers: dict[str, Subsystem],
) -> None:
    rule = rule_of(UPDATER, selector_of("container", "web", "db"))

    verdict = qualify(handlers[UPDATER], rule, object_of("container", ADDED))

    assert isinstance(verdict, Addition)
    assert verdict.before == ("db", "web")
    assert verdict.after == ("api", "db", "web")
    assert json.loads(verdict.selector) == {"kind": "container", "names": ["api", "db", "web"]}


def test_an_automation_whose_handler_refuses_the_extended_selector_is_declined_with_its_message(
    handlers: dict[str, Subsystem], monkeypatch: pytest.MonkeyPatch
) -> None:
    message = "Use at most 4 KiB."

    def refuse(text: str) -> str:
        raise ValidationFailed({"target_selector": message})

    monkeypatch.setattr(handlers[UPDATER], "parse_selector", refuse)

    verdict = qualify(
        handlers[UPDATER], rule_of(UPDATER, selector_of("container")), object_of("container")
    )

    assert isinstance(verdict, Declined)
    assert (verdict.reason, verdict.message) == ("target_not_valid", message)


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        (selector_of("container", "web", "db"), ("db", "web")),
        (selector_of("labels"), ()),
        (selector_of("environment"), ()),
        ("", ()),
        ("not json", ()),
    ],
)
def test_the_names_a_stored_selector_lists_are_empty_unless_it_names_objects(
    selector: str, expected: tuple[str, ...]
) -> None:
    assert names_in(selector) == expected


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        (selector_of("service"), True),
        (selector_of("container"), False),
        (selector_of("service_labels"), False),
        ("not json", False),
    ],
)
def test_a_stored_selector_names_services_only_when_it_lists_service_names(
    selector: str, expected: bool
) -> None:
    assert names_services(selector) is expected
