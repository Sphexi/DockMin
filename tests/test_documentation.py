"""Public documents: links, anchors, repository paths, the README table and route tokens."""

import logging
import posixpath
import re
import shutil
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.routing import iter_route_contexts

from app.config import SECRET_KEY_VAR, load_settings
from app.main import create_app
from tests.conftest import (
    COMPOSE_BINARY_VAR,
    COMPOSE_STUB,
    DATA_DIR_VAR,
    DEVELOPMENT_CHECKS_VAR,
    EXECUTABLE_FILE,
)
from tests.doc_support import is_wiki_page
from tests.support import CHEAP_ARGON2_ENV, VALID_KEY

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIRECTORIES = ("docs", "examples", "app", "tests", "scripts", "updater")
PUBLIC_FILES = (
    "README.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "SECURITY.md",
    "LICENSE",
    "compose.yaml",
    "Dockerfile",
    "Dockerfile.updater",
    "pyproject.toml",
    ".env.example",
)
DOCS = ROOT / "docs"
HOME_PAGE = "Home"
CHANGELOG_PAGE = "Changelog"
CONTRIBUTING_PAGE = "Contributing"
SIDEBAR = "docs/_Sidebar.md"
IMAGE_PREFIX = "images/"
REPOSITORY_URL = "https://github.com/Sphexi/DockMin/"
WIKI_PREFIX = f"{REPOSITORY_URL}wiki/"
BLOB_PREFIX = f"{REPOSITORY_URL}blob/main/"


WIKI_PAGE_NAMES = sorted(path.stem for path in DOCS.glob("*.md") if is_wiki_page(path))
WIKI_PAGE_FILES = [f"docs/{name}.md" for name in WIKI_PAGE_NAMES]
WIKI_TARGETS = sorted({*WIKI_PAGE_NAMES, CHANGELOG_PAGE, CONTRIBUTING_PAGE})
REPOSITORY_DOCUMENTS = [
    document
    for document in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md", "SECURITY.md")
    if (ROOT / document).is_file()
]
WIKI_PUBLISHED_REPOSITORY_DOCUMENTS = [
    document for document in ("CONTRIBUTING.md", "CHANGELOG.md") if (ROOT / document).is_file()
]
WIKI_DOCUMENTS = [
    *WIKI_PAGE_FILES,
    *WIKI_PUBLISHED_REPOSITORY_DOCUMENTS,
    *([SIDEBAR] if (ROOT / SIDEBAR).is_file() else []),
]
DOCUMENTS = [*REPOSITORY_DOCUMENTS, *WIKI_PAGE_FILES]
API_DOCUMENT = "docs/Reference-JSON-API.md"
API_PREFIX = "/api"

README_TABLE_HEADER = ["Document", "Contents"]
API_TABLE_HEADER = ["Method and path", "Role", "Fields"]
EXTERNAL_PATH_PREFIXES = ("/v2/",)
METHODS = "GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS"

FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
INLINE_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
REFERENCE_DEFINITION = re.compile(r"(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*<?(\S+?)>?(?:[ \t]+.*)?$")
AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]*:[^>\s]+)>")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
WIKI_NAME = re.compile(r"^[^/.]+$")
HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
REPOSITORY_PATH = re.compile(
    r"^(?:(?:docs|examples|app|tests|scripts)/[\w./-]*|README\.md|CONTRIBUTING\.md|CHANGELOG\.md"
    r"|SECURITY\.md|LICENSE|compose\.yaml|Dockerfile|pyproject\.toml|\.env\.example)$"
)
ROUTE_TOKEN = re.compile(rf"^({METHODS}) (/\S*)$")
MARKDOWN_LINK_CELL = re.compile(r"^\[[^\]]*\]\(([^)\s]+)\)$")
ROUTE_PARAMETER = re.compile(r"^\{[^}]+\}$")
PLACEHOLDER = re.compile(r"^(?:\{[^}]*\}|<[^>]*>)$")
GLOB = "*"


@dataclass(frozen=True)
class Item:
    document: str
    line: int
    text: str

    @property
    def label(self) -> str:
        return f"{self.document}:{self.line} {self.text}"


