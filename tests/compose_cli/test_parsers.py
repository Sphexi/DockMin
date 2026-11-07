"""Tests for app/compose_cli/parsers.py and the machine-readable-only rule of the wrapper."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.compose_cli.cli import (
    CONFIG_TAIL,
    PS_TAIL,
    START_TAIL,
    STOP_TAIL,
    VERSION_TAIL,
    ComposeProject,
    build_argv,
    down_tail,
    up_tail,
)
from app.compose_cli.errors import ComposeOutputInvalid
from app.compose_cli.parsers import (
    FLOOR,
    MAX_EVENT_ID_CHARS,
    MAX_EVENT_LINE_BYTES,
    MAX_EVENT_TEXT_CHARS,
    parse_config,
    parse_event,
    parse_ps,
    parse_version,
    parse_version_text,
)

SampleReader = Callable[[str], bytes]

JSON_FORMAT = ("--format", "json")
JSON_PROGRESS = ("--progress", "json")
PROJECT_DIRECTORY = Path("/srv/project")
PROJECT = ComposeProject("stack-a", PROJECT_DIRECTORY, PROJECT_DIRECTORY / "compose.yaml")

HUMAN_VERSION = b"Docker Compose version v2.30.1\n"
HUMAN_CONFIG = b"name: stack\nservices:\n  web:\n    image: nginx:1\n"
HUMAN_PS_TABLE = (
    b"NAME      IMAGE     COMMAND   SERVICE   CREATED   STATUS    PORTS\n"
    b"stack-web-1   nginx:1   nginx   web   2 seconds ago   Up 1 second   80/tcp\n"
)
HUMAN_EVENT = b" \xe2\xa0\x8b Container stack-web-1  Starting\n"
HUMAN_EVENT_PLAIN = b"Container stack-web-1  Started"


def ps_line(**overrides: Any) -> bytes:
    entry = {"Name": "stack-web-1", "Service": "web", "State": "running"} | overrides
    return json.dumps(entry).encode()


@pytest.mark.parametrize(
    ("document", "text", "numbers"),
    [
        (b'{"version":"v2.30.1"}', "v2.30.1", (2, 30, 1)),
        (b'{"version":"2.30.1"}', "v2.30.1", (2, 30, 1)),
        (b'{"version":"v2.31.0-rc1"}', "v2.31.0-rc1", (2, 31, 0)),
        (b'{"version":"v5.5.1+build.7"}', "v5.5.1+build.7", (5, 5, 1)),
        (b'{"version":" v2.30.0 "}', "v2.30.0", (2, 30, 0)),
    ],
)
def test_parse_version_reads_the_release_with_or_without_the_v_prefix(
    document: bytes, text: str, numbers: tuple[int, int, int]
) -> None:
    version = parse_version(document)

    assert version.text == text
    assert (version.major, version.minor, version.patch) == numbers


def test_parse_version_reads_a_real_version_sample(sample: SampleReader) -> None:
    assert parse_version(sample("version.stdout")).text == "v5.5.1"


@pytest.mark.parametrize(
    "document",
    [
        b"",
        b"Docker Compose version v2.30.1",
        b"[]",
        b'"v2.30.1"',
        b"{}",
        b'{"version":2}',
        b'{"version":"v2.30"}',
        b'{"version":"latest"}',
        b'{"version":"v2.30.1\\nextra"}',
        b'{"version":"v2.30.1","padding":"' + b"x" * 1100 + b'"}',
        b'{"version":NaN}',
    ],
)
def test_parse_version_refuses_anything_that_is_not_a_version_object(document: bytes) -> None:
    with pytest.raises(ComposeOutputInvalid):
        parse_version(document)


@pytest.mark.parametrize("text", ["", "junk", "v2.30", "v2..0", "2.30.x", "v1.2.3-" + "a" * 65])
def test_parse_version_text_returns_none_for_text_that_is_not_a_version(text: str) -> None:
    assert parse_version_text(text) is None


@pytest.mark.parametrize(
    ("text", "meets_floor"),
    [
        ("v2.31.9", False),
        ("v1.99.99", False),
        ("v2.32.0", True),
        ("v2.32.1", True),
        ("v2.33.0-rc1", True),
        ("v5.5.1", True),
    ],
)
def test_at_least_compares_a_release_with_the_floor(text: str, meets_floor: bool) -> None:
    version = parse_version_text(text)

    assert version is not None
    assert version.at_least(FLOOR) is meets_floor


def test_the_floor_is_v2_32_0() -> None:
    assert (FLOOR.major, FLOOR.minor, FLOOR.patch) == (2, 32, 0)


def test_parse_config_reads_the_sections_and_keeps_the_whole_document(
    sample: SampleReader,
) -> None:
    project = parse_config(sample("config-local.stdout"))

    assert project.name == "dockmin-e2e-sample"
    assert set(project.services) == {"once", "web"}
    assert set(project.networks) == {"appnet"}
    assert set(project.volumes) == {"data"}
    assert set(project.configs) == {"motd"}
    assert set(project.secrets) == {"tok"}
    assert set(project.document) == {
        "configs",
        "name",
        "networks",
        "secrets",
        "services",
        "volumes",
    }
    assert project.services["web"]["image"] == "registry:2"


def test_parse_config_sees_a_profile_service_when_all_profiles_are_enabled(
    sample: SampleReader,
) -> None:
    project = parse_config(sample("config-profile-all.stdout"))

    assert set(project.services) == {"once", "tool", "web"}
    assert project.services["tool"]["profiles"] == ["debug"]


def test_parse_config_treats_absent_sections_as_empty_and_null_items_as_empty() -> None:
    project = parse_config(b'{"name":"p","services":{"web":null}}')

    assert project.services == {"web": {}}
    assert project.networks == project.volumes == project.configs == project.secrets == {}


@pytest.mark.parametrize(
    "document",
    [
        b"",
        b"[]",
        b'{"services":{}}',
        b'{"name":"","services":{}}',
        b'{"name":7}',
        b'{"name":"p","services":[]}',
        b'{"name":"p","services":{"web":"nginx"}}',
        b'{"name":"p","networks":{"n":[]}}',
        b'{"name":"p","services":Infinity}',
    ],
)
def test_parse_config_refuses_a_document_of_the_wrong_shape(document: bytes) -> None:
    with pytest.raises(ComposeOutputInvalid):
        parse_config(document)


def test_parse_config_refuses_the_error_document_of_an_invalid_project(
    sample: SampleReader,
) -> None:
    error_document = sample("config-invalid.stderr")

    with pytest.raises(ComposeOutputInvalid):
        parse_config(error_document)
    assert parse_event(error_document) is None


def test_parse_ps_reads_json_lines(sample: SampleReader) -> None:
    entries = parse_ps(sample("ps-local.stdout"))

    assert [(e.name, e.service, e.state) for e in entries] == [
        ("dockmin-e2e-sample-once-1", "once", "exited"),
        ("dockmin-e2e-sample-web-1", "web", "running"),
    ]
    assert entries[0].exit_code == 3
    assert entries[1].health is None


def test_parse_ps_returns_no_entries_for_empty_output(sample: SampleReader) -> None:
    assert parse_ps(sample("ps-empty.stdout")) == ()


def test_parse_ps_skips_blank_lines_between_entries() -> None:
    stdout = ps_line(Name="a") + b"\n\n  \n" + ps_line(Name="b") + b"\n"

    assert [e.name for e in parse_ps(stdout)] == ["a", "b"]


def test_parse_ps_keeps_a_health_string_and_ignores_a_boolean_exit_code() -> None:
    (entry,) = parse_ps(ps_line(Health="healthy", ExitCode=True))

    assert entry.health == "healthy"
    assert entry.exit_code is None


@pytest.mark.parametrize(
    "stdout",
    [
        b"[" + ps_line() + b"]",
        HUMAN_PS_TABLE,
        ps_line() + b"\nnot json\n",
        b'["not an object"]',
        b'{"Name":"web","Service":"web"}',
        ps_line(State=3),
        ps_line() + b'\n{"Name":NaN}',
    ],
)
def test_parse_ps_refuses_output_that_is_not_json_lines_of_containers(stdout: bytes) -> None:
    with pytest.raises(ComposeOutputInvalid):
        parse_ps(stdout)


def test_parse_event_reads_every_field() -> None:
    line = (
        b'{"id":"Image x","parent_id":"pull","status":"Working","text":"t","current":3,"total":9}'
    )

    event = parse_event(line)

    assert event is not None
    assert (event.resource, event.parent, event.status, event.text) == (
        "Image x",
        "pull",
        "Working",
        "t",
    )
    assert (event.current, event.total, event.is_error) == (3, 9, False)


@pytest.mark.parametrize("status", ["Error", "error", "ERROR"])
def test_parse_event_flags_an_error_status_in_any_case(status: str) -> None:
    event = parse_event(json.dumps({"id": "Container c", "status": status}).encode())

    assert event is not None
    assert event.is_error is True


def test_parse_event_accepts_an_id_that_is_not_kind_and_name(sample: SampleReader) -> None:
    lines = sample("up-image-missing.stderr").splitlines()

    events = [e for e in map(parse_event, lines) if e is not None]

    assert [e.resource for e in events][-1] == "service:m:1"
    assert events[-1].is_error is True


def test_parse_event_takes_events_from_a_failing_up_and_ignores_its_error_line(
    sample: SampleReader,
) -> None:
    lines = sample("up-fail-local.stderr").splitlines()

    events = [parse_event(line) for line in lines]

    assert events[-1] is None
    assert [e is not None for e in events] == [True] * (len(lines) - 1) + [False]


def test_parse_event_ignores_the_warning_lines_of_an_adopting_up(sample: SampleReader) -> None:
    lines = sample("up-adopt-both.stderr").splitlines()

    events = [parse_event(line) for line in lines]

    assert [e is None for e in events] == [True, True, False, False, False, False]


@pytest.mark.parametrize(
    "line",
    [
        b'{"error":true,"message":"boom"}',
        b'{"level":"warning","msg":"careful","time":"t"}',
        b'{"id":1,"status":"Done"}',
        b'{"id":"Container c"}',
        b'{"status":"Done"}',
        b"[1]",
        b'"text"',
        b"12",
        b"null",
        b"{not json",
        b'{"id":"a","status":"Done","current":NaN}',
        b"",
        HUMAN_EVENT,
        HUMAN_EVENT_PLAIN,
        HUMAN_PS_TABLE,
    ],
)
def test_parse_event_returns_none_for_any_line_that_is_not_an_event(line: bytes) -> None:
    assert parse_event(line) is None


def test_parse_event_returns_none_for_a_line_over_the_length_cap() -> None:
    padding = "x" * MAX_EVENT_LINE_BYTES
    line = json.dumps({"id": "Container c", "status": "Done", "text": padding}).encode()

    assert parse_event(line) is None


def test_parse_event_returns_long_fields_whole_and_drops_invalid_counts() -> None:
    document = {
        "id": "i" * (MAX_EVENT_ID_CHARS + 10),
        "status": "Working",
        "text": "t" * (MAX_EVENT_TEXT_CHARS + 10),
        "current": -1,
        "total": True,
    }

    event = parse_event(json.dumps(document).encode())

    assert event is not None
    assert len(event.resource or "") == MAX_EVENT_ID_CHARS + 10
    assert len(event.text or "") == MAX_EVENT_TEXT_CHARS + 10
    assert (event.current, event.total) == (None, None)


def test_bounded_cuts_each_text_field_to_its_limit_and_leaves_the_rest() -> None:
    document = {
        "id": "i" * (MAX_EVENT_ID_CHARS + 10),
        "parent_id": "p" * (MAX_EVENT_ID_CHARS + 10),
        "status": "s" * (MAX_EVENT_ID_CHARS + 10),
        "text": "t" * (MAX_EVENT_TEXT_CHARS + 10),
        "current": 3,
        "total": 4,
    }
    event = parse_event(json.dumps(document).encode())
    assert event is not None

    cut = event.bounded()

    assert len(cut.resource or "") == MAX_EVENT_ID_CHARS
    assert len(cut.parent or "") == MAX_EVENT_ID_CHARS
    assert len(cut.status or "") == MAX_EVENT_ID_CHARS
    assert len(cut.text or "") == MAX_EVENT_TEXT_CHARS
    assert (cut.current, cut.total) == (3, 4)


def test_parse_event_tolerates_surrounding_whitespace() -> None:
    event = parse_event(b'  {"id":"Container c","status":"Done"}\r')

    assert event is not None
    assert event.resource == "Container c"


@pytest.mark.parametrize(
    ("parser", "human_output"),
    [
        (parse_version, HUMAN_VERSION),
        (parse_config, HUMAN_CONFIG),
        (parse_ps, HUMAN_PS_TABLE),
    ],
)
def test_every_document_parser_refuses_human_readable_output(
    parser: Callable[[bytes], object], human_output: bytes
) -> None:
    with pytest.raises(ComposeOutputInvalid):
        parser(human_output)


@pytest.mark.parametrize("tail", [VERSION_TAIL, CONFIG_TAIL, PS_TAIL])
def test_the_read_commands_ask_for_json_output(tail: tuple[str, ...]) -> None:
    assert tail[-2:] == JSON_FORMAT


@pytest.mark.parametrize(
    "tail",
    [
        CONFIG_TAIL,
        PS_TAIL,
        up_tail(remove_orphans=False),
        up_tail(remove_orphans=True),
        down_tail(remove_orphans=False),
        down_tail(remove_orphans=True),
        START_TAIL,
        STOP_TAIL,
    ],
)
def test_every_project_command_asks_for_json_progress(tail: tuple[str, ...]) -> None:
    argv = build_argv(PROJECT, PROJECT_DIRECTORY / "empty.env", tail)

    assert argv[:2] == JSON_PROGRESS
    assert argv[-len(tail) :] == tail
