"""Tests for the project location read from labels and the strict environment-file subset."""

import pytest

from app.self_identity import SelfIdentity
from app.services.compose_read_inputs import (
    CONFIG_FILES_LABEL,
    ENVIRONMENT_FILE_LABEL,
    MAX_CONFIG_FILES,
    WORKING_DIR_LABEL,
    ProjectLocation,
    locate_project,
    parse_environment_file,
    variables_of,
)
from app.services.stack_read_result import NeedsFile, ReadVariable

WORKING_DIR = "/srv/stacks/shop"
COMPOSE_FILE = f"{WORKING_DIR}/compose.yaml"
ENV_FILE = f"{WORKING_DIR}/.env"
SECRET_VALUE = "hunter2-sentinel"
ENVIRONMENT = 1
OWN_MOUNT = "/srv/stacks/shop/data"


def labels(
    working_dir: str = WORKING_DIR, config: str = COMPOSE_FILE, env: str | None = ENV_FILE
) -> dict[str, str]:
    found = {WORKING_DIR_LABEL: working_dir, CONFIG_FILES_LABEL: config}
    if env is not None:
        found[ENVIRONMENT_FILE_LABEL] = env
    return found


def never(_path: str) -> bool:
    return False


def locate(*sets: dict[str, str]) -> ProjectLocation | NeedsFile:
    return locate_project(list(sets), never)


def test_labels_name_the_working_directory_and_every_file_relative_to_it() -> None:
    found = locate(
        labels(config=f"{COMPOSE_FILE},{WORKING_DIR}/override/prod.yaml", env=f"{ENV_FILE}")
    )

    assert found == ProjectLocation(WORKING_DIR, ("compose.yaml", "override/prod.yaml"), (".env",))


def test_a_project_without_an_environment_file_label_has_no_environment_files() -> None:
    found = locate(labels(env=None))

    assert isinstance(found, ProjectLocation)
    assert found.environment_files == ()


@pytest.mark.parametrize(
    ("label", "other"),
    [
        (WORKING_DIR_LABEL, "/srv/other"),
        (CONFIG_FILES_LABEL, f"{WORKING_DIR}/other.yaml"),
        (ENVIRONMENT_FILE_LABEL, f"{WORKING_DIR}/.other"),
    ],
)
def test_containers_that_disagree_on_a_label_need_a_file(label: str, other: str) -> None:
    first = labels()
    second = {**first, label: other}

    assert locate(first, second) == NeedsFile("labels_disagree", label)


def test_a_container_without_the_labels_needs_a_file() -> None:
    assert locate({}) == NeedsFile("labels_missing", WORKING_DIR_LABEL)
    assert locate({WORKING_DIR_LABEL: WORKING_DIR}) == NeedsFile(
        "labels_missing", CONFIG_FILES_LABEL
    )


@pytest.mark.parametrize(
    ("working_dir", "reason"),
    [
        ("/", "working_directory_root"),
        ("/srv/../etc", "working_directory_not_normalised"),
        ("srv/shop", "working_directory_not_absolute"),
        ("/srv/shop\n", "working_directory_control_characters"),
        ("/srv/a:b", "working_directory_bind_separator"),
        ("/" + "a" * 1024, "working_directory_too_long"),
    ],
)
def test_a_working_directory_that_is_not_plain_needs_a_file(working_dir: str, reason: str) -> None:
    assert locate(labels(working_dir=working_dir)) == NeedsFile(reason, WORKING_DIR_LABEL)


@pytest.mark.parametrize("working_dir", [WORKING_DIR, "/srv", f"{OWN_MOUNT}/inner", OWN_MOUNT, "/"])
def test_a_working_directory_holding_or_inside_a_dockmin_mount_is_refused(
    working_dir: str,
) -> None:
    identity = SelfIdentity(mount_sources={ENVIRONMENT: frozenset({OWN_MOUNT})})

    found = locate_project(
        [labels(working_dir=working_dir, config=f"{working_dir}/c.yaml")],
        lambda path: identity.protects_path(ENVIRONMENT, path),
    )

    assert found == NeedsFile(
        "working_directory_root" if working_dir == "/" else "own_mount", WORKING_DIR_LABEL
    )