@dataclass(frozen=True)
class Scan:
    links: list[Item]
    spans: list[Item]
    headings: list[str]


def _split_fences(lines: list[str]) -> Iterator[tuple[int, str, bool]]:
    """Yield (line number, line, inside a fenced block) for every line."""
    fence: tuple[str, int] | None = None
    for number, line in enumerate(lines, start=1):
        marker = FENCE.match(line)
        if fence is None:
            if marker and not (marker.group(1)[0] == "`" and "`" in marker.group(2)):
                fence = (marker.group(1)[0], len(marker.group(1)))
                yield number, line, True
                continue
            yield number, line, False
            continue
        yield number, line, True
        if (
            marker
            and marker.group(1)[0] == fence[0]
            and len(marker.group(1)) >= fence[1]
            and not marker.group(2).strip()
        ):
            fence = None


def scan_text(document: str, text: str) -> Scan:
    """Collect link targets, code spans and heading texts, ignoring fenced blocks."""
    spans: list[Item] = []
    headings: list[str] = []
    visible: list[str] = []
    for number, line, fenced in _split_fences(text.splitlines()):
        if fenced:
            visible.append("")
            continue
        heading = HEADING.match(line)
        if heading:
            headings.append(heading.group(1))
        for span in CODE_SPAN.finditer(line):
            spans.append(Item(document, number, span.group(2).strip()))
        visible.append(CODE_SPAN.sub(lambda found: "x" * len(found.group(0)), line))
    body = "\n".join(visible)
    links: list[Item] = []
    for pattern in (INLINE_LINK, REFERENCE_DEFINITION, AUTOLINK):
        for found in pattern.finditer(body):
            line_number = body.count("\n", 0, found.start(1)) + 1
            links.append(Item(document, line_number, found.group(1)))
    return Scan(sorted(links, key=lambda item: item.line), spans, headings)


@cache
def scan(document: str) -> Scan:
    return scan_text(document, (ROOT / document).read_text(encoding="utf-8"))


def slug(heading: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "").replace("*", "")
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


def slugs(headings: list[str]) -> set[str]:
    seen: Counter[str] = Counter()
    found: set[str] = set()
    for heading in headings:
        base = slug(heading)
        found.add(base if seen[base] == 0 else f"{base}-{seen[base]}")
        seen[base] += 1
    return found


def split_reference(reference: str) -> tuple[str, str]:
    path, _, fragment = reference.partition("#")
    return unquote(path.partition("?")[0]), unquote(fragment)


def repository_path(document: str, path: str) -> str:
    """Return the repository-relative path a link in a document points to."""
    if not path:
        return document
    joined = (
        path.lstrip("/")
        if path.startswith("/")
        else posixpath.join(posixpath.dirname(document), path)
    )
    return posixpath.normpath(joined)


def under_public_root(relative: str) -> bool:
    return relative in PUBLIC_FILES or relative.split("/")[0] in PUBLIC_DIRECTORIES


def link_problem(link: Item) -> str | None:
    path, _ = split_reference(link.text)
    relative = repository_path(link.document, path)
    if relative.startswith(".."):
        return "leaves the repository"
    if not under_public_root(relative):
        return f"{relative} is not under a public root"
    if not (ROOT / relative).exists():
        return f"{relative} does not exist"
    return None


def anchor_problem(link: Item) -> str | None:
    path, fragment = split_reference(link.text)
    relative = repository_path(link.document, path)
    if fragment not in (slug_set(relative) or set()):
        return f"no heading in {relative} has the slug {fragment}"
    return None


@cache
def slug_set(relative: str) -> set[str] | None:
    """Return the heading slugs of a Markdown file under a public root, else None."""
    target = ROOT / relative
    if not relative.endswith(".md") or not under_public_root(relative) or not target.is_file():
        return None
    return slugs(scan_text(relative, target.read_text(encoding="utf-8")).headings)


def wiki_file(page: str) -> str | None:
    """Return the repository file a wiki page name is published from, else None."""
    if page == CHANGELOG_PAGE:
        return "CHANGELOG.md"
    if page == CONTRIBUTING_PAGE:
        return "CONTRIBUTING.md"
    return f"docs/{page}.md" if page in WIKI_PAGE_NAMES else None


