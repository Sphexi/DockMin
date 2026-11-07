"""Helpers for the UI shell tests: HTML and CSS scanning and partial macro rendering."""

import json
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from jinja2 import nodes
from markupsafe import escape

from app.db.repositories.pagination import CursorPosition, Page, encode_cursor
from app.services.navigation import NavigationCounts, NavigationEnvironment
from app.services.stack_variable_reveal import RevealedValue
from app.services.stack_views import VariableView
from app.web import (
    auth_routes,
    container_routes,
    encryption_routes,
    environment_routes,
    image_routes,
    network_routes,
    stack_file_routes,
    stack_routes,
    swarm_node_routes,
    swarm_object_routes,
    swarm_routes,
    swarm_service_routes,
    swarm_stack_routes,
    user_routes,
    volume_routes,
)
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from app.web.templating import PARTIALS_PREFIX, STATIC_DIR, TEMPLATES_DIR, build_environment
from tests.docker_engine.container_engine import Box
from tests.web.html_tree import Node, parse

HOSTILE = "\"><script>alert('x')</script>&"
ESCAPED_HOSTILE = str(escape(HOSTILE))
BENIGN = "plain"
TOKEN = "t" * 43

CSS_DIR = STATIC_DIR / "css"
COMPONENT_SHEETS = ("base.css", "components.css", "nojs.css")
URL_ATTRIBUTES = ("href", "src", "action", "hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete")

ENVIRONMENT = build_environment()

# Just enough of a request for route_path to resolve names against the real routers.
ROUTING = FastAPI()
ROUTING.include_router(auth_routes.router)
ROUTING.include_router(user_routes.router)
for _router in (
    encryption_routes.router,
    environment_routes.router,
    container_routes.router,
    image_routes.router,
    network_routes.router,
    stack_routes.router,
    stack_file_routes.router,
    swarm_node_routes.router,
    swarm_object_routes.router,
    swarm_routes.router,
    swarm_service_routes.router,
    swarm_stack_routes.router,
    volume_routes.router,
):
    ROUTING.include_router(_router)
REQUEST = SimpleNamespace(app=ROUTING, scope={})

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_JINJA_SYNTAX = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

_NAMED_COLOUR_LIST = """
    aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue
    blueviolet brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk
    crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki
    darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
    darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue
    dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite
    gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki
    lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
    lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen
    lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen
    magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen
    mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream
    mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
    palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
    powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown
    seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen
    steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow
    yellowgreen"""
_NAMED_COLOURS = frozenset(re.findall(r"[a-z]+", _NAMED_COLOUR_LIST))


@dataclass(frozen=True)
class Tag:
    """A start tag with its attributes, values already unescaped."""

    name: str
    attrs: dict[str, str]


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[Tag] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(Tag(tag, {name: value or "" for name, value in attrs}))


def parse_tags(html: str) -> list[Tag]:
    """Return every start tag in document order."""
    collector = _TagCollector()
    collector.feed(html)
    return collector.tags


def hx_triggers(response: Any) -> set[str]:
    """Return the event names in a response's HX-Trigger header, whichever form it takes."""
    header = response.headers.get("hx-trigger", "").strip()
    if header.startswith("{"):
        return set(json.loads(header))
    return {name.strip() for name in header.split(",") if name.strip()}


@dataclass(frozen=True)
class Tile:
    """One resource tile: its label, count text, and the link it is, None when plain text."""

    label: str
    count: str
    href: str | None
    element: str


def _text_of_class(tile: Node, name: str) -> str:
    return next((n.text() for n in tile.walk() if name in n.classes()), "")


def resource_tiles_in(html: str) -> list[Tile]:
    """Return every `.resource-tile` in document order."""
    return [
        Tile(
            _text_of_class(node, "resource-tile__label"),
            _text_of_class(node, "resource-tile__count"),
            node.attrs.get("href"),
            node.tag,
        )
        for node in parse(html).walk()
        if "resource-tile" in node.classes()
    ]


def links_named(html: str, container_class: str) -> list[tuple[str, str]]:
    """Return (text, href) for every anchor inside elements carrying the class."""
    return [
        (a.text(), a.attrs.get("href", ""))
        for box in parse(html).walk()
        if container_class in box.classes()
        for a in box.walk()
        if a.tag == "a"
    ]


def disclosures_in(html: str) -> dict[str, bool]:
    """Return every disclosure on the page by id, true when it renders open."""
    return {
        node.attrs["id"]: "open" in node.attrs
        for node in parse(html).walk()
        if node.tag == "details" and "disclosure" in node.classes()
    }


