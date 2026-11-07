"""CSV encoding of events: column order, quoting that round-trips, formula neutralisation."""

import csv
import io
from typing import Any

import pytest

from app.db.engine import Session
from app.db.repositories.activity_event import ActivityEvent
from app.services.activity_csv import (
    CSV_COLUMNS,
    ExportRow,
    csv_header,
    csv_rows,
    export_rows,
    neutralise,
    resolve_names,
)

QUOTED = 'a,b "quoted"\nsecond line'
BIDI_OVERRIDE = "‮"
UNKNOWN_ID = 9999
STAMP = "2030-01-01T00:00:00Z"
FIRST_USER = 1
FIRST_ENVIRONMENT = 1
ELEVENTH_COLUMN_INDEX = 10
COLUMN = {name: number for number, name in enumerate(CSV_COLUMNS)}


def event(**fields: Any) -> ActivityEvent:
    values: dict[str, Any] = {
        "id": 1,
        "category": "user_action",
        "actor_type": "user",
        "actor_user_id": FIRST_USER,
        "actor_subsystem": None,
        "rule_id": None,
        "job_run_id": None,
        "request_id": None,
        "environment_id": FIRST_ENVIRONMENT,
        "target_type": "container",
        "target_id": "c1",
        "action": "container_started",
        "result": "success",
        "detail": None,
        "occurred_at": STAMP,
    }
    return ActivityEvent(**(values | fields))


def row_of(*, username: str | None = None, **fields: Any) -> ExportRow:
    return ExportRow(event(**fields), username, None)


def parse(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


def cell(data: bytes, column: str) -> str:
    return parse(data)[0][COLUMN[column]]


NEUTRALISED = [
    "=1+1",
    "+1",
    "-1",
    "@sum",
    "\tx",
    "\rx",
    "\n=cmd",
    " =1",
    "   =1",
    "\N{IDEOGRAPHIC SPACE}@x",
    "\N{FULLWIDTH EQUALS SIGN}1",
    "\N{FULLWIDTH PLUS SIGN}1",
    "\N{FULLWIDTH HYPHEN-MINUS}1",
    "\N{FULLWIDTH COMMERCIAL AT}x",
]
UNCHANGED = ["plain", "a=b", "", "   ", " x=1", "é=1", "1+1", "x-"]


def test_the_header_names_the_columns_in_their_documented_order() -> None:
    assert parse(csv_header()) == [list(CSV_COLUMNS)]
    assert CSV_COLUMNS[:6] == (
        "id",
        "occurred_at",
        "category",
        "actor_type",
        "actor_user_id",
        "actor_username",
    )


def test_rows_use_crlf_line_ends_and_no_byte_order_mark() -> None:
    data = csv_header() + csv_rows([row_of()])

    assert data.count(b"\r\n") == 2
    assert not data.startswith(b"\xef\xbb\xbf")


def test_no_rows_encode_to_nothing_so_the_header_is_written_once_by_the_caller() -> None:
    assert csv_rows([]) == b""


@pytest.mark.parametrize("text", [QUOTED, 'only "quotes"', "a,b", "line\r\nbreak", "tail\n"])
def test_a_cell_with_a_comma_a_quote_or_a_line_break_reads_back_unchanged(text: str) -> None:
    data = csv_rows([row_of(target_id=text)])

    [parsed] = parse(data)
    assert len(parsed) == len(CSV_COLUMNS)
    assert parsed[COLUMN["target_id"]] == text


@pytest.mark.parametrize("text", NEUTRALISED)
def test_a_cell_a_spreadsheet_would_read_as_a_formula_is_prefixed_with_a_quote(text: str) -> None:
    assert neutralise(text) == "'" + text


@pytest.mark.parametrize("text", UNCHANGED)
def test_a_cell_that_cannot_open_a_formula_is_left_alone(text: str) -> None:
    assert neutralise(text) == text


def test_every_text_column_is_neutralised_including_the_names_taken_from_detail() -> None:
    detail = {"rule_name": "=cmd", "reason": "+r", "message": "@m"}

    data = csv_rows([row_of(username="-evil", target_id="=t", action="@a", detail=detail)])

    assert [
        cell(data, name)
        for name in ("actor_username", "target_id", "action", "rule_name", "reason", "message")
    ] == ["'-evil", "'=t", "'@a", "'=cmd", "'+r", "'@m"]


def test_bidirectional_and_other_control_characters_are_kept_in_the_cell() -> None:
    text = f"admin{BIDI_OVERRIDE}\x07gnp.exe"

    data = csv_rows([row_of(target_id=text)])

    assert cell(data, "target_id") == text


def test_detail_is_canonical_json_with_sorted_keys_and_non_ascii_escaped() -> None:
    detail = {"z": 1, "a": True, "message": QUOTED, "word": "é"}

    data = csv_rows([row_of(detail=detail)])

    assert cell(data, "detail") == (
        '{"a":true,"message":"a,b \\"quoted\\"\\nsecond line","word":"\\u00e9","z":1}'
    )


def test_empty_values_are_empty_cells_and_numbers_are_digits() -> None:
    data = csv_rows(
        [row_of(actor_type="anonymous", actor_user_id=None, environment_id=None, rule_id=7)]
    )

    assert [cell(data, name) for name in ("actor_user_id", "environment_id", "rule_id")] == [
        "",
        "",
        "7",
    ]
    assert [cell(data, name) for name in ("actor_username", "detail", "message")] == ["", "", ""]


def test_the_request_id_is_column_eleven_after_the_run_and_empty_when_there_is_none() -> None:
    with_id = csv_rows([row_of(request_id="abc.1-2_3")])
    without = csv_rows([row_of()])

    assert (
        CSV_COLUMNS.index("request_id")
        == CSV_COLUMNS.index("job_run_id") + 1
        == ELEVENTH_COLUMN_INDEX
    )
    assert (cell(with_id, "request_id"), cell(without, "request_id")) == ("abc.1-2_3", "")


def test_a_lone_surrogate_in_a_cell_becomes_a_question_mark_and_does_not_end_the_stream() -> None:
    data = csv_rows([row_of(id=1, target_id="a\ud800b"), row_of(id=2, target_id="after")])

    parsed = parse(data)
    assert [row[COLUMN["target_id"]] for row in parsed] == ["a?b", "after"]


async def test_a_name_that_no_longer_exists_is_empty_and_the_rest_are_resolved(
    db: Session,
) -> None:
    await db.execute(
        "INSERT INTO user (id, username, password_hash, role, created_at) "
        "VALUES (?, 'operator', 'x', 'administrator', ?)",
        (FIRST_USER, STAMP),
    )
    await db.execute(
        "INSERT INTO environment (id, name, connection_kind, socket_path, created_at, "
        "updated_at) VALUES (?, 'host', 'unix_socket', '/s', ?, ?)",
        (FIRST_ENVIRONMENT, STAMP, STAMP),
    )
    events = [
        event(id=1),
        event(id=2, actor_user_id=UNKNOWN_ID, environment_id=UNKNOWN_ID),
        event(id=3, actor_type="anonymous", actor_user_id=None, environment_id=None),
    ]

    usernames, names = await resolve_names(db, events)
    rows = await export_rows(db, events)

    assert (usernames, names) == ({FIRST_USER: "operator"}, {FIRST_ENVIRONMENT: "host"})
    assert [(row.actor_username, row.environment_name) for row in rows] == [
        ("operator", "host"),
        (None, None),
        (None, None),
    ]