def wiki_destination(link: Item) -> str | None:
    """Return the Markdown file a wiki-page link, same-page anchor or wiki URL points to."""
    if link.text.startswith(WIKI_PREFIX):
        page = split_reference(link.text.removeprefix(WIKI_PREFIX))[0]
    elif SCHEME.match(link.text):
        return None
    else:
        page = split_reference(link.text)[0]
        if not page:
            return link.document
        if not WIKI_NAME.fullmatch(page):
            return None
    return wiki_file(page)


def _blob_problem(url: str) -> str | None:
    path = split_reference(url.removeprefix(BLOB_PREFIX))[0]
    relative = posixpath.normpath(path)
    if not path or relative.startswith(".."):
        return f"{url} names no repository path"
    if not (ROOT / relative).exists():
        return f"{relative} does not exist in the repository"
    return None


def wiki_link_problem(link: Item) -> str | None:
    """Return why a link in a wiki page fails on the published wiki, else None."""
    if SCHEME.match(link.text):
        if link.text.startswith(WIKI_PREFIX):
            return wiki_url_problem(link)
        return _blob_problem(link.text) if link.text.startswith(BLOB_PREFIX) else None
    page = split_reference(link.text)[0]
    if not page:
        return None
    if page.startswith(IMAGE_PREFIX):
        return None if (DOCS / page).is_file() else f"docs/{page} does not exist"
    if not WIKI_NAME.fullmatch(page):
        return f"{page} is a file link, which does not resolve on the wiki"
    return None if wiki_file(page) else f"no wiki page {page}"


def wiki_url_problem(link: Item) -> str | None:
    """Return why a wiki URL names no page, else None."""
    page = split_reference(link.text.removeprefix(WIKI_PREFIX))[0]
    return None if wiki_file(page) else f"no wiki page {page!r}"


def wiki_anchor_problem(link: Item) -> str | None:
    destination = wiki_destination(link)
    fragment = split_reference(link.text)[1]
    if fragment not in (slug_set(destination or "") or set()):
        return f"no heading in {destination} has the slug {fragment}"
    return None


def _links(documents: list[str]) -> list[Item]:
    return [
        link
        for document in documents
        for link in scan(document).links
        if not link.text.lower().startswith("mailto:")
    ]


REPOSITORY_LINKS = _links(REPOSITORY_DOCUMENTS)
WIKI_LINKS = _links(WIKI_DOCUMENTS)
RELATIVE_LINKS = [link for link in REPOSITORY_LINKS if not SCHEME.match(link.text)]
WIKI_URL_LINKS = list(
    dict.fromkeys(
        link for link in (*REPOSITORY_LINKS, *WIKI_LINKS) if link.text.startswith(WIKI_PREFIX)
    )
)
ABSOLUTE_LINKS = [link for link in (*REPOSITORY_LINKS, *WIKI_LINKS) if SCHEME.match(link.text)]


def _has_checkable_anchor(link: Item) -> bool:
    path, fragment = split_reference(link.text)
    return bool(fragment) and slug_set(repository_path(link.document, path)) is not None


def _has_checkable_wiki_anchor(link: Item) -> bool:
    destination = wiki_destination(link)
    fragment = split_reference(link.text)[1]
    return bool(fragment) and destination is not None and slug_set(destination) is not None


ANCHORED_LINKS = [link for link in RELATIVE_LINKS if _has_checkable_anchor(link)]
ANCHORED_WIKI_LINKS = [
    link
    for link in (*(item for item in WIKI_LINKS if not SCHEME.match(item.text)), *WIKI_URL_LINKS)
    if _has_checkable_wiki_anchor(link)
]
PATH_SPANS = [
    span
    for document in DOCUMENTS
    for span in scan(document).spans
    if REPOSITORY_PATH.match(span.text)
]


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]