def activity_panel_in(html: str) -> tuple[str, list[str]] | None:
    """Return the title and event actions of the page's activity panel, None when it has none."""
    for panel in parse(html).walk():
        if panel.tag == "section" and "activity-panel" in panel.classes():
            title = next(n.text() for n in panel.walk() if n.tag == "h2")
            actions = [
                next(n.text() for n in item.walk() if "mono" in n.classes())
                for item in panel.walk()
                if "activity-list__item" in item.classes()
            ]
            return title, actions
    return None


def facts_under(html: str, heading: str) -> dict[str, str]:
    """Return the term-to-text pairs of the facts list that follows the named heading."""
    for node in parse(html).walk():
        if node.tag in ("h2", "h3", "h4") and node.text() == heading and node.parent is not None:
            siblings = [c for c in node.parent.children if isinstance(c, Node)]
            facts = next((c for c in siblings[siblings.index(node) + 1 :] if c.tag == "dl"), None)
            if facts is None:
                continue
            terms = [c for c in facts.children if isinstance(c, Node)]
            return {t.text(): d.text() for t, d in zip(terms[::2], terms[1::2], strict=True)}
    return {}


_STATE_OVERRIDES: dict[str, dict[str, Any]] = {}
_PLAIN_INSPECT = Box.inspect


def _inspect_with_state(self: Box) -> dict[str, Any]:
    document = _PLAIN_INSPECT(self)
    document["State"] = document["State"] | _STATE_OVERRIDES.get(self.id, {})
    return document


def with_state(monkeypatch: pytest.MonkeyPatch, box: Box, **state: Any) -> None:
    """Make a fake container's inspect document carry extra or replaced State fields."""
    monkeypatch.setattr(Box, "inspect", _inspect_with_state)
    monkeypatch.setitem(_STATE_OVERRIDES, box.id, state)


def run_region_in(html: str) -> Tag | None:
    """Return the run page's live region, None when the markup has none."""
    return next((t for t in parse_tags(html) if t.attrs.get("id") == "run-region"), None)


def refresh_metas_in(html: str) -> list[Tag]:
    """Return every meta element that asks the browser to reload the page."""
    return [
        t
        for t in parse_tags(html)
        if t.name == "meta" and t.attrs.get("http-equiv", "").lower() == "refresh"
    ]


def automation_panel_present(html: str) -> bool:
    """Whether the page renders the rules panel of an object."""
    return any("automation-panel" in node.classes() for node in parse(html).walk())


def urls_in(html: str) -> list[str]:
    """Return the value of every URL-bearing attribute."""
    return [
        tag.attrs[name] for tag in parse_tags(html) for name in URL_ATTRIBUTES if name in tag.attrs
    ]


def same_origin_links(html: str) -> list[str]:
    """Return the anchors that point at a path on this server."""
    hrefs = [
        tag.attrs["href"] for tag in parse_tags(html) if tag.name == "a" and "href" in tag.attrs
    ]
    return [href for href in hrefs if href.startswith("/") and not href.startswith("//")]


async def fetch_statuses(client: AsyncClient, paths: list[str]) -> dict[str, int]:
    """GET each path and map it to its status code."""
    return {path: (await client.get(path)).status_code for path in paths}


def strip_jinja(source: str) -> str:
    """Remove Jinja comments, statements and expressions so the markup parses as HTML."""
    return _JINJA_SYNTAX.sub("", source)


def strip_jinja_comments(source: str) -> str:
    """Remove Jinja comments only."""
    return _JINJA_COMMENT.sub("", source)


def inline_code_in(html: str) -> list[str]:
    """Return a description of every inline script, style or event handler in the markup."""
    found: list[str] = []
    for tag in parse_tags(html):
        if tag.name == "script" and "src" not in tag.attrs:
            found.append("<script> without src")
        if tag.name == "style":
            found.append("<style> element")
        for name, value in tag.attrs.items():
            if name == "style" or re.fullmatch(r"on[a-z]+", name) or name.startswith("hx-on"):
                found.append(f"{tag.name}[{name}]")
            if value.strip().lower().startswith("javascript:"):
                found.append(f"{tag.name}[{name}]=javascript:")
    return found


def template_files() -> list[Path]:
    """Return every template under the templates directory."""
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def relative_name(path: Path) -> str:
    """Return a template path relative to the templates directory, with forward slashes."""
    return path.relative_to(TEMPLATES_DIR).as_posix()


def strip_css_comments(css: str) -> str:
    """Remove block comments."""
    return _CSS_COMMENT.sub("", css)


