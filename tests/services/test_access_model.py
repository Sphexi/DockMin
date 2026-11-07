"""The vocabulary and the one evaluation rule every authorisation path shares; no database."""

import pytest

from app.auth.errors import ValidationFailed
from app.db.repositories.resource_control import AccessLevel, ControlStanding
from app.services.access import (
    COMPOSE_PROJECT_LABEL,
    SWARM_NAMESPACE_LABEL,
    Decision,
    Reason,
    ResourceType,
    SubmittedLabels,
    evaluate,
    is_canonical_id,
    satisfies,
    stack_of,
)
from app.services.authorisation import UnavailableInspector
from tests.access_support import ENV_A, container_id, service_id, standing

CONTROL_ID = 7
NETWORK_HEX_ID = "b" * 64
NETWORK_SWARM_ID = "z0123456789abcdefghijklmn"
WRITE_CONTROL = standing(control_id=CONTROL_ID, access_level="write", user_granted=True)


@pytest.mark.parametrize(
    ("held", "required", "expected"),
    [
        (None, "read", False),
        (None, "write", False),
        ("read", "read", True),
        ("read", "write", False),
        ("write", "read", True),
        ("write", "write", True),
    ],
)
def test_satisfies_compares_the_held_level_with_the_required_one(
    held: AccessLevel | None, required: AccessLevel, expected: bool
) -> None:
    assert satisfies(held, required) is expected


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({COMPOSE_PROJECT_LABEL: "WebApp"}, "webapp"),
        ({SWARM_NAMESPACE_LABEL: "MyStack"}, "mystack"),
        ({COMPOSE_PROJECT_LABEL: "first", SWARM_NAMESPACE_LABEL: "second"}, "first"),
        ({COMPOSE_PROJECT_LABEL: "", SWARM_NAMESPACE_LABEL: "Fallback"}, "fallback"),
        ({COMPOSE_PROJECT_LABEL: ""}, None),
        ({"com.example.unrelated": "shop"}, None),
        ({}, None),
    ],
)
def test_stack_of_returns_the_canonical_key_of_the_stack_the_labels_name(
    labels: dict[str, str], expected: str | None
) -> None:
    assert stack_of(labels) == expected


@pytest.mark.parametrize(
    ("resource_type", "resource_id"),
    [
        ("container", container_id(1)),
        ("service", service_id(1)),
        ("volume", "data"),
        ("network", NETWORK_HEX_ID),
        ("network", NETWORK_SWARM_ID),
        ("volume", "a" * 255),
        ("secret", service_id(2)),
        ("config", service_id(3)),
        ("stack", "MyStack"),
    ],
)
def test_is_canonical_id_accepts_the_form_each_type_is_keyed_by(
    resource_type: ResourceType, resource_id: str
) -> None:
    assert is_canonical_id(resource_type, resource_id)


@pytest.mark.parametrize(
    ("resource_type", "resource_id"),
    [
        ("container", container_id(1)[:12]),
        ("container", "web"),
        ("container", f" {container_id(1)}"),
        ("container", f"{container_id(1)} "),
        ("container", f"{container_id(1)}\n"),
        ("container", "A" * 64),
        ("container", f"{container_id(1)}0"),
        ("container", container_id(1)[:-1]),
        ("container", ""),
        ("service", container_id(1)),
        ("secret", "Any Name"),
        ("secret", service_id(2)[:-1]),
        ("secret", "A" * 25),
        ("config", "any-name"),
        ("config", f"{service_id(3)} "),
        ("service", "web_frontend"),
        ("service", f"{service_id(1)} "),
        ("network", "backend"),
        ("network", NETWORK_HEX_ID[:12]),
        ("network", NETWORK_HEX_ID.upper()),
        ("network", NETWORK_HEX_ID + "0"),
        ("network", NETWORK_SWARM_ID[:-1]),
        ("network", f"{NETWORK_SWARM_ID[:-1]}_"),
        ("volume", ""),
        ("volume", "-data"),
        ("volume", "../data"),
        ("volume", "a" * 256),
        ("volume", "data "),
    ],
)
def test_is_canonical_id_rejects_a_short_id_a_name_and_padding(
    resource_type: ResourceType, resource_id: str
) -> None:
    assert not is_canonical_id(resource_type, resource_id)


