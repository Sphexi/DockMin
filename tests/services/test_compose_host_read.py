"""Tests for ComposeFileReader: the files of a project read through the helper container."""

from collections.abc import AsyncIterator, Sequence

import pytest
import yaml

from app.auth.principal import Principal
from app.compose_cli.errors import ComposeCommandFailed
from app.docker_engine import EngineClient
from app.self_identity import SelfIdentity
from app.services.compose_host_read import MERGED_LOSSES, ComposeFileReader, FilesRead
from app.services.compose_read_inputs import CONFIG_FILES_LABEL, WORKING_DIR_LABEL
from app.services.host_files import HostFileReader
from app.services.stack_read_result import HostReadResult, NeedsFile
from tests.docker_engine.helpers import ENV_ID
from tests.host_read_support import (
    ENVIRONMENT_FILE_LABEL,
    PROJECT,
    WORKING_DIR,
    ReaderEngine,
    add_project_container,
    compose_labels,
)

ADMIN = Principal(1, "admin", "administrator")
SECRET = "sentinel-secret-9917"
OWN_MOUNT = f"{WORKING_DIR}/data"

COMPOSE = """\
services:
  web:
    image: nginx:1
    env_file:
      - ./env/app.env
    configs:
      - source: site
        target: /etc/site.conf
    secrets:
      - token
configs:
  site:
    file: ./conf/site.conf
secrets:
  token:
    file: ./secrets/token.txt
"""


@pytest.fixture
def daemon() -> ReaderEngine:
    engine = ReaderEngine()
    engine.add_image("nginx:1")
    return engine


@pytest.fixture
async def client(daemon: ReaderEngine) -> AsyncIterator[EngineClient]:
    async with EngineClient(ENV_ID, daemon.bundle()) as opened:
        yield opened


def deep_merge(into: dict[str, object], other: dict[str, object]) -> None:
    for key, value in other.items():
        held = into.get(key)
        if isinstance(held, dict) and isinstance(value, dict):
            deep_merge(held, value)
        elif isinstance(held, list) and isinstance(value, list):
            held.extend(value)
        else:
            into[key] = value


async def merge_by_concatenating(name: str, texts: Sequence[str]) -> str:
    merged: dict[str, object] = {}
    for text in texts:
        deep_merge(merged, yaml.safe_load(text))
    return yaml.safe_dump({"name": name, **merged})


async def read(
    client: EngineClient,
    daemon: ReaderEngine,
    *labels: dict[str, str],
    protects: SelfIdentity | None = None,
    merge: object = merge_by_concatenating,
) -> FilesRead:
    for number, found in enumerate(labels or (compose_labels(),), start=1):
        add_project_container(daemon, number, found)
    containers = await client.containers.list(all=True)
    identity = protects or SelfIdentity()
    reader = ComposeFileReader(HostFileReader(client), merge)  # type: ignore[arg-type]
    return await reader.read(
        ADMIN, PROJECT, containers, lambda path: identity.protects_path(ENV_ID, path)
    )


def serve_project(daemon: ReaderEngine) -> None:
    daemon.serve("compose.yaml", COMPOSE)
    daemon.serve("env/app.env", f"API_TOKEN={SECRET}\n")
    daemon.serve("conf/site.conf", "listen 80;\n")
    daemon.serve("secrets/token.txt", SECRET)


async def test_a_one_file_project_is_read_rewritten_and_pre_checked(
    client: EngineClient, daemon: ReaderEngine
) -> None:
    serve_project(daemon)

    found = await read(client, daemon)

    assert isinstance(found.outcome, HostReadResult), found.outcome
    result = found.outcome
    document = yaml.safe_load(result.compose_text)
    assert result.file_source == "read"
    assert document["services"]["web"]["env_file"] == ["./env_app.env"]
    assert document["configs"]["site"]["file"] == "./conf_site.conf"
    assert document["secrets"]["token"]["file"] == "./secrets_token.txt"
    assert {item.name: item.secret for item in result.files} == {
        "env_app.env": True,
        "conf_site.conf": False,
        "secrets_token.txt": True,
    }
    assert found.files_read == (
        "compose.yaml",
        "env/app.env",
        "conf/site.conf",
        "secrets/token.txt",
    )
    assert found.refused == ()
    assert found.helper_removed is True and daemon.helper_boxes() == []
    assert daemon.verbs() == ["create", "remove"]