def table_rows(document: str, header: list[str]) -> list[Item]:
    """Return the first cell of each body row of the table with this header."""
    lines = (ROOT / document).read_text(encoding="utf-8").splitlines()
    rows: list[Item] = []
    for index, line in enumerate(lines):
        if line.startswith("|") and _cells(line) == header:
            for offset, body in enumerate(lines[index + 2 :], start=index + 3):
                if not body.startswith("|"):
                    break
                rows.append(Item(document, offset, _cells(body)[0]))
            break
    return rows


def readme_page(row: Item) -> str:
    """Return the wiki page a README table row links to, or the raw cell if it is not a wiki URL."""
    link = MARKDOWN_LINK_CELL.match(row.text)
    if link and link.group(1).startswith(WIKI_PREFIX):
        return split_reference(link.group(1).removeprefix(WIKI_PREFIX))[0]
    return row.text


README_ROWS = table_rows("README.md", README_TABLE_HEADER)
API_ROWS = table_rows(API_DOCUMENT, API_TABLE_HEADER)
SIDEBAR_PAGE_COUNTS = Counter(
    split_reference(link.text)[0]
    for link in (scan(SIDEBAR).links if SIDEBAR in WIKI_DOCUMENTS else [])
    if not SCHEME.match(link.text)
)
SIDEBAR_REQUIRED = sorted(({*WIKI_PAGE_NAMES} - {HOME_PAGE}) | {CHANGELOG_PAGE, CONTRIBUTING_PAGE})


def route_tokens() -> tuple[list[Item], list[Item]]:
    """Return (tokens to check against the route table, tokens for an external API)."""
    checked: list[Item] = []
    external: list[Item] = []
    for document in DOCUMENTS:
        for span in scan(document).spans:
            if token := ROUTE_TOKEN.match(span.text):
                bucket = external if token.group(2).startswith(EXTERNAL_PATH_PREFIXES) else checked
                bucket.append(span)
    return checked, external


CHECKED_TOKENS, EXTERNAL_TOKENS = route_tokens()


def path_segments(path: str) -> list[str]:
    return [part for part in path.partition("?")[0].strip("/").split("/") if part]


def path_matches(route_path: str, documented: str) -> bool:
    """Compare segment by segment; a route parameter matches any documented segment."""
    route = path_segments(route_path)
    doc = path_segments(documented)
    if doc and doc[-1] == GLOB:
        prefix = doc[:-1]
        return len(route) > len(prefix) and _segments_match(route[: len(prefix)], prefix)
    return len(route) == len(doc) and _segments_match(route, doc)


def _segments_match(route: list[str], doc: list[str]) -> bool:
    return all(
        ROUTE_PARAMETER.match(wanted) or (wanted == given and not PLACEHOLDER.match(given))
        for wanted, given in zip(route, doc, strict=True)
    )


def token_matches(routes: list[tuple[str, str]], token: str) -> bool:
    method, path = token.split(" ", 1)
    return any(
        route_method == method and path_matches(route_path, path)
        for route_method, route_path in routes
    )


@pytest.fixture(scope="module")
def routes(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, str]]:
    """Return (method, path template) for every route of an app built without its lifespan."""
    directory = tmp_path_factory.mktemp("documentation")
    data = directory / "data"
    data.mkdir()
    compose = directory / "docker-compose"
    shutil.copyfile(COMPOSE_STUB, compose)
    compose.chmod(EXECUTABLE_FILE)
    env = {
        SECRET_KEY_VAR: VALID_KEY,
        DATA_DIR_VAR: str(data),
        COMPOSE_BINARY_VAR: str(compose),
        DEVELOPMENT_CHECKS_VAR: "true",
    } | CHEAP_ARGON2_ENV
    root_logger = logging.getLogger()
    handlers, level = list(root_logger.handlers), root_logger.level
    try:
        application = create_app(load_settings(env))
    finally:
        root_logger.handlers[:] = handlers
        root_logger.setLevel(level)
    return sorted(
        {
            (method, context.path)
            for context in iter_route_contexts(application.router.routes)
            if context.path is not None
            for method in context.methods or ()
        }
    )


def _params(items: list[Item]) -> list[object]:
    return [pytest.param(item, id=item.label) for item in items]


