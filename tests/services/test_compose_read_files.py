"""Tests for reading the files a Compose file names and rewriting its references."""

from collections.abc import Mapping

import pytest
import yaml

from app.services.compose_read_files import Collected, collect_references, find_references
from app.services.host_files import FileRead, FileRefusal, FileRefusalReason, HostFile
from app.services.stack_document import MAX_FILES
from app.services.stack_read_result import NeedsFile, finish

WORKING_DIR = "/srv/stacks/shop"

COMPOSE = """\
services:
  web:
    image: nginx:1
    env_file:
      - ./env/app.env
      - path: "conf/extra.env"
        required: false
    configs:
      - source: site
secrets:
  token:
    file: ./secrets/token.txt
configs:
  site:
    file: ./conf/site.conf
"""


class FakeReader:
    """Serves relative paths from a table and records every read."""

    def __init__(self, files: Mapping[str, bytes | FileRefusalReason]) -> None:
        self.files = files
        self.reads: list[str] = []

    async def read(self, relative_path: str, *, limit: int) -> FileRead:
        self.reads.append(relative_path)
        found = self.files.get(relative_path, FileRefusalReason.MISSING)
        if isinstance(found, FileRefusalReason):
            return FileRefusal(relative_path, found)
        return HostFile(relative_path, found)


async def collect(files: Mapping[str, bytes | FileRefusalReason], *texts: str) -> Collected:
    outcome = await collect_references(FakeReader(files), WORKING_DIR, texts)
    assert isinstance(outcome, Collected), outcome
    return outcome


async def refused(files: Mapping[str, bytes | FileRefusalReason], text: str) -> NeedsFile:
    outcome = await collect_references(FakeReader(files), WORKING_DIR, [text])
    assert isinstance(outcome, NeedsFile), outcome
    return outcome


def project(**services: object) -> str:
    return yaml.safe_dump({"services": {"web": {"image": "nginx:1", **services}}})


PROJECT_FILES: dict[str, bytes | FileRefusalReason] = {
    "env/app.env": b"A=1\n",
    "conf/extra.env": b"B=2\n",
    "secrets/token.txt": b"s3cr3t\n",
    "conf/site.conf": b"listen 80;\n",
}


async def test_every_reference_is_read_and_rewritten_to_its_companion_file() -> None:
    collected = await collect(PROJECT_FILES, COMPOSE)

    document = yaml.safe_load(collected.texts[0])
    assert document["services"]["web"]["env_file"] == [
        "./env_app.env",
        {"path": "./conf_extra.env", "required": False},
    ]
    assert document["secrets"]["token"]["file"] == "./secrets_token.txt"
    assert document["configs"]["site"]["file"] == "./conf_site.conf"
    assert {item.name: item.secret for item in collected.files} == {
        "env_app.env": True,
        "conf_extra.env": True,
        "secrets_token.txt": True,
        "conf_site.conf": False,
    }
    assert {item.name: item.content for item in collected.files}["conf_site.conf"] == (
        PROJECT_FILES["conf/site.conf"]
    )
    assert collected.names_compose_files is False


async def test_the_rewritten_text_keeps_everything_else_and_passes_the_pre_check() -> None:
    collected = await collect(PROJECT_FILES, COMPOSE)

    assert collected.texts[0] == COMPOSE.replace("./env/app.env", "./env_app.env").replace(
        '"conf/extra.env"', '"./conf_extra.env"'
    ).replace("./secrets/token.txt", "./secrets_token.txt").replace(
        "./conf/site.conf", "./conf_site.conf"
    )
    finished = finish(collected.texts[0], kind="compose", source="read", files=collected.files)
    assert not isinstance(finished, NeedsFile), finished


async def test_an_absolute_path_inside_the_working_directory_is_read_relative_to_it() -> None:
    collected = await collect(
        {"env/app.env": b"A=1\n"}, project(env_file=f"{WORKING_DIR}/env/app.env")
    )

    assert yaml.safe_load(collected.texts[0])["services"]["web"]["env_file"] == "./env_app.env"


async def test_a_file_named_twice_is_read_once() -> None:
    reader = FakeReader({"env/app.env": b"A=1\n"})

    outcome = await collect_references(
        reader, WORKING_DIR, [project(env_file=["env/app.env", "./env/app.env"])]
    )

    assert isinstance(outcome, Collected)
    assert reader.reads == ["env/app.env"] and len(outcome.files) == 1


