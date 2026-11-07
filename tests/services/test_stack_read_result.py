"""Tests for app/services/stack_read_result.py: the shared outcome type and document helpers."""

import pytest
import yaml

from app.services.stack_read_result import (
    HostReadResult,
    NeedsFile,
    Octal,
    ReadFile,
    ReadVariable,
    SecretVariables,
    cpu_amount,
    dump_document,
    duration_text,
    finish,
    is_secret_entry,
    is_secret_name,
    mask_secret_values,
)

SECOND = 1_000_000_000
MAX_VARIABLES = 100
COMPOSE = "services:\n  app:\n    image: nginx:1.27\n"


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("DB_PASSWORD", True),
        ("api_token", True),
        ("GPG_KEY", True),
        ("Credentials_file", True),
        ("CLIENT_SECRET", True),
        ("KEYCLOAK_URL", True),
        ("DB_PASS", True),
        ("MYSQL_PWD", True),
        ("PASSWD", True),
        ("PGPASS", True),
        ("BASIC_AUTH", True),
        ("PRIVATE_NOTE", True),
        ("SENTRY_DSN", True),
        ("DATABASE_URL", False),
        ("APP_ENV", False),
    ],
)
def test_is_secret_name_matches_its_words_anywhere_in_the_name(name: str, secret: bool) -> None:
    assert is_secret_name(name) is secret


@pytest.mark.parametrize(
    ("nanoseconds", "text"),
    [
        (0, "0s"),
        (30 * SECOND, "30s"),
        (90 * SECOND, "1m30s"),
        (500_000_000, "500ms"),
        (3_600 * SECOND + 1, "1h1ns"),
        (1_500_000_000, "1s500ms"),
    ],
)
def test_duration_text_writes_go_duration_strings(nanoseconds: int, text: str) -> None:
    assert duration_text(nanoseconds) == text


@pytest.mark.parametrize(
    ("nanocpus", "amount"),
    [(500_000_000, 0.5), (2_000_000_000, 2), (1_000_000, 0.001), (333_333_333, 0.333333333)],
)
def test_cpu_amount_is_the_quota_in_cpus(nanocpus: int, amount: int | float) -> None:
    assert cpu_amount(nanocpus) == amount


@pytest.mark.parametrize(
    "text",
    [
        "yes",
        "null",
        "0o17",
        "80:80",
        "1_000",
        "1.50",
        "- item",
        "a: b",
        "# note",
        "  padded ",
        "$$5",
    ],
)
def test_dumped_strings_load_back_as_the_same_string(text: str) -> None:
    dumped = dump_document({"key": text, "list": [text]})

    assert yaml.safe_load(dumped) == {"key": text, "list": [text]}


def test_dump_keeps_a_variable_reference_unquoted_and_writes_octal_modes() -> None:
    dumped = dump_document({"value": "${DB_PASSWORD}", "mode": Octal(0o440), "count": 3})

    assert dumped.splitlines() == ["value: ${DB_PASSWORD}", "mode: 0440", "count: 3"]
    assert yaml.safe_load(dumped)["mode"] == 0o440


def test_dump_repeats_shared_values_instead_of_writing_anchors() -> None:
    shared = ["a"]

    dumped = dump_document({"one": shared, "two": shared})

    assert "&" not in dumped
    assert "*" not in dumped


def test_a_secret_value_is_named_after_its_environment_variable() -> None:
    names = SecretVariables()

    reference = names.reference("web", "DB_PASSWORD", "hunter2")

    assert reference == "${DB_PASSWORD}"
    assert names.variables == (ReadVariable("DB_PASSWORD", "hunter2", True),)


def test_the_same_value_under_the_same_name_is_one_variable() -> None:
    names = SecretVariables()

    first = names.reference("web", "TOKEN", "abc")
    second = names.reference("api", "TOKEN", "abc")

    assert first == second == "${TOKEN}"
    assert len(names.variables) == 1


