"""Tests for app/db/sql_text.py."""

import pytest

from app.db.sql_text import normalise_sql

NORMALISATIONS = {
    "line comment": ("CREATE TABLE a (x INTEGER) -- trailing\n", "CREATE TABLE a (x INTEGER)"),
    "block comment": ("CREATE /* note */ TABLE a (x INTEGER)", "CREATE TABLE a (x INTEGER)"),
    "comment between tokens": ("a/**/b", "a b"),
    "comment inside a body": (
        "CREATE TABLE a (\n    -- why\n    x INTEGER,\n    y TEXT\n)",
        "CREATE TABLE a ( x INTEGER, y TEXT )",
    ),
    "whitespace runs": ("CREATE   TABLE\n\t a\r\n(x)", "CREATE TABLE a (x)"),
    "leading and trailing whitespace": ("  \n SELECT 1 \n ", "SELECT 1"),
    "unterminated block comment": ("SELECT 1 /* open", "SELECT 1"),
    "block comments do not nest": ("a /* x /* y */ b */", "a b */"),
    "trailing line comment without a newline": ("SELECT 1 --", "SELECT 1"),
    "line comment ends at a NUL": ("a -- x\x00y\nb", "a \x00y b"),
}
KEPT_VERBATIM = {
    "dashes in a string": "SELECT '-- not a comment'",
    "block opener in a string": "SELECT '/* not a comment */'",
    "escaped quote in a string": "SELECT 'it''s -- still text'",
    "whitespace in a string": "SELECT 'a   b\n c'",
    "dashes in a double-quoted identifier": 'SELECT "a -- b" FROM t',
    "block opener in a backtick identifier": "SELECT `a /* b` FROM t",
    "dashes in a bracketed identifier": "SELECT [a -- b] FROM t",
    "doubled double quote": 'SELECT "a"" -- b" FROM t',
    "unterminated string": "SELECT 'abc -- def",
    "quote inside a bracketed identifier": "SELECT [a 'b] FROM t",
}


@pytest.mark.parametrize(("sql", "expected"), NORMALISATIONS.values(), ids=NORMALISATIONS.keys())
def test_comments_are_removed_and_whitespace_outside_quoting_is_collapsed(
    sql: str, expected: str
) -> None:
    assert normalise_sql(sql) == expected


@pytest.mark.parametrize("sql", KEPT_VERBATIM.values(), ids=KEPT_VERBATIM.keys())
def test_comment_markers_and_whitespace_inside_quoting_are_kept(sql: str) -> None:
    assert normalise_sql(sql) == sql


def test_a_comment_after_a_string_literal_is_removed_and_the_literal_kept() -> None:
    assert normalise_sql("SELECT '--' -- real comment\n, 2") == "SELECT '--' , 2"


def test_string_literals_differing_in_inner_whitespace_normalise_differently() -> None:
    assert normalise_sql("SELECT 'a  b'") != normalise_sql("SELECT 'a b'")