async def test_a_file_named_as_an_include_and_as_an_env_file_is_refused_not_half_read() -> None:
    files = {"shared.yaml": b"services: {}\n"}
    text = (
        "include:\n  - shared.yaml\n"
        "services:\n  web:\n    image: nginx:1\n    env_file: shared.yaml\n"
    )

    outcome = await collect_references(FakeReader(files), WORKING_DIR, [text])

    assert outcome == NeedsFile("file_claimed_twice", "services.web.env_file")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("../outside.env", "reference_outside"),
        ("/etc/passwd", "reference_outside"),
        (f"{WORKING_DIR}-other/a.env", "reference_outside"),
        ("env/${STAGE}.env", "reference_has_variable"),
        (".env.local", "file_name_unusable"),
    ],
)
async def test_a_reference_that_cannot_be_a_companion_file_needs_a_file_with_its_path(
    value: str, reason: str
) -> None:
    outcome = await refused({}, project(env_file=value))

    assert outcome == NeedsFile(reason, "services.web.env_file")


async def test_two_paths_that_flatten_to_one_name_need_a_file() -> None:
    files: dict[str, bytes | FileRefusalReason] = {"a/b.env": b"A=1\n", "a_b.env": b"B=1\n"}

    outcome = await refused(files, project(env_file=["a/b.env", "a_b.env"]))

    assert outcome == NeedsFile("file_name_collision", "services.web.env_file[1]")


@pytest.mark.parametrize(
    "reason",
    [FileRefusalReason.MISSING, FileRefusalReason.SYMLINK, FileRefusalReason.TOO_LARGE],
)
async def test_a_referenced_file_that_cannot_be_read_gives_the_reason_and_the_compose_path(
    reason: FileRefusalReason,
) -> None:
    outcome = await refused({"env/app.env": reason}, project(env_file="env/app.env"))

    assert outcome == NeedsFile(reason.value, "services.web.env_file")


async def test_a_path_shared_through_an_alias_or_written_as_a_block_scalar_is_refused() -> None:
    aliased = "x-path: &p ./env/app.env\nservices:\n  web:\n    image: nginx:1\n    env_file: *p\n"
    block = "services:\n  web:\n    image: nginx:1\n    env_file: |\n      ./env/app.env\n"

    assert await refused({}, aliased) == NeedsFile("reference_aliased", "services.web.env_file")
    assert await refused({}, block) == NeedsFile("reference_not_plain", "services.web.env_file")


async def test_a_compose_file_that_is_not_yaml_needs_a_file() -> None:
    outcome = await refused({}, "services: [unclosed\n")

    assert outcome.reason == "invalid_compose"


async def test_more_referenced_files_than_a_stack_holds_need_a_file() -> None:
    names = [f"f{number}.env" for number in range(MAX_FILES + 1)]
    files: dict[str, bytes | FileRefusalReason] = dict.fromkeys(names, b"A=1\n")

    outcome = await refused(files, project(env_file=names))

    assert outcome.reason == "too_many_files"


async def test_an_included_file_is_read_with_its_own_references_resolved_beside_it() -> None:
    files: dict[str, bytes | FileRefusalReason] = {
        "parts/db.yaml": b"services:\n  db:\n    image: pg:1\n    env_file: db.env\n",
        "parts/db.env": b"P=1\n",
    }
    text = "include:\n  - parts/db.yaml\nservices:\n  web:\n    image: nginx:1\n"

    collected = await collect(files, text)

    by_name = {item.name: item for item in collected.files}
    assert yaml.safe_load(collected.texts[0])["include"] == ["./parts_db.yaml"]
    assert yaml.safe_load(by_name["parts_db.yaml"].content)["services"]["db"]["env_file"] == (
        "./parts_db.env"
    )
    assert by_name["parts_db.yaml"].secret is False and by_name["parts_db.env"].secret is True
    assert collected.names_compose_files is True
    finished = finish(collected.texts[0], kind="compose", source="read", files=collected.files)
    assert not isinstance(finished, NeedsFile), finished


async def test_includes_nested_deeper_than_the_limit_need_a_file() -> None:
    files: dict[str, bytes | FileRefusalReason] = {
        f"l{depth}.yaml": f"include: [l{depth + 1}.yaml]\nservices: {{}}\n".encode()
        for depth in range(1, 8)
    }
    text = "include: [l1.yaml]\nservices: {}\n"

    outcome = await refused(files, text)

    assert outcome.reason == "too_deep"


def test_find_references_reports_where_each_scalar_lies() -> None:
    [reference, *_] = find_references(COMPOSE)

    assert COMPOSE[reference.start : reference.end] == "./env/app.env"
    assert (reference.kind, reference.path) == ("env", "services.web.env_file[0]")
