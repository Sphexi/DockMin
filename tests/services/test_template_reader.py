"""What the signed-in user is offered: enabled catalogues only, filtered, paged, and by role."""

import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from app.auth.principal import Principal
from app.db.engine import Database
from app.services.activity import ActivityRecorder
from app.services.template_catalogues import CatalogueInput, TemplateCatalogueService
from app.services.template_reader import TemplateEntry, TemplatePage, TemplateReader
from tests.job_support import ADMIN, STANDARD
from tests.scheduler.support import Clock
from tests.template_support import (
    PORTAINER_URL,
    ScriptedFetcher,
    container,
    fixture_text,
    portainer_document,
)

type Seed = Callable[[str, str, bytes], Awaitable[int]]

FIRST_PAGE = 2
UNKNOWN_CATALOGUE = 99


@pytest.fixture
async def database(provisioned_template: Path, tmp_path: Path) -> AsyncIterator[Database]:
    path = tmp_path / "dockmin.db"
    shutil.copyfile(provisioned_template, path)
    opened = Database(path)
    yield opened
    await opened.aclose()


@pytest.fixture
def seed(database: Database) -> Seed:
    """Return a function that adds and enables a catalogue serving the given document."""
    clock = Clock()

    async def add(name: str, kind: str, document: bytes) -> int:
        async with database.session() as session:
            service = TemplateCatalogueService(
                session,
                clock,
                ActivityRecorder(session, clock),
                fetcher=ScriptedFetcher(document),
            )
            catalogue_id = await service.create(ADMIN, CatalogueInput(name, kind, PORTAINER_URL))
            await service.enable(ADMIN, catalogue_id)
            return catalogue_id

    return add


async def listed(database: Database, actor: Principal = STANDARD, **options: Any) -> TemplatePage:
    async with database.session() as session:
        return await TemplateReader(session).list_templates(actor, **options)


def titles(page: TemplatePage) -> list[str]:
    return [entry.template.title for entry in page.items]


async def test_every_enabled_catalogue_is_listed_by_title_and_a_disabled_one_is_not(
    database: Database, seed: Seed
) -> None:
    await seed(
        "portainer",
        "portainer",
        portainer_document(container("Zulu", id=1), container("alpha", id=2)),
    )
    await seed("lsio", "linuxserver", fixture_text("linuxserver.json").encode())
    async with database.session() as session:
        clock = Clock()
        service = TemplateCatalogueService(session, clock, ActivityRecorder(session, clock))
        await service.disable(ADMIN, 1)

    page = await listed(database)

    assert titles(page) == ["gluetun", "plex", "swag"] and page.total == 3


async def test_a_search_matches_every_word_against_title_description_and_categories(
    database: Database, seed: Seed
) -> None:
    await seed(
        "portainer",
        "portainer",
        portainer_document(
            container("Redis", id=1, description="In-memory store", categories=["Database"]),
            container("Postgres", id=2, description="Relational store", categories=["Database"]),
            container("Nginx", id=3, description="Web server", categories=["Web"]),
        ),
    )

    by_text = await listed(database, search="STORE relational")
    by_category_word = await listed(database, search="web")
    by_category_filter = await listed(database, category="database")

    assert titles(by_text) == ["Postgres"]
    assert titles(by_category_word) == ["Nginx"]
    assert titles(by_category_filter) == ["Postgres", "Redis"]


async def test_the_catalogue_filter_and_the_category_list_follow_the_catalogue(
    database: Database, seed: Seed
) -> None:
    first = await seed(
        "a", "portainer", portainer_document(container("One", id=1, categories=["x"]))
    )
    second = await seed(
        "b", "portainer", portainer_document(container("Two", id=1, categories=["Y"]))
    )

    only_second = await listed(database, catalogue_id=second)
    async with database.session() as session:
        reader = TemplateReader(session)
        categories = await reader.categories(STANDARD)
        summaries = await reader.catalogues(STANDARD)

    assert titles(only_second) == ["Two"] and only_second.items[0].ref == f"{second}:1"
    assert categories == ["x", "Y"]
    assert [(s.id, s.name, s.entry_count) for s in summaries] == [(first, "a", 1), (second, "b", 1)]


