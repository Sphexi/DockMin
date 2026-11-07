"""Guard: SQL text may appear only in the repositories, the migrations and the engine."""

import ast
import re
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
ALLOWED = (
    APP_DIR / "db" / "repositories",
    APP_DIR / "db" / "migrations",
    APP_DIR / "db" / "engine.py",
)

# Anchored at the start of the literal, so prose that merely contains a word like "select"
# or "update" does not match.
SQL_START = re.compile(
    r"^\s*(?:"
    r"SELECT\s|INSERT\s+(?:OR\s+\w+\s+)?INTO\s|UPDATE\s+\S+\s+SET\s|DELETE\s+FROM\s"
    r"|CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\s|DROP\s+(?:TABLE|INDEX|TRIGGER|VIEW)\s"
    r"|ALTER\s+TABLE\s|PRAGMA\s|VACUUM\b|BEGIN\b|COMMIT\b|ROLLBACK\b|WITH\s+\w+\s+AS\s"
    r")"
)


def sql_literals(source: str) -> list[str]:
    """Return every string literal in source that starts like an SQL statement."""
    literals = (
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    return [text for text in literals if SQL_START.match(text)]


def is_allowed(path: Path) -> bool:
    return any(path == allowed or allowed in path.parents for allowed in ALLOWED)


@pytest.mark.parametrize(
    "source",
    [
        'query = "SELECT id FROM user"',
        'query = "  INSERT INTO user (id) VALUES (1)"',
        'query = "UPDATE user SET role = ?"',
        'query = "DELETE FROM user"',
        'query = f"SELECT {columns} FROM user"',
        'query = ("CREATE TABLE t (id INTEGER)")',
        'query = "PRAGMA foreign_keys = ON"',
        'query = "BEGIN IMMEDIATE"',
    ],
)
def test_sql_literals_flags_a_statement_at_the_start_of_a_string(source: str) -> None:
    assert len(sql_literals(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        'message = "Select a role from the list"',
        'message = "Please update the settings before you begin"',
        'message = "Failed to delete from the cache"',
        'message = "the select element"',
        '"""Fetch a user and update its role."""',
        'label = "Begin"',
    ],
)
def test_sql_literals_ignores_prose_that_mentions_a_keyword(source: str) -> None:
    assert sql_literals(source) == []


def test_sql_appears_nowhere_in_app_outside_the_permitted_modules() -> None:
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}: {text.strip()[:40]}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if not is_allowed(path)
        for text in sql_literals(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