def declarations(css: str) -> list[tuple[str, str]]:
    """Return every (property, value) pair, including those inside at-rules."""
    pairs: list[tuple[str, str]] = []
    for body in re.findall(r"\{([^{}]*)\}", strip_css_comments(css)):
        for part in body.split(";"):
            prop, separator, value = part.partition(":")
            if separator:
                pairs.append((prop.strip(), value.strip()))
    return pairs


def colour_literals(value: str) -> list[str]:
    """Return the colour literals in a declaration value; var() references are not literals."""
    value = re.sub(r"""("[^"]*"|'[^']*')""", "", value)
    value = re.sub(r"url\([^)]*\)", "", value)
    value = re.sub(r"var\(\s*--[\w-]+", "", value)
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", value)
    functions = re.findall(
        r"\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix|light-dark)\(", value
    )
    names = [word for word in re.findall(r"[a-z]+", value.lower()) if word in _NAMED_COLOURS]
    return hexes + functions + names


def custom_properties(body: str) -> dict[str, str]:
    """Parse a rule body into property to whitespace-normalised value."""
    pairs = (part.partition(":") for part in body.split(";"))
    return {name.strip(): " ".join(value.split()) for name, separator, value in pairs if separator}


def css_rules(css: str) -> list[tuple[str, dict[str, str]]]:
    """Return (selector, properties) for every innermost rule, in source order."""
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", strip_css_comments(css))
    return [(" ".join(selector.split()), custom_properties(body)) for selector, body in rules]


@dataclass(frozen=True)
class MacroSpec:
    """One macro defined in a partial, with the argument facts the generic tests need."""

    partial: str
    name: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    takes_caller: bool


def partial_macros() -> list[MacroSpec]:
    """Discover every macro in the partials directory from the template syntax tree."""
    specs: list[MacroSpec] = []
    for path in sorted((TEMPLATES_DIR / PARTIALS_PREFIX).glob("*.html")):
        tree = ENVIRONMENT.parse(path.read_text(encoding="utf-8"))
        for macro in tree.find_all(nodes.Macro):
            names = tuple(arg.name for arg in macro.args)
            split = len(names) - len(macro.defaults)
            specs.append(
                MacroSpec(
                    partial=path.stem,
                    name=macro.name,
                    required=names[:split],
                    optional=names[split:],
                    takes_caller=any(n.name == "caller" for n in macro.find_all(nodes.Name)),
                )
            )
    return specs


def build_pager(text: str) -> PageView:
    """Return a middle-of-the-list pager whose filter value is text."""
    page = Page(items=[text], next_cursor=encode_cursor(CursorPosition("a", 2)))
    return PageView.of(
        page,
        base_path="/items",
        limit=DEFAULT_LIMIT,
        cursor=encode_cursor(CursorPosition("a", 1)),
        sort="name",
        params=(("q", text),),
    )


# Arguments that are not free text: a choice list, a numeric, or a structure.
_STRUCTURED: dict[str, Any] = {
    "tone": lambda text: "neutral",
    "level": lambda text: "info",
    "state": lambda text: "running",
    "type": lambda text: "text",
    "pager": build_pager,
    "columns": lambda text: [Column("name", text, sortable=True), Column("size", "Size")],
    "rows": lambda text: [
        SimpleNamespace(
            name=text,
            state="running",
            size=1,
            id=1,
            username=text,
            role="standard",
            is_enabled=True,
            last_login_at=None,
        )
    ],
    "steps": lambda text: [SimpleNamespace(state="done", label=text)],
    "variable": lambda text: VariableView(1, text, True, None, text, can_reveal=True),
    "revealed": RevealedValue,
    "environment": lambda text: NavigationEnvironment(
        id=1,
        name=text,
        status="ready",
        connection="local",
        engine_version=None,
        is_swarm=True,
        counts=NavigationCounts(1, 2, 3, 4, 5, 6, 7),
    ),
}
ADMINISTRATOR = SimpleNamespace(is_administrator=True, username="admin")
# Macros that read the signed-in principal from the template context, not from an argument.
MACRO_CONTEXT: dict[str, dict[str, Any]] = {
    "navigation_counts.navigation_counts": {"principal": ADMINISTRATOR},
}
_LEFT_AT_DEFAULT = frozenset({"percent", "poll_seconds", "swap"})


def macro_arguments(spec: MacroSpec, text: str, *, required_only: bool) -> dict[str, Any]:
    """Build keyword arguments for a macro, putting text into every free-text parameter."""
    names = spec.required if required_only else spec.required + spec.optional
    arguments = {
        name: _STRUCTURED[name](text) if name in _STRUCTURED else text
        for name in names
        if name not in _LEFT_AT_DEFAULT
    }
    if spec.takes_caller:
        arguments["caller"] = lambda: text
    return arguments