def test_a_different_value_under_the_same_name_takes_a_name_from_the_service() -> None:
    names = SecretVariables()

    names.reference("web", "TOKEN", "abc")
    other = names.reference("api", "TOKEN", "xyz")

    assert other == "${api_TOKEN}"


def test_a_name_outside_the_variable_grammar_is_rewritten_to_fit_it() -> None:
    names = SecretVariables()

    reference = names.reference("web", "my-secret.key", "abc")

    assert reference == "${my_secret_key}"


def test_a_value_that_cannot_be_a_variable_has_no_reference() -> None:
    assert SecretVariables().reference("web", "TOKEN", "one\ntwo") is None


def test_finish_returns_the_result_for_text_that_passes_every_check() -> None:
    outcome = finish(
        COMPOSE,
        kind="compose",
        source="generated",
        variables=[ReadVariable("TOKEN", "abc", True)],
        losses=["a loss"],
    )

    assert isinstance(outcome, HostReadResult)
    assert outcome.compose_text == COMPOSE
    assert outcome.losses == ("a loss",)


@pytest.mark.parametrize(
    ("text", "files", "variables", "reason"),
    [
        ("services:\n  app:\n    build: .\n", [], [], "refused_construct"),
        (COMPOSE + "x" * 140_000, [], [], "too_large"),
        (COMPOSE, [ReadFile("notes.txt", b"\xff\xfe\x00bad")], [], "invalid"),
        (COMPOSE, [], [ReadVariable("DOCKER_HOST", "x")], "invalid"),
        (
            COMPOSE,
            [],
            [ReadVariable(f"V{number}", "1") for number in range(MAX_VARIABLES + 1)],
            "invalid",
        ),
    ],
)
def test_finish_refuses_what_the_stack_checks_refuse(
    text: str, files: list[ReadFile], variables: list[ReadVariable], reason: str
) -> None:
    outcome = finish(text, kind="compose", source="generated", files=files, variables=variables)

    assert isinstance(outcome, NeedsFile)
    assert outcome.reason == reason


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        ("postgres://app:hunter2@db/shop", True),
        ("amqp://guest:guest@mq:5672", True),
        ("https://example.com/a@b", False),
        ("postgres://app@db/shop", False),
        ("plain", False),
    ],
)
def test_a_value_with_credentials_in_a_url_is_secret_whatever_its_name(
    value: str, secret: bool
) -> None:
    assert is_secret_entry("DATABASE_URL", value) is secret


def test_masking_hides_secret_environment_values_in_both_forms_and_leaves_the_rest() -> None:
    text = (
        "services:\n"
        "  web:\n"
        "    image: nginx:1\n"
        "    environment:\n"
        "      API_TOKEN: abc123\n"
        "      REGION: eu\n"
        "      DATABASE_URL: 'postgres://app:hunter2@db/shop'\n"
        "  worker:\n"
        "    image: nginx:1\n"
        "    environment:\n"
        "      - DB_PASS=abc123\n"
        "      - MODE=fast\n"
    )

    masked = mask_secret_values(text)

    document = yaml.safe_load(masked)["services"]
    assert document["web"]["environment"] == {
        "API_TOKEN": "(hidden)",
        "REGION": "eu",
        "DATABASE_URL": "(hidden)",
    }
    assert document["worker"]["environment"] == ["DB_PASS=(hidden)", "MODE=fast"]
    assert "abc123" not in masked and "hunter2" not in masked
    assert "image: nginx:1" in masked


def test_masking_a_text_that_is_not_compose_shows_nothing_of_it() -> None:
    assert mask_secret_values("a: [unclosed\n  API_TOKEN: abc123") == "(hidden)"


def test_a_result_matches_a_host_with_its_fingerprint_or_with_none_taken() -> None:
    fingerprinted = HostReadResult(COMPOSE, "read", host_state=("a=1",))

    assert fingerprinted.matches_host(("a=1",))
    assert not fingerprinted.matches_host(("a=2",))
    assert HostReadResult(COMPOSE, "read").matches_host(("anything",))
