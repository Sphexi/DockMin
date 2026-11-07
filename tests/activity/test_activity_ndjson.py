"""NDJSON encoding of events and log lines: keys, types, escapes, and choosing an encoding."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

import app.services.activity_log as activity_log
from app.db.engine import Session
from app.db.repositories.activity_event import ActivityEvent, ActivityFilter, DetailValue
from app.services.activity_csv import CSV_COLUMNS, ExportRow, csv_header, csv_rows
from app.services.activity_log import ActivityLogService, ExportStream
from app.services.activity_ndjson import (
    NDJSON_MEDIA_TYPE,
    encode_object,
    export_encoding,
    ndjson_rows,
)
from app.services.log_lines import TRUNCATION_MARKER, LogLine, Stream
from app.services.log_ndjson import (
    MAX_PAYLOAD_DEPTH,
    LogContext,
    classify,
    container_source,
    encode_log_line,
    encode_notice,
    for_task,
    line_time,
    service_image,
    service_source,
)
from tests.access_support import ADMIN

SCHEMA_VERSION = 2
LOG_SCHEMA_VERSION = 2
CONTEXT = LogContext("dockmin", 2, "production", None)
REQUEST_ID = "a1b2c3d4e5f6"
NOW = datetime(2030, 3, 1, 12, 0, tzinfo=UTC)
STAMP = "2030-01-01T00:00:00Z"
FIRST_USER = 3
FIRST_ENVIRONMENT = 2
RULE = 4
RUN = 5
SECOND_NS = 1_000_000_000
EPOCH_2026_NS = 1_767_225_600_000_000_000
LINE_BREAKS = ["\N{NEXT LINE}", "\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}"]
EXPORT_PAGE = 2
STORED_ROWS = 3
INSERT_ANONYMOUS = (
    "INSERT INTO activity_event (category, actor_type, target_type, target_id, action, result, "
    "detail, occurred_at) VALUES ('authentication', 'anonymous', 'user', ?, 'login_failed', "
    "'failure', ?, ?)"
)


def event(**fields: Any) -> ActivityEvent:
    values: dict[str, Any] = {
        "id": 1,
        "category": "user_action",
        "actor_type": "user",
        "actor_user_id": None,
        "actor_subsystem": None,
        "rule_id": None,
        "job_run_id": None,
        "request_id": None,
        "environment_id": None,
        "target_type": "container",
        "target_id": None,
        "action": "container_started",
        "result": "success",
        "detail": None,
        "occurred_at": STAMP,
    }
    return ActivityEvent(**(values | fields))


def row_of(
    *, username: str | None = None, environment: str | None = None, **fields: Any
) -> ExportRow:
    return ExportRow(event(**fields), username, environment)


def records(data: bytes) -> list[dict[str, Any]]:
    assert data.endswith(b"\n")
    return [json.loads(line) for line in data.decode("utf-8").split("\n")[:-1]]


def log_line(
    text: str = "hello", stamp: int | None = EPOCH_2026_NS, stream: Stream = "stdout"
) -> LogLine:
    return LogLine(stream, text, None, stamp)


def test_a_record_names_the_schema_version_then_every_csv_column_in_the_csv_order() -> None:
    [record] = records(ndjson_rows([row_of()]))

    assert list(record) == ["schema_version", *CSV_COLUMNS]
    assert record["schema_version"] == SCHEMA_VERSION


def test_a_record_carries_numbers_as_numbers_and_the_detail_as_an_object() -> None:
    detail: dict[str, DetailValue] = {
        "rule_name": "nightly",
        "reason": "budget",
        "message": "stopped",
        "attempt": 3,
        "forced": True,
    }
    row = row_of(
        username="operator",
        environment="production",
        id=7,
        actor_user_id=FIRST_USER,
        actor_subsystem="container-health-checker",
        rule_id=RULE,
        job_run_id=RUN,
        request_id=REQUEST_ID,
        environment_id=FIRST_ENVIRONMENT,
        target_id="c1",
        detail=detail,
    )

    [record] = records(ndjson_rows([row]))

    assert record == {
        "schema_version": SCHEMA_VERSION,
        "id": 7,
        "occurred_at": STAMP,
        "category": "user_action",
        "actor_type": "user",
        "actor_user_id": FIRST_USER,
        "actor_username": "operator",
        "actor_subsystem": "container-health-checker",
        "rule_id": RULE,
        "rule_name": "nightly",
        "job_run_id": RUN,
        "request_id": REQUEST_ID,
        "environment_id": FIRST_ENVIRONMENT,
        "environment_name": "production",
        "target_type": "container",
        "target_id": "c1",
        "action": "container_started",
        "result": "success",
        "reason": "budget",
        "message": "stopped",
        "detail": detail,
    }


def test_every_key_is_present_and_null_when_the_event_has_no_such_value() -> None:
    [record] = records(ndjson_rows([row_of()]))

    nullable = [
        "actor_user_id",
        "actor_username",
        "actor_subsystem",
        "rule_id",
        "rule_name",
        "job_run_id",
        "request_id",
        "environment_id",
        "environment_name",
        "target_id",
        "reason",
        "message",
        "detail",
    ]
    assert {key: record[key] for key in nullable} == dict.fromkeys(nullable)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("budget", "budget"), (5, "5"), (True, "true"), (False, "false"), (None, None)],
)
def test_reason_and_message_are_strings_whatever_type_the_detail_holds(
    reason: DetailValue, expected: str | None
) -> None:
    [record] = records(ndjson_rows([row_of(detail={"reason": reason, "message": reason})]))

    assert (record["reason"], record["message"]) == (expected, expected)
    assert record["detail"] == {"reason": reason, "message": reason}


def test_detail_keys_are_written_in_sorted_order() -> None:
    [record] = records(ndjson_rows([row_of(detail={"zeta": 1, "alpha": 2, "mid": 3})]))

    assert list(record["detail"]) == ["alpha", "mid", "zeta"]


def test_each_row_is_one_compact_line_ending_in_a_newline_with_no_header_or_byte_order_mark() -> (
    None
):
    data = ndjson_rows([row_of(id=1), row_of(id=2)])

    assert data.count(b"\n") == 2
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b'": ' not in data and b'", ' not in data
    assert [record["id"] for record in records(data)] == [1, 2]


def test_no_rows_encode_to_nothing_and_there_is_no_header() -> None:
    encoding = export_encoding("ndjson")

    assert (encoding.rows([]), encoding.header()) == (b"", b"")


@pytest.mark.parametrize("text", ["=1+1", "+1", "-1", "@sum", "\tx", "\nx"])
def test_a_value_a_spreadsheet_would_read_as_a_formula_is_not_altered(text: str) -> None:
    [record] = records(ndjson_rows([row_of(target_id=text)]))

    assert record["target_id"] == text


def test_text_outside_ascii_is_written_as_utf_8_not_as_escapes() -> None:
    data = ndjson_rows([row_of(target_id="café ☃")])

    assert "café ☃".encode() in data
    assert records(data)[0]["target_id"] == "café ☃"


@pytest.mark.parametrize("separator", LINE_BREAKS)
def test_a_unicode_line_break_in_a_value_is_escaped_so_the_record_stays_on_one_line(
    separator: str,
) -> None:
    text = f"before{separator}after"

    data = ndjson_rows([row_of(target_id=text, detail={"message": text}), row_of(id=2)])

    assert separator.encode() not in data
    assert len(data.decode("utf-8").splitlines()) == 2
    first = records(data)[0]
    assert (first["target_id"], first["message"], first["detail"]) == (
        text,
        text,
        {"message": text},
    )


def test_a_lone_surrogate_is_replaced_and_the_rest_of_the_stream_survives() -> None:
    data = ndjson_rows([row_of(id=1, target_id="bad\ud800name"), row_of(id=2)])

    first, second = records(data)
    assert first["target_id"] == "bad?name"
    assert second["id"] == 2


def test_the_ndjson_encoding_names_its_suffix_and_media_type() -> None:
    encoding = export_encoding("ndjson")

    assert (encoding.suffix, encoding.media_type) == (".ndjson", NDJSON_MEDIA_TYPE)
    assert NDJSON_MEDIA_TYPE == "application/x-ndjson"


def test_the_csv_encoding_is_the_csv_writer_unchanged() -> None:
    encoding = export_encoding("csv")
    rows = [row_of(id=9, target_id="a,b")]

    assert encoding.header() == csv_header()
    assert encoding.rows(rows) == csv_rows(rows)
    assert (encoding.suffix, encoding.media_type) == (".csv", "text/csv; charset=utf-8")


def test_an_object_encodes_to_one_line_in_the_order_given() -> None:
    assert encode_object({"b": 1, "a": [1, 2]}) == b'{"b":1,"a":[1,2]}\n'


@pytest.mark.parametrize(
    ("nanoseconds", "expected"),
    [
        (EPOCH_2026_NS, "2026-01-01T00:00:00.000000000Z"),
        (EPOCH_2026_NS + 1, "2026-01-01T00:00:00.000000001Z"),
        (EPOCH_2026_NS + 123_456_789, "2026-01-01T00:00:00.123456789Z"),
        (EPOCH_2026_NS + 61 * SECOND_NS, "2026-01-01T00:01:01.000000000Z"),
        (0, "1970-01-01T00:00:00.000000000Z"),
        (None, None),
    ],
)
def test_a_log_line_time_is_utc_with_nine_fractional_digits_or_null(
    nanoseconds: int | None, expected: str | None
) -> None:
    assert line_time(log_line(stamp=nanoseconds)) == expected


def test_a_log_record_has_the_common_keys_then_the_context_then_the_source_keys_in_order() -> None:
    line = log_line("hello", EPOCH_2026_NS + 5, "stderr")

    [record] = records(encode_log_line(line, CONTEXT, {"service_id": "s" * 25, "task_id": None}))

    assert list(record.items()) == [
        ("schema_version", LOG_SCHEMA_VERSION),
        ("time", "2026-01-01T00:00:00.000000005Z"),
        ("stream", "stderr"),
        ("format", "text"),
        ("message", "hello"),
        ("instance_name", "dockmin"),
        ("environment_id", FIRST_ENVIRONMENT),
        ("environment_name", "production"),
        ("host_name", None),
        ("service_id", "s" * 25),
        ("task_id", None),
    ]


def test_a_log_record_of_a_line_without_a_time_has_a_null_time() -> None:
    [record] = records(encode_log_line(log_line(stamp=None), CONTEXT, {}))

    assert record["time"] is None


def test_a_container_source_names_the_compose_labels_or_null() -> None:
    labelled = container_source(
        container_id="c" * 64,
        container_name="web",
        image="nginx",
        labels={"com.docker.compose.project": "shop", "com.docker.compose.service": "web"},
    )
    bare = container_source(container_id="c" * 64, container_name="web", image="nginx", labels={})

    assert list(labelled) == [
        "container_id",
        "container_name",
        "image",
        "compose_project",
        "compose_service",
    ]
    assert (labelled["compose_project"], labelled["compose_service"]) == ("shop", "web")
    assert (bare["compose_project"], bare["compose_service"]) == (None, None)


def test_a_service_source_takes_the_task_and_node_a_line_names_in_place() -> None:
    source = service_source(
        service_id="s" * 25,
        service_name="api",
        image=service_image({"ContainerSpec": {"Image": "api:2"}}),
        labels={"com.docker.stack.namespace": "prod"},
    )

    named = for_task(source, "t" * 25, "n" * 25)

    assert list(named) == [
        "service_id",
        "service_name",
        "task_id",
        "node_id",
        "image",
        "stack_namespace",
    ]
    assert (named["task_id"], named["node_id"], named["image"]) == ("t" * 25, "n" * 25, "api:2")
    assert (source["task_id"], source["node_id"], named["stack_namespace"]) == (None, None, "prod")


def test_a_json_object_line_is_a_payload_with_no_message() -> None:
    [record] = records(encode_log_line(log_line('{"a":1,"b":[true,null]}'), CONTEXT, {}))

    assert record["format"] == "json"
    assert record["payload"] == {"a": 1, "b": [True, None]}
    assert "message" not in record
    assert "level" not in record


def test_a_json_object_line_with_leading_whitespace_is_still_an_object() -> None:
    assert classify('  \t{"a":1}').payload == {"a": 1}


def test_a_key_repeated_in_an_object_keeps_its_last_value() -> None:
    assert classify('{"a":1,"a":2}').payload == {"a": 2}


@pytest.mark.parametrize(
    "text",
    [
        "plain text",
        "",
        "[1,2]",
        '"quoted"',
        "42",
        "true",
        "null",
        '{"a":1',
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":-Infinity}',
        '{"a":1e999}',
        '{"a":1}' + TRUNCATION_MARKER,
        "{" + '"k":' * 5 + "}",
    ],
)
def test_a_line_that_is_not_a_finite_json_object_stays_text(text: str) -> None:
    classified = classify(text)

    assert (classified.format, classified.message, classified.payload) == ("text", text, None)


@pytest.mark.parametrize(
    "text",
    [
        '{"a":"x\\u009b31my"}',
        '{"a":"x\\u001b[31my"}',
        '{"x\\u009b31m":1}',
        '{"x\\u001b[31m":1}',
        '{"outer":[{"a":"\\u009b"}]}',
        '{"a":"x\\u0085y"}',
        '{"a":"x\\u0000y"}',
        '{"a":"x\\u007fy"}',
    ],
)
def test_a_json_line_whose_decoded_string_holds_a_control_sequence_stays_text(text: str) -> None:
    classified = classify(text)

    assert (classified.format, classified.payload, classified.message) == ("text", None, text)


def test_a_json_string_holding_a_newline_a_carriage_return_or_a_tab_stays_json() -> None:
    value = "line1\nline2\tx\r"

    classified = classify(json.dumps({"msg": value, "k\n": 1}))

    assert classified.format == "json"
    assert classified.payload == {"msg": value, "k\n": 1}


def nested(levels: int) -> str:
    return '{"a":' * levels + "1" + "}" * levels


def test_an_object_nested_to_the_depth_limit_is_a_payload() -> None:
    assert classify(nested(MAX_PAYLOAD_DEPTH)).format == "json"


def test_an_object_nested_past_the_depth_limit_stays_text() -> None:
    text = nested(MAX_PAYLOAD_DEPTH + 1)

    assert (classify(text).format, classify(text).message) == ("text", text)


def test_an_array_nested_past_the_depth_limit_inside_an_object_stays_text() -> None:
    text = '{"a":' + "[" * MAX_PAYLOAD_DEPTH + "]" * MAX_PAYLOAD_DEPTH + "}"

    assert classify(text).format == "text"


def test_a_line_nested_far_past_the_parser_recursion_limit_stays_text() -> None:
    assert classify('{"a":' + "[" * 8000).format == "text"


@pytest.mark.parametrize("key", ["level", "severity", "lvl"])
def test_a_string_level_key_gives_a_lower_case_level(key: str) -> None:
    classified = classify(json.dumps({key: "ERROR"}))

    assert classified.level == "error"


def test_the_first_of_level_severity_and_lvl_present_decides() -> None:
    assert classify('{"lvl":"c","severity":"b","level":"a"}').level == "a"
    assert classify('{"lvl":"c","severity":"b"}').level == "b"


@pytest.mark.parametrize(
    "document",
    [
        '{"level":30}',
        '{"level":""}',
        '{"level":null}',
        '{"msg":"x"}',
        '{"level":"' + "x" * 33 + '"}',
    ],
)
def test_a_level_that_is_not_a_short_string_gives_none(document: str) -> None:
    assert classify(document).level is None


def test_a_level_that_is_unusable_falls_through_to_the_next_key() -> None:
    assert classify('{"level":30,"severity":"Warn"}').level == "warn"


def test_a_text_line_has_no_level_and_the_stream_never_gives_one() -> None:
    [record] = records(encode_log_line(log_line("ERROR: boom", stream="stderr"), CONTEXT, {}))

    assert "level" not in record


@pytest.mark.parametrize("separator", LINE_BREAKS)
def test_a_log_message_with_a_unicode_line_break_stays_one_record(separator: str) -> None:
    text = f"one{separator}two"

    data = encode_log_line(log_line(text), CONTEXT, {}) + encode_log_line(
        log_line("next"), CONTEXT, {}
    )

    assert len(data.decode("utf-8").splitlines()) == 2
    assert records(data)[0]["message"] == text


@pytest.mark.parametrize("separator", ["\N{LINE SEPARATOR}", "\N{PARAGRAPH SEPARATOR}"])
def test_a_json_payload_with_a_unicode_line_break_stays_one_record(separator: str) -> None:
    data = encode_log_line(log_line(json.dumps({"m": f"a{separator}b"})), CONTEXT, {})

    assert len(data.decode("utf-8").splitlines()) == 1
    assert records(data)[0]["payload"] == {"m": f"a{separator}b"}


def test_a_notice_is_a_record_with_the_schema_version_and_the_text_alone() -> None:
    assert records(encode_notice("[output stopped]")) == [
        {"schema_version": LOG_SCHEMA_VERSION, "notice": "[output stopped]"}
    ]


async def add_events(db: Session, count: int) -> None:
    for number in range(count):
        await db.execute(
            INSERT_ANONYMOUS, (f"user{number}", json.dumps({"username": f"u{number}"}), STAMP)
        )


@pytest.fixture
def service(db: Session) -> ActivityLogService:
    return ActivityLogService(db=db, clock=lambda: NOW, retention_days=None)


async def drained(stream: ExportStream) -> list[bytes]:
    try:
        return [chunk async for chunk in stream]
    finally:
        await stream.aclose()


async def test_an_ndjson_export_is_one_record_per_stored_event_newest_first_with_no_header(
    service: ActivityLogService, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_events(db, STORED_ROWS)
    monkeypatch.setattr(activity_log, "EXPORT_PAGE_SIZE", EXPORT_PAGE)

    chunks = await drained(service.export(ADMIN, ActivityFilter(), "ndjson"))

    parsed = records(b"".join(chunks))
    assert len(chunks) == 2
    assert [record["id"] for record in parsed] == [3, 2, 1]
    assert {tuple(record) for record in parsed} == {("schema_version", *CSV_COLUMNS)}
    assert parsed[0]["actor_type"] == "anonymous"
    assert parsed[0]["actor_user_id"] is None
    assert parsed[0]["detail"] == {"username": "u2"}
    assert parsed[0]["target_id"] == "user2"


async def test_an_export_without_a_format_is_csv_with_its_header(
    service: ActivityLogService, db: Session
) -> None:
    await add_events(db, 1)

    default = b"".join(await drained(service.export(ADMIN, ActivityFilter())))
    named = b"".join(await drained(service.export(ADMIN, ActivityFilter(), "csv")))

    assert default == named
    assert default.startswith(csv_header())
    assert default.count(b"\r\n") == 2


async def test_an_ndjson_export_of_a_filter_nothing_matches_is_empty(
    service: ActivityLogService, db: Session
) -> None:
    await add_events(db, 1)

    chunks = await drained(service.export(ADMIN, ActivityFilter(action="no_such_action"), "ndjson"))

    assert b"".join(chunks) == b""
