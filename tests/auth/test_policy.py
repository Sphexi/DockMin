"""Tests for app/auth/policy.py."""

import pytest

from app.auth.errors import ValidationFailed
from app.auth.passwords import MAX_PASSWORD_LENGTH
from app.auth.policy import PasswordPolicy, check_username

MIN_LENGTH = 12
USERNAME = "operator"
ACCEPTABLE = "twelve-chars"
LENGTH_ONLY = PasswordPolicy(min_length=MIN_LENGTH, required_classes=0)
THREE_CLASSES = PasswordPolicy(min_length=MIN_LENGTH, required_classes=3)

PROBLEM_CASES = [
    pytest.param(LENGTH_ONLY, "x" * (MIN_LENGTH - 1), "at least", id="too-short"),
    pytest.param(LENGTH_ONLY, "x" * (MAX_PASSWORD_LENGTH + 1), "at most", id="too-long"),
    pytest.param(LENGTH_ONLY, "OPERATOR", "same as the username", id="equals-username-any-case"),
    pytest.param(THREE_CLASSES, "alllowercase1", "Mix at least 3", id="two-classes-of-three"),
]
ACCEPTED_CASES = [
    pytest.param(LENGTH_ONLY, "x" * MIN_LENGTH, id="exactly-the-minimum"),
    pytest.param(THREE_CLASSES, "Lower-Upper-1", id="three-classes"),
]
BAD_USERNAMES = [
    pytest.param("ab", id="too-short"),
    pytest.param("a" * 65, id="too-long"),
    pytest.param("-leading", id="leading-symbol"),
    pytest.param("has space", id="space"),
    pytest.param("semi;colon", id="punctuation"),
]
GOOD_USERNAMES = ["abc", "a.b-c_d@e", "A" + "b" * 63]


@pytest.mark.parametrize(("policy", "password", "fragment"), PROBLEM_CASES)
def test_check_rejects_a_password_that_breaks_a_rule_naming_the_rule(
    policy: PasswordPolicy, password: str, fragment: str
) -> None:
    with pytest.raises(ValidationFailed) as raised:
        policy.check(password, USERNAME)

    assert fragment in raised.value.fields["password"]


@pytest.mark.parametrize(("policy", "password"), ACCEPTED_CASES)
def test_check_accepts_a_password_that_meets_every_rule(
    policy: PasswordPolicy, password: str
) -> None:
    policy.check(password, USERNAME)


def test_check_reports_every_broken_rule_at_once() -> None:
    problems = THREE_CLASSES.problems(USERNAME.upper(), USERNAME)

    assert len(problems) == 3


def test_check_attaches_the_message_to_the_named_field() -> None:
    with pytest.raises(ValidationFailed) as raised:
        LENGTH_ONLY.check("short", USERNAME, field_name="new_password")

    assert set(raised.value.fields) == {"new_password"}


@pytest.mark.parametrize("username", BAD_USERNAMES)
def test_check_username_rejects_a_name_outside_the_pattern(username: str) -> None:
    with pytest.raises(ValidationFailed) as raised:
        check_username(username)

    assert set(raised.value.fields) == {"username"}


@pytest.mark.parametrize("username", GOOD_USERNAMES)
def test_check_username_accepts_a_name_inside_the_pattern(username: str) -> None:
    check_username(username)
