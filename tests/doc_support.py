"""Shared selection of the docs/ files that are published as wiki pages."""

from pathlib import Path

NON_PAGE_FILES = frozenset({"_Sidebar.md", "_Footer.md"})


def is_wiki_page(path: Path) -> bool:
    """Return whether a docs/ file is wiki page source: an uppercase-initial name."""
    return path.name not in NON_PAGE_FILES and path.name[0].isupper()
