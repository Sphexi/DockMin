"""Read side of the template catalogues: what an enabled catalogue offers the signed-in user."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Final

from app.auth.principal import Principal
from app.db.engine import Session
from app.db.repositories.template_catalogue import StoredCatalogue, TemplateCatalogueRepository
from app.services.app_template import (
    AppTemplate,
    TemplateDocument,
    parse_template_ref,
    template_ref,
)
from app.services.template_catalogues import MAX_CATALOGUES

DEFAULT_LIMIT: Final = 24
MAX_LIMIT: Final = 200
MAX_SEARCH_CHARS: Final = 200
_CACHED_DOCUMENTS: Final = MAX_CATALOGUES

_parsed: OrderedDict[str, tuple[AppTemplate, ...]] = OrderedDict()


@dataclass(frozen=True, slots=True)
class CatalogueSummary:
    """An enabled catalogue that holds templates."""

    id: int
    name: str
    entry_count: int


@dataclass(frozen=True, slots=True)
class TemplateEntry:
    """A template with the catalogue it came from and its `<catalogue id>:<key>` reference."""

    ref: str
    catalogue_id: int
    catalogue_name: str
    template: AppTemplate


@dataclass(frozen=True, slots=True)
class TemplatePage:
    """One slice of the matching templates, ordered by title."""

    items: list[TemplateEntry]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        """Report whether more matches follow this slice."""
        return self.offset + len(self.items) < self.total


def _parse_content(content: str) -> tuple[AppTemplate, ...]:
    return TemplateDocument.model_validate_json(content).templates


def _matches(entry: TemplateEntry, terms: list[str], category: str | None) -> bool:
    template = entry.template
    if category is not None and category.casefold() not in {
        c.casefold() for c in template.categories
    }:
        return False
    if not terms:
        return True
    haystack = " ".join((template.title, template.description, *template.categories)).casefold()
    return all(term in haystack for term in terms)


def _select(entries: list[TemplateEntry], search: str, category: str | None) -> list[TemplateEntry]:
    terms = search.casefold().split()
    found = [entry for entry in entries if _matches(entry, terms, category)]
    found.sort(
        key=lambda e: (e.template.title.casefold(), e.catalogue_name.casefold(), e.template.key)
    )
    return found


class TemplateReader:
    """Lists and fetches templates from enabled catalogues for any signed-in user.

    A template marked administrator-only is invisible to everyone else: it is not listed, not
    counted, and fetching it answers None like a missing one.
    """

    def __init__(self, db: Session) -> None:
        self._catalogues = TemplateCatalogueRepository(db)

    async def catalogues(self, actor: Principal) -> list[CatalogueSummary]:
        """Return the enabled catalogues that hold templates the actor may see, by name."""
        stored = await self._catalogues.enabled_with_content(limit=MAX_CATALOGUES)
        summaries = [
            CatalogueSummary(item.id, item.name, len(await self._catalogue_entries(actor, item)))
            for item in stored
        ]
        return [summary for summary in summaries if summary.entry_count]

    async def categories(self, actor: Principal) -> list[str]:
        """Return every category of the templates the actor may see, ignoring case, sorted."""
        entries = await self._entries(actor, None)
        names: dict[str, str] = {}
        for entry in entries:
            for name in entry.template.categories:
                names.setdefault(name.casefold(), name)
        return [names[key] for key in sorted(names)]

    async def list_templates(
        self,
        actor: Principal,
        *,
        search: str = "",
        category: str | None = None,
        catalogue_id: int | None = None,
        offset: int = 0,
        limit: int = DEFAULT_LIMIT,
    ) -> TemplatePage:
        """Return one slice of the templates matching a search, a category and a catalogue.

        The search matches every word against title, description and categories, ignoring case.
        """
        limit = min(max(limit, 1), MAX_LIMIT)
        offset = max(offset, 0)
        entries = await self._entries(actor, catalogue_id)
        found = await asyncio.to_thread(_select, entries, search[:MAX_SEARCH_CHARS], category)
        return TemplatePage(found[offset : offset + limit], len(found), offset, limit)

    async def get_template(self, actor: Principal, ref: str) -> TemplateEntry | None:
        """Return the template a `<catalogue id>:<key>` reference names, or None.

        None covers an unknown key, a catalogue that is missing, disabled or empty, and an
        administrator-only template for anyone else.
        """
        parsed = parse_template_ref(ref)
        if parsed is None:
            return None
        stored = await self._catalogues.get(parsed.catalogue_id)
        if stored is None or not stored.is_enabled or stored.content_digest is None:
            return None
        for entry in await self._catalogue_entries(actor, stored):
            if entry.template.key == parsed.key:
                return entry
        return None

    async def _entries(self, actor: Principal, catalogue_id: int | None) -> list[TemplateEntry]:
        stored = await self._catalogues.enabled_with_content(limit=MAX_CATALOGUES)
        entries: list[TemplateEntry] = []
        for catalogue in stored:
            if catalogue_id is None or catalogue.id == catalogue_id:
                entries.extend(await self._catalogue_entries(actor, catalogue))
        return entries

    async def _catalogue_entries(
        self, actor: Principal, stored: StoredCatalogue
    ) -> list[TemplateEntry]:
        digest = stored.content_digest
        if digest is None:
            return []
        templates = _parsed.get(digest)
        if templates is None:
            content = await self._catalogues.content_of(stored.id, digest=digest)
            if content is None:
                return []
            templates = await asyncio.to_thread(_parse_content, content)
            _parsed[digest] = templates
            while len(_parsed) > _CACHED_DOCUMENTS:
                _parsed.popitem(last=False)
        else:
            _parsed.move_to_end(digest)
        return [
            TemplateEntry(template_ref(stored.id, template.key), stored.id, stored.name, template)
            for template in templates
            if actor.is_administrator or not template.administrator_only
        ]
