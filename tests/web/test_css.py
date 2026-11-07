"""Tests for app/static/css/: the token layer and the rule that components hold no colour."""

import re
from pathlib import Path

import pytest

from app.web.templating import APP_DIR, STATIC_DIR
from tests.web.helpers import (
    COMPONENT_SHEETS,
    CSS_DIR,
    colour_literals,
    css_rules,
    declarations,
    relative_name,
    strip_css_comments,
    strip_jinja_comments,
    template_files,
)

LIGHT = ":root"
DARK = '[data-theme="dark"]'
SYSTEM_DARK = ":root:not([data-theme])"
SYSTEM_DARK_BLOCK = re.compile(
    r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)\s*\{\s*:root:not\(\[data-theme\]\)\s*\{"
)
THEMED_PREFIXES = ("--color-", "--shadow-")
THEME_INDEPENDENT = frozenset({"--color-clear"})
FOCUS_RESET_SELECTORS = frozenset({"main:focus"})
PAIRED_RESET_SUFFIX = ":focus:not(:focus-visible)"
OUTLINE_REMOVED = re.compile(r"(?:outline|outline-style)\s*:\s*(?:none|0)\b")
POSITIVE_TABINDEX = re.compile(r"""tabindex\s*=\s*["']?\s*([1-9]\d*)""", re.IGNORECASE)
CLASS_SELECTOR = re.compile(r"\.([A-Za-z_][\w-]*)")
ATTRIBUTE_SELECTOR = re.compile(r"\[[^\]]*\]")
STATIC_REFERENCE = re.compile(r"""static_path\(\s*['"]([^'"]+)['"]""")
STATIC_LITERAL = re.compile(r"""/static/([\w./-]+)""")
CSS_URL = re.compile(r"""url\(\s*['"]?([^'")]+)""")
CLASSES_NOT_WRITTEN_IN_SOURCE = frozenset({"htmx-request", "visible-char"})
SCRIPT_SHEET_SUFFIXES = (".html", ".js", ".py")
BUILD_ARTEFACTS = ("package.json", "package-lock.json", "*.map", "node_modules")


def _tokens() -> str:
    return (CSS_DIR / "tokens.css").read_text(encoding="utf-8")


def _rule(selector: str) -> dict[str, str]:
    return next(properties for found, properties in css_rules(_tokens()) if found == selector)


@pytest.mark.parametrize(
    "value",
    [
        "#fff",
        "#FFFFFF80",
        "rgb(0 0 0 / 0.5)",
        "hsl(10 20% 30%)",
        "1px solid red",
        "color-mix(in srgb, var(--a), var(--b))",
        "0 0 1px 1px rebeccapurple",
    ],
)
def test_colour_scanner_detects_a_literal(value: str) -> None:
    assert colour_literals(value)


@pytest.mark.parametrize(
    "value",
    [
        "var(--color-red)",
        "1px solid var(--color-border)",
        "currentcolor",
        "transparent",
        "0 1px 2px var(--shadow-colour, var(--color-border))",
        '"red"',
        "url(red.png)",
        '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    ],
)
def test_colour_scanner_ignores_references_keywords_and_strings(value: str) -> None:
    assert colour_literals(value) == []


@pytest.mark.parametrize("sheet", COMPONENT_SHEETS)
def test_component_stylesheet_contains_no_colour_literal(sheet: str) -> None:
    pairs = declarations((CSS_DIR / sheet).read_text(encoding="utf-8"))

    offenders = [f"{prop}: {value}" for prop, value in pairs if colour_literals(value)]

    assert pairs
    assert offenders == []


@pytest.mark.parametrize("sheet", COMPONENT_SHEETS)
def test_component_stylesheet_has_no_colour_scheme_media_branch(sheet: str) -> None:
    css = strip_css_comments((CSS_DIR / sheet).read_text(encoding="utf-8"))

    assert "prefers-color-scheme" not in css


def test_system_dark_block_is_a_dark_media_query_scoped_to_unset_theme() -> None:
    assert SYSTEM_DARK_BLOCK.search(strip_css_comments(_tokens()))


def test_system_dark_block_defines_the_same_values_as_the_explicit_dark_block() -> None:
    assert _rule(DARK)
    assert _rule(SYSTEM_DARK) == _rule(DARK)


def test_dark_block_overrides_exactly_the_themed_light_tokens() -> None:
    light = {
        name
        for name in _rule(LIGHT)
        if name.startswith(THEMED_PREFIXES) and name not in THEME_INDEPENDENT
    }
    dark = {name for name in _rule(DARK) if name.startswith("--")}

    assert dark == light


def _sheet(name: str) -> str:
    return (CSS_DIR / name).read_text(encoding="utf-8")


def _outline_removals(css: str) -> list[str]:
    return [
        selector
        for selector, properties in css_rules(css)
        if any(OUTLINE_REMOVED.fullmatch(f"{name}: {value}") for name, value in properties.items())
    ]


def _unpaired(selectors: list[str]) -> list[str]:
    return [
        selector
        for selector in selectors
        if selector not in FOCUS_RESET_SELECTORS and not selector.endswith(PAIRED_RESET_SUFFIX)
    ]


@pytest.mark.parametrize(
    "css",
    [
        ".a:focus { outline: none; }",
        ".a { outline: 0; }",
        ".a { outline-style: none; }",
        ".a { outline:none }",
    ],
)
def test_outline_scanner_detects_a_removed_outline(css: str) -> None:
    assert _outline_removals(css)


@pytest.mark.parametrize(
    "css",
    [
        ".a { outline: var(--focus-width) solid var(--color-focus); }",
        ".a { outline-offset: 0; }",
        ".a { outline: 0.2rem solid red; }",
    ],
)
def test_outline_scanner_accepts_a_drawn_outline(css: str) -> None:
    assert _outline_removals(css) == []


@pytest.mark.parametrize("sheet", COMPONENT_SHEETS)
def test_no_stylesheet_removes_an_outline_outside_the_focus_rules(sheet: str) -> None:
    assert _unpaired(_outline_removals(_sheet(sheet))) == []


def test_every_focusable_element_draws_the_one_focus_style() -> None:
    rules = dict(css_rules(_sheet("base.css")))
    focus = next(
        props for selector, props in rules.items() if selector.startswith(":focus-visible")
    )

    assert focus["outline"] == "var(--focus-width) solid var(--color-focus)"
    assert focus["outline-offset"] == "var(--focus-width)"


@pytest.mark.parametrize("markup", ['<a tabindex="1">', "<a tabindex=2>", "<a tabindex = '3'>"])
def test_tabindex_scanner_detects_a_positive_value(markup: str) -> None:
    assert POSITIVE_TABINDEX.search(markup)


@pytest.mark.parametrize("markup", ['<a tabindex="0">', '<a tabindex="-1">', "<a>"])
def test_tabindex_scanner_accepts_zero_negative_and_absent(markup: str) -> None:
    assert POSITIVE_TABINDEX.search(markup) is None


def test_no_template_sets_a_positive_tabindex() -> None:
    offenders = [
        relative_name(path)
        for path in template_files()
        if POSITIVE_TABINDEX.search(strip_jinja_comments(path.read_text(encoding="utf-8")))
    ]

    assert offenders == []


def _class_selectors() -> dict[str, str]:
    found: dict[str, str] = {}
    for sheet in COMPONENT_SHEETS:
        for selector, _ in css_rules(_sheet(sheet)):
            for name in CLASS_SELECTOR.findall(ATTRIBUTE_SELECTOR.sub("", selector)):
                found.setdefault(name, sheet)
    return found


def _source_corpus() -> str:
    parts = [
        path.read_text(encoding="utf-8")
        for path in sorted(APP_DIR.rglob("*"))
        if path.suffix in SCRIPT_SHEET_SUFFIXES and "vendor" not in path.parts
    ]
    return "\n".join(parts)


CLASSES = _class_selectors()
WORDS = frozenset(re.findall(r"[\w-]+", _source_corpus()))


def test_class_scanner_reads_classes_from_compound_selectors() -> None:
    selector = 'a.btn--x:hover > .y[data-k="a.b"]'

    assert CLASS_SELECTOR.findall(ATTRIBUTE_SELECTOR.sub("", selector)) == ["btn--x", "y"]


def test_stylesheets_declare_class_selectors() -> None:
    assert len(CLASSES) > 0


@pytest.mark.parametrize("name", sorted(CLASSES))
def test_class_selector_is_produced_by_a_template_script_or_python_string(name: str) -> None:
    assert name in CLASSES_NOT_WRITTEN_IN_SOURCE or name in WORDS


@pytest.mark.parametrize("name", sorted(CLASSES_NOT_WRITTEN_IN_SOURCE))
def test_allowlisted_class_is_still_declared_in_a_stylesheet(name: str) -> None:
    assert name in CLASSES


def _own_files() -> list[Path]:
    return [
        path
        for path in sorted(STATIC_DIR.rglob("*"))
        if path.is_file() and "vendor" not in path.relative_to(STATIC_DIR).parts
    ]


@pytest.mark.parametrize("pattern", BUILD_ARTEFACTS)
def test_static_directory_holds_no_build_artefact(pattern: str) -> None:
    assert list(STATIC_DIR.rglob(pattern)) == []


def _referenced_assets() -> dict[str, str]:
    referenced: dict[str, str] = {}
    for path in template_files():
        for target in STATIC_REFERENCE.findall(path.read_text(encoding="utf-8")):
            referenced.setdefault(target, relative_name(path))
    for sheet in COMPONENT_SHEETS:
        for target in CSS_URL.findall(_sheet(sheet)):
            if not target.startswith(("data:", "#")):
                referenced.setdefault(target, sheet)
    for path in _own_files():
        if path.suffix in (".js", ".py"):
            for target in STATIC_LITERAL.findall(path.read_text(encoding="utf-8")):
                referenced.setdefault(target, path.name)
    return referenced


def test_every_referenced_static_asset_exists() -> None:
    referenced = _referenced_assets()

    missing = [target for target in referenced if not (STATIC_DIR / target).is_file()]

    assert referenced
    assert missing == []


def test_every_own_static_file_is_a_stylesheet_script_or_icon() -> None:
    suffixes = {path.suffix for path in _own_files()}

    assert suffixes <= {".css", ".js", ".svg"}