@pytest.mark.parametrize("working_dir", ["/srv/stacks/other", "/srv/stacks/shop-data", "/var/lib"])
def test_a_working_directory_beside_a_dockmin_mount_is_read(working_dir: str) -> None:
    identity = SelfIdentity(mount_sources={ENVIRONMENT: frozenset({OWN_MOUNT})})

    found = locate_project(
        [labels(working_dir=working_dir, config=f"{working_dir}/c.yaml", env=None)],
        lambda path: identity.protects_path(ENVIRONMENT, path),
    )

    assert isinstance(found, ProjectLocation)


@pytest.mark.parametrize(
    "config",
    [
        "/etc/compose.yaml",
        f"{WORKING_DIR}/../compose.yaml",
        f"{WORKING_DIR}-other/compose.yaml",
        f"{WORKING_DIR}//compose.yaml",
    ],
)
def test_a_config_file_outside_the_working_directory_needs_a_file(config: str) -> None:
    assert locate(labels(config=config)) == NeedsFile("config_file_outside", CONFIG_FILES_LABEL)


def test_an_environment_file_outside_the_working_directory_needs_a_file() -> None:
    assert locate(labels(env="/etc/environment")) == NeedsFile(
        "environment_file_outside", ENVIRONMENT_FILE_LABEL
    )


def test_more_config_files_than_the_cap_need_a_file() -> None:
    many = ",".join(f"{WORKING_DIR}/{number}.yaml" for number in range(MAX_CONFIG_FILES + 1))

    assert locate(labels(config=many)) == NeedsFile("too_many_config_files", CONFIG_FILES_LABEL)


def test_the_supported_subset_parses_into_values() -> None:
    text = "\n".join(
        [
            "# a comment",
            "",
            "PLAIN=value",
            "export EXPORTED=1",
            'DOUBLE="two words"',
            "SINGLE='x'",
            "EMPTY=",
            "PLAIN=again",
            "URL=http://host/a#b",
        ]
    )

    assert parse_environment_file(text, ".env") == {
        "PLAIN": "again",
        "EXPORTED": "1",
        "DOUBLE": "two words",
        "SINGLE": "x",
        "EMPTY": "",
        "URL": "http://host/a#b",
    }


@pytest.mark.parametrize(
    "line",
    [
        f'KEY="a\\nb{SECRET_VALUE}"',
        f"KEY={SECRET_VALUE}$HOME",
        f'KEY="{SECRET_VALUE}${{HOME}}"',
        f"KEY='{SECRET_VALUE}",
        f"KEY={SECRET_VALUE} # trailing comment",
        f"KEY=`{SECRET_VALUE}`",
        f'KEY="{SECRET_VALUE}" # comment',
        f"KEY={SECRET_VALUE}\\ more",
        "KEY",
        f"KEY = {SECRET_VALUE}",
        f"1KEY={SECRET_VALUE}",
        f"DOCKER_HOST={SECRET_VALUE}",
    ],
)
def test_a_line_outside_the_subset_needs_a_file_naming_its_line_and_never_its_value(
    line: str,
) -> None:
    outcome = parse_environment_file(f"FIRST=ok\n{line}\n", ".env")

    assert outcome == NeedsFile("environment_file_unsupported", ".env:2")
    assert SECRET_VALUE not in repr(outcome)


def test_names_that_say_secret_become_secret_variables() -> None:
    found = variables_of({"API_TOKEN": "t", "db_password": "p", "SIGNING_KEY": "k", "REGION": "eu"})

    assert found == (
        ReadVariable("API_TOKEN", "t", True),
        ReadVariable("db_password", "p", True),
        ReadVariable("SIGNING_KEY", "k", True),
        ReadVariable("REGION", "eu", False),
    )
