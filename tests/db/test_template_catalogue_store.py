"""The template_catalogue table: its constraints, guarded writes and reads."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.db.engine import Session
from app.db.repositories.errors import DuplicateError
from app.db.repositories.template_catalogue import (
    CatalogueFields,
    TemplateCatalogueRepository,
    TemplateCatalogueStore,
)

START = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
FIRST_STAMP = "2026-03-01T12:00:00Z"
NEXT_SECOND = "2026-03-01T12:00:01Z"
URL = "https://catalogue.example.test/templates.json"
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
CONTENT = '{"format":1,"templates":[]}'
MAX_CONTENT = 4 * 1024 * 1024
MAX_ENTRIES = 2000
PAGE_SIZE = 2
INSERT = (
    "INSERT INTO template_catalogue (name, kind, url, {extra}created_at, updated_at) "
    "VALUES (?, ?, ?, {marks}'x', 'x')"
)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def fields(name: str, **changes: str) -> CatalogueFields:
    return CatalogueFields(**({"name": name, "kind": "portainer", "url": URL} | changes))


def insert(connection: sqlite3.Connection, column: str, value: object, name: str = "c") -> None:
    connection.execute(
        INSERT.format(extra=f"{column}, ", marks="?, "), (name, "portainer", URL, value)
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(session: Session, clock: Clock) -> TemplateCatalogueStore:
    return TemplateCatalogueStore(session, clock)


@pytest.fixture
def reads(session: Session) -> TemplateCatalogueRepository:
    return TemplateCatalogueRepository(session)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("url", "http://catalogue.example.test/x", id="plain-http"),
        pytest.param("url", "https://", id="url-too-short"),
        pytest.param("kind", "unraid", id="unknown-kind"),
        pytest.param("is_enabled", 2, id="enabled-flag"),
        pytest.param("entry_count", MAX_ENTRIES + 1, id="too-many-entries"),
        pytest.param("content_digest", "ABC", id="digest-format"),
        pytest.param("content_digest", "G" * 64, id="digest-not-hex"),
        pytest.param("last_failure", "x" * 257, id="failure-too-long"),
        pytest.param("content", "x" * (MAX_CONTENT + 1), id="content-over-4-mib"),
    ],
)
def test_the_table_refuses_a_row_that_breaks_its_checks(
    raw_connection: sqlite3.Connection, column: str, value: object
) -> None:
    values = {"name": "c", "kind": "portainer", "url": URL, column: value}
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)

    with pytest.raises(sqlite3.IntegrityError):
        raw_connection.execute(
            f"INSERT INTO template_catalogue ({columns}, created_at, updated_at) "
            f"VALUES ({marks}, 'x', 'x')",
            tuple(values.values()),
        )


def test_the_table_accepts_a_minimal_row_and_defaults_it_to_disabled(
    raw_connection: sqlite3.Connection,
) -> None:
    insert(raw_connection, "content", CONTENT)

    row = raw_connection.execute("SELECT is_enabled, entry_count, content FROM template_catalogue")
    assert tuple(row.fetchone()) == (0, 0, CONTENT)


async def test_a_name_is_unique_ignoring_case(store: TemplateCatalogueStore) -> None:
    await store.add_catalogue(fields("Mine"))

    with pytest.raises(DuplicateError):
        await store.add_catalogue(fields("mine"))


async def test_a_change_applies_only_to_the_stamp_the_form_was_loaded_at(
    store: TemplateCatalogueStore, reads: TemplateCatalogueRepository
) -> None:
    catalogue_id = await store.add_catalogue(fields("one"))

    stale = await store.change_catalogue(
        catalogue_id,
        fields("two"),
        discard_content=False,
        expected_updated_at="2001-01-01T00:00:00Z",
    )
    fresh = await store.change_catalogue(
        catalogue_id, fields("two"), discard_content=False, expected_updated_at=FIRST_STAMP
    )

    stored = await reads.get(catalogue_id)
    assert (stale, fresh) == (False, True)
    assert stored is not None and (stored.name, stored.updated_at) == ("two", NEXT_SECOND)


async def test_storing_a_fetch_keeps_the_content_when_none_is_given_and_a_discard_clears_it(
    store: TemplateCatalogueStore, reads: TemplateCatalogueRepository, clock: Clock
) -> None:
    catalogue_id = await store.add_catalogue(fields("one"))
    await store.store_fetch(
        catalogue_id,
        enable=True,
        entry_count=3,
        content=CONTENT,
        digest=DIGEST,
        expected_updated_at=FIRST_STAMP,
    )
    clock.now += timedelta(minutes=1)
    stamp = (await reads.get(catalogue_id)).updated_at  # type: ignore[union-attr]

    await store.store_fetch(
        catalogue_id,
        enable=False,
        entry_count=4,
        content=None,
        digest=DIGEST,
        expected_updated_at=stamp,
    )

    kept = await reads.get(catalogue_id)
    assert kept is not None
    assert (kept.is_enabled, kept.entry_count, kept.content_digest) == (True, 4, DIGEST)
    assert kept.updated_at == stamp
    assert await reads.content_of(catalogue_id, digest=DIGEST) == CONTENT
    assert await reads.content_of(catalogue_id, digest=OTHER_DIGEST) is None

    await store.change_catalogue(
        catalogue_id,
        fields("one", url="https://other.example.test/x"),
        discard_content=True,
        expected_updated_at=stamp,
    )
    cleared = await reads.get(catalogue_id)
    assert cleared is not None
    assert (
        cleared.is_enabled,
        cleared.entry_count,
        cleared.content_digest,
        cleared.fetched_at,
    ) == (
        False,
        0,
        None,
        None,
    )


async def test_a_fetch_or_failure_for_a_catalogue_changed_meanwhile_writes_nothing(
    store: TemplateCatalogueStore, reads: TemplateCatalogueRepository
) -> None:
    catalogue_id = await store.add_catalogue(fields("one"))

    fetched = await store.store_fetch(
        catalogue_id,
        enable=True,
        entry_count=1,
        content=CONTENT,
        digest=DIGEST,
        expected_updated_at="2001-01-01T00:00:00Z",
    )
    failed = await store.store_fetch_failure(
        catalogue_id, code="network", expected_updated_at="2001-01-01T00:00:00Z"
    )

    stored = await reads.get(catalogue_id)
    assert (fetched, failed) == (False, False)
    assert stored is not None and (stored.is_enabled, stored.last_failure) == (False, None)


async def test_a_removal_needs_the_exact_name_and_the_enabled_listing_skips_unfetched_catalogues(
    store: TemplateCatalogueStore, reads: TemplateCatalogueRepository
) -> None:
    fetched = await store.add_catalogue(fields("b-fetched"))
    await store.store_fetch(
        fetched,
        enable=True,
        entry_count=1,
        content=CONTENT,
        digest=DIGEST,
        expected_updated_at=FIRST_STAMP,
    )
    await store.add_catalogue(fields("a-never-fetched"))
    third = await store.add_catalogue(fields("c-third"))

    listed = await reads.enabled_with_content(limit=10)
    wrong_case = await store.remove_catalogue(third, expected_name="C-THIRD")
    exact = await store.remove_catalogue(third, expected_name="c-third")
    page = await reads.page_catalogues(limit=PAGE_SIZE)

    assert [c.name for c in listed] == ["b-fetched"]
    assert (wrong_case, exact) == (False, True)
    assert [c.name for c in page.items] == [
        "a-never-fetched",
        "b-fetched",
    ] and page.next_cursor is None
    assert await reads.count_catalogues() == PAGE_SIZE