@pytest.mark.parametrize(
    "key",
    [
        COMPOSE_PROJECT_LABEL,
        SWARM_NAMESPACE_LABEL,
        COMPOSE_PROJECT_LABEL.upper(),
        SWARM_NAMESPACE_LABEL.title(),
        f" {COMPOSE_PROJECT_LABEL} ",
        f"\t{SWARM_NAMESPACE_LABEL}\n",
    ],
)
def test_submitted_labels_refuses_either_inheritance_label_in_any_spelling(key: str) -> None:
    with pytest.raises(ValidationFailed) as refused:
        SubmittedLabels({"com.example.harmless": "x", key: "shop"})

    assert "labels" in refused.value.fields


def test_submitted_labels_accepts_labels_that_place_an_object_in_no_stack() -> None:
    labels = SubmittedLabels({"com.example.owner": "team", "com.docker.compose.service": "web"})

    assert labels.values["com.example.owner"] == "team"


def test_an_uncontrolled_object_is_denied_when_the_default_grants_nothing() -> None:
    decision = evaluate(None, default=None)

    assert decision == Decision(None, (Reason.UNCONTROLLED_DENIED,))
    assert not decision.visible


@pytest.mark.parametrize("default", ["read", "write"])
def test_an_uncontrolled_object_takes_the_default_level(default: AccessLevel) -> None:
    decision = evaluate(None, default=default)

    assert decision == Decision(default, (Reason.UNCONTROLLED_DEFAULT,))


def test_an_administrators_only_control_hides_the_object_whatever_else_is_granted() -> None:
    held = standing(
        control_id=CONTROL_ID,
        is_administrators_only=True,
        is_owner=True,
        user_granted=True,
        team_granted=True,
    )

    decision = evaluate(held, default="write")

    assert decision == Decision(None, (Reason.ADMINISTRATORS_ONLY,), CONTROL_ID)


def test_a_control_granting_this_user_nothing_hides_the_object_despite_a_permissive_default() -> (
    None
):
    decision = evaluate(standing(control_id=CONTROL_ID), default="write")

    assert decision == Decision(None, (Reason.NOT_GRANTED,), CONTROL_ID)


@pytest.mark.parametrize(
    ("held", "reason"),
    [
        (standing(is_public=True), Reason.PUBLIC),
        (standing(is_owner=True), Reason.OWNER),
        (standing(user_granted=True), Reason.USER_GRANT),
        (standing(team_granted=True), Reason.TEAM_GRANT),
    ],
)
def test_each_way_of_holding_access_is_reported_as_its_own_reason(
    held: ControlStanding, reason: Reason
) -> None:
    decision = evaluate(held, default=None)

    assert decision.reasons == (reason,)
    assert decision.level == "read"


def test_every_applicable_reason_is_reported_in_a_fixed_order() -> None:
    held = standing(is_public=True, is_owner=True, user_granted=True, team_granted=True)

    decision = evaluate(held, default=None)

    assert decision.reasons == (
        Reason.PUBLIC,
        Reason.OWNER,
        Reason.USER_GRANT,
        Reason.TEAM_GRANT,
    )


@pytest.mark.parametrize("default", [None, "write"])
def test_the_controls_own_level_applies_instead_of_the_default(default: AccessLevel | None) -> None:
    decision = evaluate(standing(user_granted=True, access_level="read"), default=default)

    assert decision.level == "read"


def test_a_decision_made_through_a_stack_names_the_stack() -> None:
    decision = evaluate(WRITE_CONTROL, default=None, inherited_from="shop")

    assert decision.inherited_from == "shop"
    assert decision.control_id == CONTROL_ID
    assert decision.visible


async def test_the_placeholder_inspector_fails_the_request_instead_of_reporting_no_labels() -> None:
    with pytest.raises(RuntimeError):
        await UnavailableInspector().labels_of(ENV_A, "container", container_id(1))
