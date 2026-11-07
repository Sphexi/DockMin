"""container-updater decisions that need no host: verdicts, refusal text and parameters."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.auth.errors import ValidationFailed
from app.services.image_updates import UpdateStatus
from app.services.pull_credentials import (
    BindingCredentialMissing,
    CredentialChoiceRequired,
    CredentialSetMismatch,
)
from app.services.registry_resolution import CredentialSetUnavailable
from app.subsystems.acting_run import NAMES_LISTING_LIMIT, listing
from app.subsystems.container_health_checker import ContainerHealthChecker
from app.subsystems.container_updater import (
    REFUSALS,
    SIGNATURE_LIMIT,
    ContainerUpdater,
    UpdaterParameters,
    _pull_refusal,
    _refusal_reason,
    _verdict_of,
    refusal_signature,
)
from app.subsystems.selectors import (
    MAX_TARGET_NAMES,
    NOT_OFFERED,
    compose_managed,
    parse_selector_text,
)
from app.subsystems.stack_restarter import StackRestarter

SELECTORS = {
    "container": '{"kind":"container","names":["web"]}',
    "labels": '{"kind":"labels","labels":[["app","web"]]}',
    "stack": '{"kind":"stack","names":["shop"]}',
    "service": '{"kind":"service","names":["web"]}',
    "environment": '{"kind":"environment"}',
}
CHECKED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
RUNNING = "a" * 64
LOCAL = "b" * 64
PROJECT_LABEL = "com.docker.compose.project"
LONGEST_NAME = 128
MANY_NAMES = MAX_TARGET_NAMES


def status(
    state: str, reason: str | None = None, local_image_id: str | None = None
) -> UpdateStatus:
    return UpdateStatus(
        "registry.example.com/app:1",
        "registry.example.com/app:1",
        state,  # type: ignore[arg-type]
        reason,
        None,
        None,
        CHECKED,
        local_image_id,
    )


@pytest.mark.parametrize(
    ("update", "running", "verdict"),
    [
        (status("update_available"), RUNNING, "update_available"),
        (status("update_available"), None, "update_available"),
        (status("up_to_date", local_image_id=LOCAL), RUNNING, "local_newer"),
        (status("up_to_date", local_image_id=LOCAL), LOCAL, None),
        (status("up_to_date", local_image_id=LOCAL), None, None),
        (status("up_to_date", local_image_id=None), RUNNING, None),
        (status("unknown", "timeout", local_image_id=LOCAL), RUNNING, None),
        (status("pinned", local_image_id=LOCAL), RUNNING, None),
        (status("not_bound", local_image_id=LOCAL), RUNNING, None),
    ],
    ids=[
        "newer-on-the-registry",
        "newer-on-the-registry-unknown-running-image",
        "tag-moved-locally",
        "same-image",
        "running-image-unknown",
        "no-local-image",
        "unknown-is-never-an-update",
        "pinned",
        "not-bound",
    ],
)
def test_a_verdict_comes_from_the_state_and_the_image_ids(
    update: UpdateStatus, running: str | None, verdict: str | None
) -> None:
    assert _verdict_of(update, running) == verdict


@pytest.mark.parametrize(
    ("update", "refusal"),
    [
        (status("not_bound"), "not_bound"),
        (status("unknown", "credential_missing"), "credential_missing"),
        (status("unknown", "credential_mismatch"), "credential_mismatch"),
        (status("unknown", "credential_not_available"), "credential_unavailable"),
        (status("unknown", "timeout"), None),
        (status("unknown", "registry_not_configured"), None),
        (status("up_to_date"), None),
        (status("update_available"), None),
    ],
)
def test_only_a_credential_problem_is_a_refusal(update: UpdateStatus, refusal: str | None) -> None:
    assert _refusal_reason(update) == refusal


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (BindingCredentialMissing(), "credential_missing"),
        (CredentialChoiceRequired(()), "not_bound"),
        (CredentialSetMismatch(), "credential_mismatch"),
        (CredentialSetUnavailable("not_granted_user"), "credential_unavailable"),
    ],
)
def test_a_credential_error_at_pull_time_maps_to_the_refusal_of_the_same_name(
    error: Exception, reason: str
) -> None:
    refusal = _pull_refusal(error)

    assert refusal is not None and refusal.reason == reason
    assert REFUSALS[reason] is type(refusal)


@pytest.mark.parametrize("error", [RuntimeError("x"), ValueError("y"), TimeoutError()])
def test_any_other_error_at_pull_time_is_not_a_refusal(error: Exception) -> None:
    assert _pull_refusal(error) is None


def test_every_refusal_carries_a_fixed_message_without_registry_text() -> None:
    assert set(REFUSALS) == {
        "credential_missing",
        "not_bound",
        "credential_mismatch",
        "credential_unavailable",
    }
    for refusal in REFUSALS.values():
        assert str(refusal()) == refusal.message


def test_the_refusal_signature_is_the_same_for_the_same_refusals_in_any_order() -> None:
    forward = refusal_signature([("a:1", "not_bound"), ("b:1", "credential_missing")])
    backward = refusal_signature([("b:1", "credential_missing"), ("a:1", "not_bound")])

    assert forward == backward == "a:1=not_bound,b:1=credential_missing"
    assert refusal_signature([("a:1", "credential_missing")]) != forward


def test_a_very_long_refusal_signature_is_cut_to_the_limit() -> None:
    long = refusal_signature([(f"registry.example.com/app{n}:1", "not_bound") for n in range(100)])

    assert len(long) == SIGNATURE_LIMIT


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "apply"},
        {"verify_seconds": -1},
        {"verify_seconds": 301},
        {"unknown_field": 1},
        {"require_healthy": "maybe"},
    ],
)
def test_parameters_outside_what_the_form_offers_are_refused(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UpdaterParameters(**arguments)


def test_parameters_default_to_a_report_that_watches_thirty_seconds_for_health() -> None:
    parameters = UpdaterParameters()

    assert (parameters.action, parameters.verify_seconds, parameters.require_healthy) == (
        "report",
        30,
        True,
    )
    assert UpdaterParameters(verify_seconds=0).verify_seconds == 0
    assert UpdaterParameters(verify_seconds=300).verify_seconds == 300


OFFERED = [
    (ContainerUpdater, "container"),
    (ContainerUpdater, "labels"),
    (ContainerUpdater, "stack"),
    (ContainerUpdater, "service"),
    (ContainerHealthChecker, "container"),
    (ContainerHealthChecker, "labels"),
    (ContainerHealthChecker, "stack"),
    (ContainerHealthChecker, "environment"),
    (StackRestarter, "container"),
    (StackRestarter, "stack"),
]
NOT_OFFERED_KINDS = [
    (ContainerUpdater, "environment"),
    (ContainerHealthChecker, "service"),
    (StackRestarter, "service"),
    (StackRestarter, "environment"),
    (StackRestarter, "labels"),
]


@pytest.mark.parametrize(("subsystem", "kind"), OFFERED)
def test_each_automation_accepts_the_target_kinds_it_can_act_on(subsystem: Any, kind: str) -> None:
    parsed = parse_selector_text(SELECTORS[kind], subsystem.target_kinds)

    assert parsed.kind == kind


@pytest.mark.parametrize(("subsystem", "kind"), NOT_OFFERED_KINDS)
def test_each_automation_refuses_the_target_kinds_it_cannot_act_on(
    subsystem: Any, kind: str
) -> None:
    with pytest.raises(ValidationFailed) as refused:
        parse_selector_text(SELECTORS[kind], subsystem.target_kinds)

    assert refused.value.fields["target_selector"] == NOT_OFFERED


def test_only_the_updater_acts_on_swarm_stacks() -> None:
    assert ContainerUpdater.stack_kinds == {"compose", "swarm"}
    assert ContainerHealthChecker.stack_kinds == {"compose"}
    assert StackRestarter.stack_kinds == {"compose"}


def test_the_notes_of_targets_that_act_on_nothing_are_cut_at_a_name_boundary() -> None:
    names = [f"{number:02}".ljust(LONGEST_NAME, "x") for number in range(MANY_NAMES)]
    notes = [str(compose_managed(name, {PROJECT_LABEL: "shop"})) for name in names]

    text = listing(notes, NAMES_LISTING_LIMIT, "; ")

    kept, _, rest = text.rpartition(" and ")
    shown = kept.split("; ")
    assert len(text) <= NAMES_LISTING_LIMIT
    assert shown == notes[: len(shown)]
    assert rest == f"{MANY_NAMES - len(shown)} more"