@pytest.mark.parametrize("link", _params(RELATIVE_LINKS))
def test_relative_link_resolves_to_a_file_under_a_public_root(link: Item) -> None:
    assert link_problem(link) is None, f"{link.label}: {link_problem(link)}"


@pytest.mark.parametrize("link", _params(ANCHORED_LINKS))
def test_anchor_matches_a_heading_slug_in_its_target(link: Item) -> None:
    assert anchor_problem(link) is None, f"{link.label}: {anchor_problem(link)}"


@pytest.mark.parametrize("link", _params(WIKI_LINKS))
def test_wiki_link_has_a_form_that_resolves_on_the_wiki(link: Item) -> None:
    assert wiki_link_problem(link) is None, f"{link.label}: {wiki_link_problem(link)}"


@pytest.mark.parametrize("link", _params(ANCHORED_WIKI_LINKS))
def test_wiki_anchor_matches_a_heading_slug_on_its_page(link: Item) -> None:
    assert wiki_anchor_problem(link) is None, f"{link.label}: {wiki_anchor_problem(link)}"


@pytest.mark.parametrize("link", _params(WIKI_URL_LINKS))
def test_wiki_url_names_a_wiki_page(link: Item) -> None:
    assert wiki_url_problem(link) is None, f"{link.label}: {wiki_url_problem(link)}"


@pytest.mark.parametrize("span", _params(PATH_SPANS))
def test_backticked_repository_path_exists(span: Item) -> None:
    assert (ROOT / span.text).exists(), f"{span.label} does not exist"


@pytest.mark.parametrize("link", _params(ABSOLUTE_LINKS))
def test_absolute_link_is_a_well_formed_http_url(link: Item) -> None:
    parts = urlsplit(link.text)

    assert parts.scheme in ("http", "https") and parts.netloc, f"{link.label} is not an http(s) URL"


def test_readme_has_a_documentation_table() -> None:
    assert README_ROWS


@pytest.mark.parametrize("row", _params(README_ROWS))
def test_readme_table_row_links_to_a_wiki_page_by_its_url(row: Item) -> None:
    assert readme_page(row) in WIKI_TARGETS, f"{row.label} is not a link to an existing wiki page"


@pytest.mark.parametrize("page", WIKI_TARGETS)
def test_readme_table_lists_each_wiki_page_once(page: str) -> None:
    listed = [row for row in README_ROWS if readme_page(row) == page]

    assert len(listed) == 1, f"README.md lists {page} {len(listed)} times"


def test_sidebar_exists() -> None:
    assert SIDEBAR in WIKI_DOCUMENTS


@pytest.mark.parametrize("page", SIDEBAR_REQUIRED)
def test_sidebar_links_each_wiki_page_exactly_once(page: str) -> None:
    assert SIDEBAR_PAGE_COUNTS[page] == 1, (
        f"{SIDEBAR} links {page} {SIDEBAR_PAGE_COUNTS[page]} times"
    )


def test_sidebar_links_home_at_most_once() -> None:
    assert SIDEBAR_PAGE_COUNTS[HOME_PAGE] <= 1, (
        f"{SIDEBAR} links {HOME_PAGE} {SIDEBAR_PAGE_COUNTS[HOME_PAGE]} times"
    )


def test_api_document_has_a_route_table() -> None:
    assert API_ROWS


@pytest.mark.parametrize("token", _params(CHECKED_TOKENS))
def test_documented_method_and_path_is_a_route(token: Item, routes: list[tuple[str, str]]) -> None:
    assert token_matches(routes, token.text), f"{token.label} matches no route"


def test_every_api_route_is_in_the_api_table_exactly_once(routes: list[tuple[str, str]]) -> None:
    rows = [row.text.strip("`") for row in API_ROWS]
    api_routes = [
        (method, path)
        for method, path in routes
        if path == API_PREFIX or path.startswith(f"{API_PREFIX}/")
    ]
    counts = {
        f"{method} {path}": sum(token_matches([(method, path)], row) for row in rows)
        for method, path in api_routes
    }

    assert {route: count for route, count in counts.items() if count != 1} == {}


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Rate limit", "rate-limit"),
        ("The `/api` routes", "the-api-routes"),
        ("Keys & tokens: a note", "keys--tokens-a-note"),
        ("[Linked](x.md) *text*", "linked-text"),
        ("snake_case name", "snake_case-name"),
    ],
)
def test_slug_follows_the_github_rule(heading: str, expected: str) -> None:
    assert slug(heading) == expected


