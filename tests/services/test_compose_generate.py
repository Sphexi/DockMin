"""Tests for app/services/compose_generate.py: a Compose file written from inspect data."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.services.compose_generate import KNOWN_LOSSES, generate_compose
from app.services.stack_document import precheck
from app.services.stack_read_result import HostReadResult, NeedsFile

FIXTURES = Path(__file__).parents[1] / "fixtures" / "stack_read"
PROJECT = "plain"
IMAGE_ID = "sha256:" + "a1" * 32
IMAGE_REFERENCE = "nginx:1.27"
CONTAINER_ID = "0123456789ab" + "0" * 52
PATH_ENTRY = "PATH=/usr/bin"


def recorded(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / f"compose_{name}.json").read_text(encoding="utf-8")
    )
    return loaded


def container(
    service: str = "app", *, env: list[str] | None = None, labels: dict[str, str] | None = None
) -> dict[str, Any]:
    """The smallest inspect document the generator accepts, for one service of PROJECT."""
    return {
        "Id": CONTAINER_ID,
        "Name": f"/{PROJECT}-{service}-1",
        "Image": IMAGE_ID,
        "Config": {
            "Image": IMAGE_REFERENCE,
            "Env": env or [PATH_ENTRY],
            "Labels": {
                "com.docker.compose.project": PROJECT,
                "com.docker.compose.service": service,
                **(labels or {}),
            },
        },
        "HostConfig": {"NetworkMode": f"{PROJECT}_default"},
        "Mounts": [],
        "NetworkSettings": {"Networks": {}},
    }


IMAGES = {IMAGE_ID: {"Config": {"Env": [PATH_ENTRY]}}}


def generated(containers: list[dict[str, Any]]) -> HostReadResult:
    outcome = generate_compose(PROJECT, containers, IMAGES)
    assert isinstance(outcome, HostReadResult), outcome
    return outcome


def refused(containers: list[dict[str, Any]]) -> NeedsFile:
    outcome = generate_compose(PROJECT, containers, IMAGES)
    assert isinstance(outcome, NeedsFile), outcome
    return outcome


@pytest.mark.parametrize("case", ["shop", "ops", "minimal"])
def test_recorded_inspect_documents_reproduce_the_services_and_pass_the_precheck(
    case: str,
) -> None:
    data = recorded(case)

    outcome = generate_compose(data["project"], data["containers"], data["images"])

    assert isinstance(outcome, HostReadResult)
    assert yaml.safe_load(outcome.compose_text) == data["expect"]["document"]
    assert [[v.name, v.value] for v in outcome.variables] == data["expect"]["variables"]
    assert all(variable.secret for variable in outcome.variables)
    assert outcome.file_source == "generated"
    assert outcome.files == ()
    precheck(outcome.compose_text, kind="compose")


def test_secret_values_never_appear_in_the_generated_text() -> None:
    data = recorded("shop")

    outcome = generate_compose(data["project"], data["containers"], data["images"])

    assert isinstance(outcome, HostReadResult)
    assert "${POSTGRES_PASSWORD}" in outcome.compose_text
    for variable in outcome.variables:
        assert variable.value not in outcome.compose_text


def test_known_losses_are_listed_with_the_generated_file() -> None:
    outcome = generated([container()])

    assert outcome.losses[: len(KNOWN_LOSSES)] == KNOWN_LOSSES


def test_an_empty_secret_named_value_stays_in_the_text() -> None:
    outcome = generated([container(env=[PATH_ENTRY, "API_TOKEN="])])

    assert yaml.safe_load(outcome.compose_text)["services"]["app"]["environment"] == {
        "API_TOKEN": ""
    }
    assert outcome.variables == ()


@pytest.mark.parametrize(
    ("entry", "variable"),
    [
        ("DB_PASS=abc123", "DB_PASS"),
        ("DATABASE_URL=postgres://app:abc123@db/shop", "DATABASE_URL"),
    ],
)
def test_a_password_by_its_name_or_inside_a_url_becomes_a_secret_variable(
    entry: str, variable: str
) -> None:
    outcome = generated([container(env=[PATH_ENTRY, entry])])

    environment = yaml.safe_load(outcome.compose_text)["services"]["app"]["environment"]
    assert environment == {variable: f"${{{variable}}}"}
    assert [(v.name, v.secret) for v in outcome.variables] == [(variable, True)]
    assert "abc123" not in outcome.compose_text


def test_a_reserved_secret_name_is_stored_under_a_name_built_from_the_service() -> None:
    outcome = generated([container(env=[PATH_ENTRY, "DOCKER_REGISTRY_TOKEN=abc123"])])

    environment = yaml.safe_load(outcome.compose_text)["services"]["app"]["environment"]
    assert environment == {"DOCKER_REGISTRY_TOKEN": "${app_DOCKER_REGISTRY_TOKEN}"}
    assert [(v.name, v.value) for v in outcome.variables] == [
        ("app_DOCKER_REGISTRY_TOKEN", "abc123")
    ]


@pytest.mark.parametrize("value", ["line one\nline two", "x" * 9000])
def test_a_secret_value_that_cannot_be_a_variable_refuses_the_file_without_the_value(
    value: str,
) -> None:
    outcome = refused([container(env=[PATH_ENTRY, f"DB_PASSWORD={value}"])])

    assert outcome == NeedsFile("secret_value_unusable", "services.app.environment.DB_PASSWORD")


def test_an_environment_name_outside_the_variable_grammar_refuses_the_file() -> None:
    outcome = refused([container(env=[PATH_ENTRY, "bad name=1"])])

    assert outcome.reason == "invalid_environment_name"


def test_containers_of_another_project_refuse_the_file() -> None:
    other = container()
    other["Config"]["Labels"]["com.docker.compose.project"] = "elsewhere"

    outcome = refused([container(), other])

    assert outcome == NeedsFile("labels_disagree", "com.docker.compose.project")


def test_a_project_with_no_containers_refuses_the_file() -> None:
    assert refused([]) == NeedsFile("no_containers", PROJECT)


def test_a_service_label_outside_the_key_grammar_refuses_the_file() -> None:
    outcome = refused([container(service="web app")])

    assert outcome.reason == "invalid_service_key"


def test_a_dependency_without_a_container_is_dropped_and_listed() -> None:
    web = container("web", labels={"com.docker.compose.depends_on": "cache:service_started:false"})

    outcome = generated([web])

    assert "depends_on" not in yaml.safe_load(outcome.compose_text)["services"]["web"]
    assert "services.web: depends_on cache has no container." in outcome.losses


def test_plain_dependencies_are_written_as_a_list() -> None:
    web = container(
        "web",
        labels={
            "com.docker.compose.depends_on": "db:service_started:false,cache:service_started:false"
        },
    )

    outcome = generated([web, container("db"), container("cache")])

    assert yaml.safe_load(outcome.compose_text)["services"]["web"]["depends_on"] == [
        "cache",
        "db",
    ]


def test_an_image_missing_from_the_host_writes_its_defaults_and_says_so() -> None:
    outcome = generate_compose(PROJECT, [container()], {})

    assert isinstance(outcome, HostReadResult)
    assert yaml.safe_load(outcome.compose_text)["services"]["app"]["environment"] == {
        "PATH": "/usr/bin"
    }
    assert any("services.app" in loss and "image" in loss for loss in outcome.losses)


def test_a_text_over_the_compose_size_limit_refuses_the_file() -> None:
    labels = {f"label.{number}": "x" * 1000 for number in range(140)}

    outcome = refused([container(labels=labels)])

    assert outcome.reason == "too_large"


def test_generation_does_not_change_the_documents_it_reads() -> None:
    data = recorded("shop")
    before = copy.deepcopy(data)

    generate_compose(data["project"], data["containers"], data["images"])

    assert data == before
