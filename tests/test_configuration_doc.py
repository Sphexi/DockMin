"""docs/Reference-Configuration.md and CONTRIBUTING.md agree with the settings model and Compose."""

import re
import types
import typing
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, get_args, get_origin

import pytest
from annotated_types import Ge, Gt, Le, MaxLen, MinLen
from pydantic import SecretStr
from pydantic.fields import FieldInfo

from app.config import ENV_PREFIX, Settings
from app.selfcheck import SQLITE_TMPDIR_VAR
from tests.doc_support import is_wiki_page
from updater.cli import UPDATE_ID_ENVIRONMENT

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CONFIGURATION = DOCS / "Reference-Configuration.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"

SETTINGS_HEADER = ["Variable", "Type", "Default", "Range", "Effect"]
OTHER_HEADER = ["Variable", "Read by", "Effect"]
PROCESS_READER = "process"
RANGE_DASH = "\u2013"
NO_RANGE = "\u2014"
ABSENT_ROW = dict.fromkeys(SETTINGS_HEADER, "(no row in the document)")

TYPE_WORDS: dict[Any, str] = {
    str: "string",
    int: "integer",
    bool: "boolean",
    Path: "path",
    timedelta: "duration (seconds)",
    list[str]: "list (comma-separated)",
    SecretStr: "secret",
}

VARIABLE_CELL = re.compile(r"`([A-Za-z0-9_]+)`")
VARIABLE_MENTION = re.compile(rf"{ENV_PREFIX}[A-Z0-9_]+")
INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
BACKTICKED = re.compile(r"`([^`]+)`")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def _table_rows(path: Path, header: list[str]) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        if line.startswith("|") and _cells(line) == header:
            for body in lines[index + 2 :]:
                if not body.startswith("|"):
                    break
                rows.append(dict(zip(header, _cells(body), strict=True)))
    return rows


def _variable(row: dict[str, str]) -> str:
    match = VARIABLE_CELL.fullmatch(row["Variable"])
    return match.group(1) if match else row["Variable"]


def _without_none(annotation: Any) -> Any:
    if get_origin(annotation) in (typing.Union, types.UnionType):
        members = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(members) != 1:
            raise AssertionError(f"cannot pick one type from the union {annotation!r}")
        annotation = members[0]
    if isinstance(annotation, typing.TypeAliasType):
        annotation = annotation.__value__
    return annotation


def _number(value: Any) -> Any:
    return int(value.total_seconds()) if isinstance(value, timedelta) else value


def expected_type(field: FieldInfo) -> str | frozenset[str]:
    annotation = _without_none(field.annotation)
    if get_origin(annotation) is Literal:
        return frozenset(get_args(annotation))
    return TYPE_WORDS[annotation]


def documented_type(cell: str) -> str | frozenset[str]:
    if cell.startswith("one of "):
        return frozenset(BACKTICKED.findall(cell))
    return cell


def expected_default(field: FieldInfo) -> str:
    default = field.get_default(call_default_factory=True)
    if default is None:
        return "unset"
    if default == []:
        return "empty"
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    if isinstance(default, timedelta):
        return f"`{int(default.total_seconds())}`"
    if isinstance(default, (list, tuple)):
        return f"`{','.join(str(item) for item in default)}`"
    return f"`{default}`"


def expected_range(field: FieldInfo) -> str:
    bounds = {type(item): item for item in field.metadata}
    if Gt in bounds and {Ge, Le} & bounds.keys():
        raise AssertionError(f"a Gt bound combined with Ge or Le has no documented form: {field}")
    if Gt in bounds:
        return f"> {_number(bounds[Gt].gt)}"
    if (Ge in bounds) != (Le in bounds):
        raise AssertionError(f"a one-sided Ge/Le bound has no documented form: {field}")
    if Ge in bounds:
        return f"{_number(bounds[Ge].ge)}{RANGE_DASH}{_number(bounds[Le].le)}"
    if (MinLen in bounds) != (MaxLen in bounds):
        raise AssertionError(f"a one-sided MinLen/MaxLen bound has no documented form: {field}")
    if MinLen in bounds:
        return f"{bounds[MinLen].min_length}{RANGE_DASH}{bounds[MaxLen].max_length} characters"
    return NO_RANGE