def test_repeated_headings_get_numbered_suffixes() -> None:
    assert slugs(["Notes", "Notes", "Notes"]) == {"notes", "notes-1", "notes-2"}


def test_scan_ignores_links_spans_and_headings_inside_fenced_blocks() -> None:
    text = (
        "```bash\n# Hidden\n[a](Gone-Page) `docs/Gone-Page.md`\n```\n"
        "# Shown\n[b](Shown-Page) `docs/Shown-Span.md`\n"
    )

    result = scan_text("docs/Home.md", text)

    assert [item.text for item in result.links] == ["Shown-Page"]
    assert [item.text for item in result.spans] == ["docs/Shown-Span.md"]
    assert result.headings == ["Shown"]


def test_scan_ignores_a_link_written_inside_a_code_span() -> None:
    assert scan_text("docs/Home.md", "Write `[a](Gone-Page)` to link.\n").links == []


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Home.md", True),
        ("Usage-Stacks.md", True),
        ("_Sidebar.md", False),
        ("_Footer.md", False),
        ("stacks.md", False),
    ],
)
def test_wiki_page_selection_takes_uppercase_names_and_skips_special_and_old_files(
    name: str, expected: bool
) -> None:
    assert is_wiki_page(Path(name)) is expected


@pytest.mark.parametrize(
    ("target", "has_problem"),
    [
        (HOME_PAGE, False),
        (CHANGELOG_PAGE, False),
        (CONTRIBUTING_PAGE, False),
        (f"{HOME_PAGE}#anything", False),
        ("#anything", False),
        ("No-Such-Page", True),
        (f"{HOME_PAGE}.md", True),
        (f"docs/{HOME_PAGE}.md", True),
        ("../README.md", True),
        (f"{IMAGE_PREFIX}no-such-image.png", True),
        ("https://example.com/anything", False),
        (f"{BLOB_PREFIX}README.md#L1", False),
        (f"{BLOB_PREFIX}no-such-path.md", True),
        (f"{WIKI_PREFIX}{HOME_PAGE}#anything", False),
        (f"{WIKI_PREFIX}No-Such-Page", True),
    ],
)
def test_wiki_link_problem_accepts_page_names_and_rejects_file_links(
    target: str, has_problem: bool
) -> None:
    link = Item(f"docs/{HOME_PAGE}.md", 1, target)

    assert (wiki_link_problem(link) is not None) is has_problem


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (CHANGELOG_PAGE, "CHANGELOG.md"),
        (f"{WIKI_PREFIX}{CONTRIBUTING_PAGE}#x", "CONTRIBUTING.md"),
        ("#x", f"docs/{HOME_PAGE}.md"),
        ("No-Such-Page", None),
    ],
)
def test_wiki_destination_maps_pages_urls_and_same_page_anchors_to_files(
    target: str, expected: str | None
) -> None:
    assert wiki_destination(Item(f"docs/{HOME_PAGE}.md", 1, target)) == expected


@pytest.mark.parametrize(
    ("route", "documented", "expected"),
    [
        ("/jobs/{job_id}/run", "/jobs/{id}/run", True),
        ("/jobs/{job_id}", "/jobs/-", True),
        ("/jobs/-/runs/{run_id}", "/jobs/{id}/runs/{run_id}", False),
        ("/restore/progress/{restore_id}", "/restore/progress/*", True),
        ("/restore/progress", "/restore/progress/*", False),
        ("/images/prune", "/images/prune?mode={mode}", True),
        ("/jobs", "/jobs/{id}", False),
    ],
)
def test_path_match_compares_segments_and_treats_placeholders_as_parameters(
    route: str, documented: str, expected: bool
) -> None:
    assert path_matches(route, documented) is expected
