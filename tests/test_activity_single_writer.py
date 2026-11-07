"""Source scan: only the repository names the activity table, and only the recorder inserts."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
REPOSITORY = "app/db/repositories/activity_event.py"
RECORDER = "app/services/activity.py"
MIGRATIONS = "app/db/migrations/"

TABLE_TOKEN = re.compile(r"\bactivity_event\b")
_TABLE = r"""(?:main\.)?["`\[]?activity_event\b"""
STATEMENTS = {
    "insert": re.compile(rf"\binsert\s+(?:or\s+\w+\s+)?into\s+{_TABLE}", re.IGNORECASE),
    "replace": re.compile(rf"\breplace\s+into\s+{_TABLE}", re.IGNORECASE),
    "update": re.compile(rf"\bupdate\s+(?:or\s+\w+\s+)?{_TABLE}", re.IGNORECASE),
    "delete": re.compile(rf"\bdelete\s+from\s+{_TABLE}", re.IGNORECASE),
}


def statement_kinds(text: str) -> list[str]:
    """Return the kind of every activity_event write statement in the text."""
    return [kind for kind, pattern in STATEMENTS.items() for _ in pattern.finditer(text)]


def string_constants(source: str) -> list[str]:
    """Return every string constant, f-string literal parts and docstrings included."""
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def files_naming_the_table(app: Path) -> list[str]:
    """Return the files outside the repository and migrations whose text names the table."""
    found = []
    for path in sorted([*app.rglob("*.py"), *app.rglob("*.sql")]):
        relative = path.relative_to(app.parent).as_posix()
        if relative == REPOSITORY or relative.startswith(MIGRATIONS):
            continue
        source = path.read_text(encoding="utf-8")
        texts = [source] if path.suffix == ".sql" else string_constants(source)
        if any(TABLE_TOKEN.search(text) for text in texts):
            found.append(relative)
    return found


def references_insert_of_the_repository(source: str) -> bool:
    """Report whether the source reaches the repository and touches an insert attribute."""
    tree = ast.parse(source)
    reaches_repository = any(
        (isinstance(node, ast.Name) and node.id == "ActivityEventRepository")
        or (isinstance(node, ast.Attribute) and node.attr in {"ActivityEventRepository", "_events"})
        or (isinstance(node, ast.alias) and node.name == "ActivityEventRepository")
        for node in ast.walk(tree)
    )
    touches_insert = any(
        (isinstance(node, ast.Attribute) and node.attr == "insert")
        or (isinstance(node, ast.Constant) and node.value == "insert")
        for node in ast.walk(tree)
    )
    return reaches_repository and touches_insert


def files_referencing_the_insert(app: Path) -> list[str]:
    """Return the files other than the repository that reference its insert."""
    found = []
    for path in sorted(app.rglob("*.py")):
        relative = path.relative_to(app.parent).as_posix()
        if relative != REPOSITORY and references_insert_of_the_repository(
            path.read_text(encoding="utf-8")
        ):
            found.append(relative)
    return found


def test_only_the_repository_and_migrations_name_the_activity_table() -> None:
    assert files_naming_the_table(APP) == []


def test_the_repository_holds_one_insert_one_delete_and_no_other_write() -> None:
    source = (ROOT / REPOSITORY).read_text(encoding="utf-8")

    kinds = [kind for text in string_constants(source) for kind in statement_kinds(text)]

    assert sorted(kinds) == ["delete", "insert"]


def test_no_migration_writes_activity_event_rows() -> None:
    found = [
        path.name
        for path in sorted((ROOT / MIGRATIONS).glob("*.sql"))
        if statement_kinds(path.read_text(encoding="utf-8"))
    ]

    assert found == []


def test_only_the_recorder_references_the_repository_insert() -> None:
    assert files_referencing_the_insert(APP) == [RECORDER]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('T = "activity_event"\n', id="constant"),
        pytest.param('SQL = "INSERT INTO " "activity_event (id)"\n', id="adjacent-literals"),
        pytest.param('T = "activity_event"\nSQL = f"INSERT INTO {T} (id)"\n', id="f-string-name"),
        pytest.param('SQL = f"DELETE FROM activity_event WHERE id = {i}"\n', id="f-string-literal"),
        pytest.param('SQL = "INSERT INTO {} (id)".format("activity_event")\n', id="format"),
        pytest.param('def f():\n    """Writes activity_event."""\n', id="docstring"),
        pytest.param('X = "main.activity_event"\n', id="schema-qualified"),
    ],
)
def test_a_string_constant_naming_the_table_is_found(source: str) -> None:
    assert any(TABLE_TOKEN.search(text) for text in string_constants(source))


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('T = "activity_event_archive"\n', id="longer-name"),
        pytest.param('LOG = "activity_event_write_failed"\n', id="log-event-name"),
        pytest.param("from app.db.repositories.activity_event import DetailValue\n", id="import"),
        pytest.param("# activity_event\nx = 1\n", id="comment"),
    ],
)
def test_text_that_only_resembles_the_table_name_is_not_found(source: str) -> None:
    assert not any(TABLE_TOKEN.search(text) for text in string_constants(source))


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "from app.db.repositories.activity_event import ActivityEventRepository\n"
            "async def f(repo: ActivityEventRepository):\n    await repo.insert()\n",
            id="annotated-parameter",
        ),
        pytest.param(
            "from app.db.repositories.activity_event import ActivityEventRepository as R\n"
            "async def f(db):\n    await R(db).insert()\n",
            id="renamed-import",
        ),
        pytest.param(
            "from app.db.repositories import activity_event\n"
            "async def f(db):\n    await activity_event.ActivityEventRepository(db).insert()\n",
            id="module-attribute",
        ),
        pytest.param(
            "class S:\n    async def f(self):\n        await self._events.insert()\n",
            id="events-attribute",
        ),
        pytest.param(
            "class S:\n    async def f(self):\n        await getattr(self._events, 'insert')()\n",
            id="getattr",
        ),
        pytest.param(
            "class S:\n    async def f(self):\n        write = self._events.insert\n",
            id="bound-method-reference",
        ),
    ],
)
def test_a_reference_to_the_repository_insert_is_found(source: str) -> None:
    assert references_insert_of_the_repository(source) is True


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "class S:\n    async def f(self):\n        await self._events.page()\n", id="read-only"
        ),
        pytest.param("async def f(users):\n    await users.insert()\n", id="another-repository"),
    ],
)
def test_reading_the_repository_or_inserting_elsewhere_is_not_a_reference(source: str) -> None:
    assert references_insert_of_the_repository(source) is False


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        pytest.param("insert into activity_event (id) values (1)", "insert", id="lowercase"),
        pytest.param("INSERT\n    INTO   activity_event(id)", "insert", id="spread-over-lines"),
        pytest.param("INSERT OR REPLACE INTO activity_event (id)", "insert", id="insert-or"),
        pytest.param('INSERT INTO "activity_event" (id)', "insert", id="quoted-table"),
        pytest.param("INSERT INTO main.activity_event (id)", "insert", id="schema-qualified"),
        pytest.param("REPLACE INTO activity_event (id)", "replace", id="replace"),
        pytest.param("UPDATE activity_event SET result = 'x'", "update", id="update"),
        pytest.param("UPDATE OR IGNORE activity_event SET result = 'x'", "update", id="update-or"),
        pytest.param("DELETE FROM activity_event WHERE id = 1", "delete", id="delete"),
    ],
)
def test_the_statement_patterns_recognise_each_form_of_write(sql: str, kind: str) -> None:
    assert kind in statement_kinds(sql)


def test_the_statement_patterns_ignore_reads_and_similar_table_names() -> None:
    sql = "SELECT * FROM activity_event; INSERT INTO activity_event_archive (id) VALUES (1)"

    assert statement_kinds(sql) == []