async def test_the_environment_file_becomes_variables_with_secret_names_marked(
    client: EngineClient, daemon: ReaderEngine
) -> None:
    daemon.serve("compose.yaml", "services:\n  web:\n    image: nginx:1\n")
    daemon.serve(".env", f'REGION=eu\nexport API_TOKEN="{SECRET}"\n')
    labels = compose_labels(environment_file=f"{WORKING_DIR}/.env")

    found = await read(client, daemon, labels)

    assert isinstance(found.outcome, HostReadResult), found.outcome
    assert [(v.name, v.secret) for v in found.outcome.variables] == [
        ("REGION", False),
        ("API_TOKEN", True),
    ]
    assert SECRET not in found.outcome.compose_text


@pytest.mark.parametrize(
    "line",
    [
        f'TOKEN="a\\n{SECRET}"',
        f"PASSWORD={SECRET}$HOME",
        f"DOCKER_HOST={SECRET}",
        f"KEY={SECRET} # comment",
    ],
)
async def test_an_environment_line_outside_the_subset_needs_a_file_naming_the_line_only(
    client: EngineClient, daemon: ReaderEngine, line: str
) -> None:
    daemon.serve("compose.yaml", "services:\n  web:\n    image: nginx:1\n")
    daemon.serve(".env", f"REGION=eu\n{line}\n")

    found = await read(client, daemon, compose_labels(environment_file=f"{WORKING_DIR}/.env"))

    assert found.outcome == NeedsFile("environment_file_unsupported", ".env:2")
    assert SECRET not in repr(found)
    assert daemon.helper_boxes() == []


async def test_an_override_pair_is_merged_into_one_file_marked_merged(
    client: EngineClient, daemon: ReaderEngine
) -> None:
    daemon.serve("compose.yaml", "services:\n  web:\n    image: nginx:1\n")
    daemon.serve("compose.override.yaml", "services:\n  web:\n    ports:\n      - '8080:80'\n")
    labels = compose_labels(
        config_files=f"{WORKING_DIR}/compose.yaml,{WORKING_DIR}/compose.override.yaml"
    )

    found = await read(client, daemon, labels)

    assert isinstance(found.outcome, HostReadResult), found.outcome
    assert found.outcome.file_source == "merged"
    assert found.outcome.losses == MERGED_LOSSES
    web = yaml.safe_load(found.outcome.compose_text)["services"]["web"]
    assert web == {"image": "nginx:1", "ports": ["8080:80"]}
    assert found.files_read == ("compose.yaml", "compose.override.yaml")


async def test_a_merge_that_compose_refuses_needs_a_file(
    client: EngineClient, daemon: ReaderEngine
) -> None:
    daemon.serve("a.yaml", "services:\n  web:\n    image: nginx:1\n")
    daemon.serve("b.yaml", "services:\n  web:\n    image: nginx:2\n")

    async def failing(name: str, texts: Sequence[str]) -> str:
        raise ComposeCommandFailed(("config",), 1, "", ())

    labels = compose_labels(config_files=f"{WORKING_DIR}/a.yaml,{WORKING_DIR}/b.yaml")

    found = await read(client, daemon, labels, merge=failing)

    assert found.outcome == NeedsFile("merge_failed", CONFIG_FILES_LABEL)


async def test_several_files_that_include_others_are_not_merged(
    client: EngineClient, daemon: ReaderEngine
) -> None:
    daemon.serve("a.yaml", "include: [part.yaml]\nservices: {}\n")
    daemon.serve("b.yaml", "services:\n  web:\n    image: nginx:1\n")
    daemon.serve("part.yaml", "services:\n  db:\n    image: nginx:1\n")
    labels = compose_labels(config_files=f"{WORKING_DIR}/a.yaml,{WORKING_DIR}/b.yaml")

    found = await read(client, daemon, labels)

    assert found.outcome == NeedsFile("merge_unsupported", CONFIG_FILES_LABEL)


