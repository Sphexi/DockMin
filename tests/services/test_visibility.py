"""The per-request predicate decided from controls already loaded; no database."""

from collections.abc import Mapping

import pytest

from app.db.repositories.resource_control import AccessLevel, ControlStanding
from app.db.repositories.visibility_filter import visible_clause
from app.services.access import Reason, ResourceType
from app.services.authorisation import Visibility
from tests.access_support import (
    ENV_A,
    STACK_NAME,
    compose_labels,
    container_id,
    standing,
    swarm_labels,
)
from tests.auth_support import STANDARD_ID

CONTAINER = container_id(1)
OTHER_CONTAINER = container_id(2)
GRANTED = standing(control_id=1, user_granted=True)
REFUSED = standing(control_id=2, is_administrators_only=True)
UNGRANTED = standing(control_id=3)
WRITABLE = standing(control_id=4, user_granted=True, access_level="write")


def visibility(
    resource_type: ResourceType = "container",
    *,
    own: Mapping[str, ControlStanding] | None = None,
    stacks: Mapping[str, ControlStanding] | None = None,
    default: AccessLevel | None = None,
    allow_all: bool = False,
    covered: frozenset[str] | None = None,
) -> Visibility:
    return Visibility(
        STANDARD_ID, ENV_A, resource_type, allow_all, default, own or {}, stacks or {}, covered
    )


def test_an_object_with_its_own_control_ignores_the_control_of_its_stack() -> None:
    held = visibility(own={CONTAINER: REFUSED}, stacks={STACK_NAME: GRANTED})

    decision = held.decide(CONTAINER, compose_labels(STACK_NAME))

    assert decision.reasons == (Reason.ADMINISTRATORS_ONLY,)
    assert decision.inherited_from is None


def test_an_object_with_its_own_grant_is_not_hidden_by_a_restrictive_stack() -> None:
    held = visibility(own={CONTAINER: GRANTED}, stacks={STACK_NAME: REFUSED})

    assert held.level(CONTAINER, compose_labels(STACK_NAME)) == "read"


@pytest.mark.parametrize(
    "labels", [compose_labels(STACK_NAME), swarm_labels(STACK_NAME), compose_labels("SHOP")]
)
def test_an_object_without_a_control_inherits_the_control_of_the_stack_its_labels_name(
    labels: dict[str, str],
) -> None:
    held = visibility(stacks={STACK_NAME: GRANTED})

    decision = held.decide(CONTAINER, labels)

    assert decision.reasons == (Reason.USER_GRANT,)
    assert decision.inherited_from == STACK_NAME


def test_an_object_labelled_for_a_stack_that_is_not_controlled_takes_the_default() -> None:
    held = visibility(stacks={STACK_NAME: GRANTED}, default="read")

    decision = held.decide(CONTAINER, compose_labels("elsewhere"))

    assert decision.reasons == (Reason.UNCONTROLLED_DEFAULT,)


def test_an_unlabelled_object_takes_the_default() -> None:
    held = visibility(stacks={STACK_NAME: GRANTED}, default=None)

    assert held.level(CONTAINER, {}) is None


@pytest.mark.parametrize("resource_type", ["volume", "network", "secret", "config"])
def test_a_label_naming_a_controlled_stack_inherits_for_every_sub_resource_type(
    resource_type: ResourceType,
) -> None:
    held = visibility(resource_type, stacks={STACK_NAME: GRANTED})

    assert held.level("object", compose_labels(STACK_NAME)) == "read"


def test_a_stack_does_not_inherit_from_a_stack_its_labels_name() -> None:
    held = visibility("stack", stacks={STACK_NAME: GRANTED})

    assert held.level("other", compose_labels(STACK_NAME)) is None


@pytest.mark.parametrize("spelling", ["MyStack", "mystack", "MYSTACK"])
def test_a_stack_is_found_under_any_spelling_of_its_name(spelling: str) -> None:
    held = visibility("stack", own={"mystack": GRANTED})

    assert held.level(spelling, {}) == "read"


def test_an_object_outside_the_narrowed_set_cannot_be_decided() -> None:
    held = visibility(own={CONTAINER: GRANTED}, default="write", covered=frozenset({CONTAINER}))

    with pytest.raises(LookupError):
        held.decide(OTHER_CONTAINER, {})


def test_an_object_inside_the_narrowed_set_is_decided_by_its_control() -> None:
    held = visibility(own={CONTAINER: REFUSED}, default="write", covered=frozenset({CONTAINER}))

    assert held.level(CONTAINER, {}) is None


def test_an_administrator_visibility_admits_everything_at_write() -> None:
    held = visibility(own={CONTAINER: REFUSED}, allow_all=True)

    decision = held.decide(CONTAINER, {})

    assert decision.level == "write"
    assert decision.reasons == (Reason.ADMINISTRATOR,)


@pytest.mark.parametrize(
    ("control", "required", "expected"),
    [
        (GRANTED, "read", True),
        (GRANTED, "write", False),
        (WRITABLE, "write", True),
        (UNGRANTED, "read", False),
    ],
)
def test_permits_requires_the_level_asked_for(
    control: ControlStanding, required: AccessLevel, expected: bool
) -> None:
    held = visibility(own={CONTAINER: control})

    assert held.permits(CONTAINER, {}, required) is expected


def test_the_sql_filter_of_an_administrator_admits_every_row() -> None:
    fragment = visibility(allow_all=True).sql_filter("item.name", "item.environment_id")

    assert (fragment.clause, fragment.params) == ("1 = 1", ())


def test_the_sql_filter_cannot_be_built_without_naming_the_environment_column() -> None:
    with pytest.raises(TypeError):
        visibility("volume").sql_filter("name")  # type: ignore[call-arg]


def test_visible_clause_cannot_be_called_without_an_environment_column() -> None:
    with pytest.raises(TypeError):
        visible_clause(  # type: ignore[call-arg]
            user_id=STANDARD_ID,
            environment_id=ENV_A,
            resource_type="volume",
            column="name",
            required="read",
            honour_default=False,
        )


@pytest.mark.parametrize(
    "name", ["", "name; DROP TABLE user", "a.b.c", "1name", "name)", "na me", "name--"]
)
@pytest.mark.parametrize("position", ["column", "environment_column"])
def test_visible_clause_refuses_a_column_that_is_not_a_plain_name(name: str, position: str) -> None:
    columns = {"column": "name", "environment_column": "environment_id"} | {position: name}

    with pytest.raises(ValueError, match="plain column name"):
        visible_clause(
            user_id=STANDARD_ID,
            environment_id=ENV_A,
            resource_type="volume",
            required="read",
            honour_default=False,
            **columns,
        )