async def test_pagination_returns_slices_with_the_total_and_whether_more_follow(
    database: Database, seed: Seed
) -> None:
    await seed(
        "portainer",
        "portainer",
        portainer_document(*(container(f"App {n}", id=n) for n in range(5))),
    )

    first = await listed(database, limit=FIRST_PAGE)
    last = await listed(database, limit=FIRST_PAGE, offset=4)

    assert titles(first) == ["App 0", "App 1"] and (first.total, first.has_more) == (5, True)
    assert titles(last) == ["App 4"] and last.has_more is False


async def test_administrator_only_templates_and_catalogues_are_hidden_from_a_standard_user(
    database: Database, seed: Seed
) -> None:
    catalogue_id = await seed(
        "portainer",
        "portainer",
        portainer_document(
            container("Open", id=1, categories=["public"]),
            container("Secret", id=2, administrator_only=True, categories=["private"]),
        ),
    )
    hidden_id = await seed(
        "hidden",
        "portainer",
        portainer_document(container("Only", id=1, administrator_only=True)),
    )
    ref = f"{catalogue_id}:2"

    async with database.session() as session:
        reader = TemplateReader(session)
        standard_list = await reader.list_templates(STANDARD)
        standard_get = await reader.get_template(STANDARD, ref)
        standard_categories = await reader.categories(STANDARD)
        standard_summary = await reader.catalogues(STANDARD)
        admin_list = await reader.list_templates(ADMIN)
        admin_get = await reader.get_template(ADMIN, ref)
        admin_summary = await reader.catalogues(ADMIN)

    assert titles(standard_list) == ["Open"] and standard_list.total == 1
    assert standard_get is None and standard_categories == ["public"]
    assert [(s.id, s.entry_count) for s in standard_summary] == [(catalogue_id, 1)]
    assert [s.id for s in admin_summary] == [hidden_id, catalogue_id]
    assert titles(admin_list) == ["Only", "Open", "Secret"]
    assert isinstance(admin_get, TemplateEntry) and admin_get.template.title == "Secret"


async def test_a_template_is_fetched_by_catalogue_and_key_and_anything_else_is_none(
    database: Database, seed: Seed
) -> None:
    catalogue_id = await seed("portainer", "portainer", portainer_document(container("One", id=1)))
    async with database.session() as session:
        clock = Clock()
        other = await TemplateCatalogueService(
            session, clock, ActivityRecorder(session, clock)
        ).create(ADMIN, CatalogueInput("off", "portainer", PORTAINER_URL))

    async with database.session() as session:
        reader = TemplateReader(session)
        found = await reader.get_template(STANDARD, f"{catalogue_id}:1")
        refusals = [
            await reader.get_template(STANDARD, ref)
            for ref in (
                f"{catalogue_id}:2",
                f"{UNKNOWN_CATALOGUE}:1",
                f"{other}:1",
                "1",
                "../1:1",
                f"{catalogue_id}:",
            )
        ]

    assert found is not None and (found.catalogue_name, found.template.image) == (
        "portainer",
        "busybox:latest",
    )
    assert refusals == [None] * 6


async def test_a_disabled_catalogue_stops_offering_a_template_it_once_served(
    database: Database, seed: Seed
) -> None:
    catalogue_id = await seed("portainer", "portainer", portainer_document(container("One", id=1)))
    async with database.session() as session:
        clock = Clock()
        await TemplateCatalogueService(session, clock, ActivityRecorder(session, clock)).disable(
            ADMIN, catalogue_id
        )

    async with database.session() as session:
        assert await TemplateReader(session).get_template(STANDARD, f"{catalogue_id}:1") is None