@pytest.mark.parametrize(
    ("labels", "reason", "path"),
    [
        (compose_labels(working_dir="/"), "working_directory_root", WORKING_DIR_LABEL),
        (
            compose_labels(working_dir="/srv/../etc", config_files="/srv/../etc/c.yaml"),
            "working_directory_not_normalised",
            WORKING_DIR_LABEL,
        ),
        (
            compose_labels(working_dir="srv/shop", config_files="srv/shop/c.yaml"),
            "working_directory_not_absolute",
            WORKING_DIR_LABEL,
        ),
        (
            compose_labels(config_files="/etc/compose.yaml"),
            "config_file_outside",
            CONFIG_FILES_LABEL,
        ),
        (
            compose_labels(environment_file="/etc/environment"),
            "environment_file_outside",
            ENVIRONMENT_FILE_LABEL,
        ),
    ],
)
async def test_a_label_or_path_rule_that_fails_needs_a_file_and_creates_no_helper(
    client: EngineClient,
    daemon: ReaderEngine,
    labels: dict[str, str],
    reason: str,
    path: str,
) -> None:
    daemon.serve("compose.yaml", COMPOSE)

    found = await read(client, daemon, labels)

    assert found == FilesRead(NeedsFile(reason, path))
    assert daemon.verbs() == []


async def test_a_working_directory_holding_a_dockmin_mount_is_refused() -> None:
    daemon = ReaderEngine()
    daemon.add_image("nginx:1")
    daemon.serve("compose.yaml", COMPOSE)
    identity = SelfIdentity(mount_sources={ENV_ID: frozenset({OWN_MOUNT})})
    async with EngineClient(ENV_ID, daemon.bundle()) as client:
        found = await read(client, daemon, protects=identity)

    assert found == FilesRead(NeedsFile("own_mount", WORKING_DIR_LABEL))
    assert daemon.verbs() == []


async def test_containers_that_disagree_on_the_labels_need_a_file(
    client: EngineClient, daemon: ReaderEngine
) -> None:
    daemon.serve("compose.yaml", COMPOSE)

    found = await read(
        client,
        daemon,
        compose_labels(),
        compose_labels(service="db", working_dir=f"{WORKING_DIR}-copy"),
    )

    assert found == FilesRead(NeedsFile("labels_disagree", WORKING_DIR_LABEL))
    assert daemon.verbs() == []


async def test_a_build_section_needs_a_file_naming_it_and_renders_nothing_of_the_file(
    client: EngineClient, daemon: ReaderEngine
) -> None:
    daemon.serve("compose.yaml", f"services:\n  web:\n    build: .\n    command: {SECRET}\n")

    found = await read(client, daemon)

    assert found.outcome == NeedsFile("refused_construct", "services.web.build")
    assert SECRET not in repr(found)
    assert found.helper_removed is True


async def test_a_reference_outside_the_working_directory_needs_a_file_with_its_path(
    client: EngineClient, daemon: ReaderEngine
) -> None:
    daemon.serve(
        "compose.yaml", "services:\n  web:\n    image: nginx:1\n    env_file: ../shared.env\n"
    )
    daemon.serve("../shared.env", f"A={SECRET}\n")

    found = await read(client, daemon)

    assert found.outcome == NeedsFile("reference_outside", "services.web.env_file")
    assert found.files_read == ("compose.yaml",)
    assert SECRET not in repr(found)


async def test_a_refused_file_is_listed_with_its_reason_and_none_of_its_content(
    client: EngineClient, daemon: ReaderEngine
) -> None:
    found = await read(client, daemon)

    assert found.outcome == NeedsFile("missing", "compose.yaml")
    assert found.refused == (("compose.yaml", "missing"),)
    assert found.helper_removed is True


async def test_a_project_whose_containers_have_no_image_on_the_host_needs_a_file(
    client: EngineClient, daemon: ReaderEngine
) -> None:
    daemon.serve("compose.yaml", COMPOSE)
    daemon.remove_image(next(iter(daemon.image_store)))

    found = await read(client, daemon)

    assert found.outcome == NeedsFile("helper_image_missing", "com.docker.compose.project")
    assert daemon.helper_boxes() == []