def _name_splits(name: str) -> list[tuple[str, str]]:
    return [
        (name[: end + 1], name[end + 1 :])
        for end in range(len(name) - 1)
        if name[end] == "_" and name[: end + 1] != ENV_PREFIX
    ]


def _quoted(text: str, literal: str) -> bool:
    return f'"{literal}"' in text or f"'{literal}'" in text


def _reads_variable(path: str, name: str) -> bool:
    file = ROOT / path
    if not file.is_file():
        return False
    text = file.read_text(encoding="utf-8")
    if path.startswith("tests/"):
        return name in text or any(
            prefix in text and _quoted(text, suffix) for prefix, suffix in _name_splits(name)
        )
    return re.search(rf"\$\{{{re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def _interpolated_names() -> dict[str, list[str]]:
    files = [
        ROOT / "compose.yaml",
        *sorted(ROOT.glob("examples/**/compose.yaml")),
        *sorted(ROOT.glob("examples/**/*.template")),
    ]
    found: dict[str, list[str]] = {}
    for file in files:
        for name in INTERPOLATION.findall(file.read_text(encoding="utf-8")):
            found.setdefault(name, []).append(file.relative_to(ROOT).as_posix())
    return found


def _read_by_files(row: dict[str, str]) -> list[str]:
    return BACKTICKED.findall(row["Read by"])


FIELDS = Settings.model_fields
SETTINGS_ROWS = _table_rows(CONFIGURATION, SETTINGS_HEADER)
OTHER_ROWS = _table_rows(CONFIGURATION, OTHER_HEADER)
CONTRIBUTOR_ROWS = _table_rows(CONTRIBUTING, OTHER_HEADER)
SETTINGS_BY_VARIABLE = {_variable(row): row for row in SETTINGS_ROWS}
DOCUMENTED = {
    *SETTINGS_BY_VARIABLE,
    *(_variable(row) for row in OTHER_ROWS),
    *(_variable(row) for row in CONTRIBUTOR_ROWS),
}
PROCESS_VARIABLES = [_variable(row) for row in OTHER_ROWS if row["Read by"] == PROCESS_READER]
FILE_READERS = [
    pytest.param(_variable(row), path, id=f"{_variable(row)}-{path}")
    for row in (*OTHER_ROWS, *CONTRIBUTOR_ROWS)
    for path in _read_by_files(row)
]
DOCUMENTS = [
    ROOT / "README.md",
    CONTRIBUTING,
    *sorted(path for path in DOCS.glob("*.md") if is_wiki_page(path)),
]


def _field_variable(name: str) -> str:
    return f"{ENV_PREFIX}{name.upper()}"


def _documented_row(name: str) -> dict[str, str]:
    return SETTINGS_BY_VARIABLE.get(_field_variable(name), ABSENT_ROW)


def test_every_parsed_table_has_rows() -> None:
    tables = {"settings": SETTINGS_ROWS, "other": OTHER_ROWS, "contributor": CONTRIBUTOR_ROWS}

    empty = [name for name, rows in tables.items() if not rows]

    assert empty == []


def test_every_settings_field_has_a_documented_variable() -> None:
    fields = {_field_variable(name) for name in FIELDS}

    missing = fields - set(SETTINGS_BY_VARIABLE)

    assert missing == set()


def test_every_documented_settings_variable_is_a_settings_field() -> None:
    fields = {_field_variable(name) for name in FIELDS}

    extra = set(SETTINGS_BY_VARIABLE) - fields

    assert extra == set()


def test_no_variable_appears_in_two_rows() -> None:
    rows = (*SETTINGS_ROWS, *OTHER_ROWS)

    counts = Counter(_variable(row) for row in rows)

    assert {name: count for name, count in counts.items() if count > 1} == {}


def test_every_variable_cell_is_one_backticked_name() -> None:
    rows = (*SETTINGS_ROWS, *OTHER_ROWS, *CONTRIBUTOR_ROWS)

    malformed = [row["Variable"] for row in rows if not VARIABLE_CELL.fullmatch(row["Variable"])]

    assert malformed == []


@pytest.mark.parametrize("name", FIELDS)
def test_documented_type_equals_the_field_type(name: str) -> None:
    row = _documented_row(name)

    assert documented_type(row["Type"]) == expected_type(FIELDS[name])


@pytest.mark.parametrize("name", FIELDS)
def test_documented_default_equals_the_field_default(name: str) -> None:
    row = _documented_row(name)

    assert row["Default"] == expected_default(FIELDS[name])


@pytest.mark.parametrize("name", FIELDS)
def test_documented_range_equals_the_field_bounds(name: str) -> None:
    row = _documented_row(name)

    assert row["Range"] == expected_range(FIELDS[name])


def test_every_compose_interpolation_is_documented() -> None:
    interpolated = _interpolated_names()

    undocumented = {name: files for name, files in interpolated.items() if name not in DOCUMENTED}

    assert undocumented == {}


@pytest.mark.parametrize(("name", "path"), FILE_READERS)
def test_other_variable_is_read_by_the_file_that_names_it(name: str, path: str) -> None:
    assert _reads_variable(path, name)


def test_every_other_variable_names_files_or_the_process() -> None:
    rows = (*OTHER_ROWS, *CONTRIBUTOR_ROWS)

    unreadable = [
        _variable(row)
        for row in rows
        if row["Read by"] != PROCESS_READER and not _read_by_files(row)
    ]

    assert unreadable == []


@pytest.mark.parametrize("name", PROCESS_VARIABLES)
def test_process_variable_is_one_the_code_reads_outside_the_model(name: str) -> None:
    read_outside_the_model = {SQLITE_TMPDIR_VAR, UPDATE_ID_ENVIRONMENT}

    assert name in read_outside_the_model


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_every_variable_a_document_mentions_is_in_a_table(document: Path) -> None:
    found = VARIABLE_MENTION.findall(document.read_text(encoding="utf-8"))
    mentioned = {name for name in found if not name.endswith("_")}

    unlisted = mentioned - DOCUMENTED

    assert unlisted == set()


SECONDS = timedelta(seconds=1)
DURATION = Annotated[timedelta, Ge(SECONDS), Le(9 * SECONDS)]


def _field_of(annotation: Any) -> FieldInfo:
    return FieldInfo.from_annotation(annotation)


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param(Annotated[int, Ge(1)], id="lower-bound-only"),
        pytest.param(Annotated[int, Le(1)], id="upper-bound-only"),
        pytest.param(Annotated[int, Gt(0), Le(9)], id="exclusive-lower-with-upper"),
        pytest.param(Annotated[str, MinLen(1)], id="minimum-length-only"),
        pytest.param(Annotated[str, MaxLen(9)], id="maximum-length-only"),
    ],
)
def test_a_bound_the_table_cannot_describe_is_refused_rather_than_guessed(
    annotation: Any,
) -> None:
    field = _field_of(annotation)

    with pytest.raises(AssertionError, match="has no documented form"):
        expected_range(field)


def test_duration_bounds_are_documented_in_seconds() -> None:
    field = _field_of(DURATION)

    assert expected_range(field) == f"1{RANGE_DASH}9"


def test_a_union_of_several_types_is_refused_rather_than_resolved_to_the_first() -> None:
    with pytest.raises(AssertionError, match="cannot pick one type"):
        _without_none(int | str | None)