def render_macro(
    partial: str, name: str, /, *, context: dict[str, Any] | None = None, **arguments: Any
) -> str:
    """Render one partial macro the way render_partial does, with the token and request in scope.

    The leading parameters are positional-only so a macro argument may itself be called `name`.
    """
    scope = (
        {"csrf_token": TOKEN, "request": REQUEST} | MACRO_CONTEXT.get(f"{partial}.{name}", {})
        if context is None
        else context
    )
    module = ENVIRONMENT.get_template(f"{PARTIALS_PREFIX}/{partial}.html").make_module(scope)
    return str(getattr(module, name)(**arguments))


def inputs_named(html: str, name: str) -> list[Tag]:
    """Return every input with this name."""
    return [t for t in parse_tags(html) if t.name == "input" and t.attrs.get("name") == name]


def value_of(html: str, name: str) -> str | None:
    """Return the value attribute of the first input with this name, None when there is none."""
    found = inputs_named(html, name)
    return found[0].attrs.get("value", "") if found else None


def checked_values(html: str, name: str) -> list[str]:
    """Return the value of every checked checkbox or radio with this name."""
    return [t.attrs.get("value", "") for t in inputs_named(html, name) if "checked" in t.attrs]


def hidden_inputs(html: str) -> dict[str, str]:
    """Return the name and value of every hidden input, as a browser would submit them."""
    return {
        t.attrs["name"]: t.attrs.get("value", "")
        for t in parse_tags(html)
        if t.name == "input" and t.attrs.get("type") == "hidden" and "name" in t.attrs
    }


def selected_option(html: str, name: str) -> str | None:
    """Return the selected option of the select with this name, None when there is no select."""
    select = re.search(rf'<select[^>]*\bname="{re.escape(name)}"[^>]*>(.*?)</select>', html, re.S)
    if select is None:
        return None
    chosen = re.search(r'<option value="([^"]*)" selected', select.group(1))
    return chosen.group(1) if chosen else None


def field_error(html: str, field_id: str) -> str | None:
    """Return the text of the error shown beside the field with this id, None when there is none."""
    found = re.search(
        rf'<p class="field__error" id="{re.escape(field_id)}-error">([^<]*)</p>', html
    )
    return None if found is None else unescape(found.group(1))


def form_values(html: str) -> dict[str, str]:
    """Return what a browser would submit for the page's fields: ticked boxes, selects and text.

    Rows inside a template are skipped, as are buttons, which the caller adds as it clicks one.
    """
    live = re.sub(r"<template.*?</template>", "", html, flags=re.S)
    values: dict[str, str] = {}
    for tag in parse_tags(live):
        name = tag.attrs.get("name")
        if tag.name != "input" or name is None:
            continue
        kind = tag.attrs.get("type", "text")
        if kind in ("submit", "button"):
            continue
        if kind in ("checkbox", "radio") and "checked" not in tag.attrs:
            continue
        values[name] = tag.attrs.get("value", "")
    for found in re.finditer(r'<select[^>]*\bname="([^"]+)"[^>]*>(.*?)</select>', live, re.S):
        options = re.findall(r'<option value="([^"]*)"( selected)?', found.group(2))
        chosen = [value for value, selected in options if selected] or [options[0][0]]
        values[found.group(1)] = chosen[0]
    return values


async def reviewed_run_data(
    client: AsyncClient, confirm_path: str, **fields: str
) -> dict[str, str]:
    """Return the form a run confirmation page submits: its token and the bound review values."""
    from tests.auth_support import csrf_form

    page = await client.get(confirm_path)
    bound = {k: v for k, v in form_values(page.text).items() if k.startswith("expected_")}
    assert bound, f"{confirm_path} offered no run form"
    return csrf_form(client, **bound, **fields)


async def rule_binding(app: FastAPI, rule_id: int) -> dict[str, str]:
    """Return the values a run confirmation binds a run to, read for a rule in any state."""
    from app.db.repositories.automation_rule import AutomationRuleRepository
    from app.db.repositories.scheduled_job import ScheduledJobRepository
    from app.services.automation_rules import rule_digest

    clock = app.state.auth.clock
    async with app.state.database.session() as db:
        rule = await AutomationRuleRepository(db, clock).get(rule_id)
        job = await ScheduledJobRepository(db, clock).get_for_rule(rule_id)
    assert rule is not None and job is not None
    return {"expected_updated_at": rule.updated_at, "expected_digest": rule_digest(rule, job)}
