"""Tests that no template carries inline code or opts out of autoescaping."""

import re
from pathlib import Path

import pytest
from httpx import AsyncClient

from tests.web.helpers import (
    inline_code_in,
    relative_name,
    strip_jinja,
    strip_jinja_comments,
    template_files,
)

TEMPLATES = template_files()
TEMPLATE_IDS = [relative_name(path) for path in TEMPLATES]
DATE_FORMATTING = re.compile(r"strftime|isoformat|\.date\(\)|\.timestamp\(\)")
TIMESTAMP_FILTER = re.compile(r"\|\s*timestamp\b(?!_)")
UNSAFE_PATTERNS = (
    re.compile(r"\|\s*safe\b"),
    re.compile(r"\bMarkup\b"),
    re.compile(r"\{%-?\s*autoescape\s+(?:false|off)\b"),
)


def _unsafe_in(source: str) -> bool:
    return any(pattern.search(source) for pattern in UNSAFE_PATTERNS)


def _timestamps_outside_time(source: str) -> list[int]:
    outside = []
    for found in TIMESTAMP_FILTER.finditer(source):
        opened = source.rfind("<time", 0, found.start())
        closed = source.rfind("</time>", 0, found.start())
        if opened == -1 or closed > opened:
            outside.append(found.start())
    return outside


@pytest.mark.parametrize(
    "source",
    [
        "{{ row.at|timestamp }}",
        '<time datetime="{{ a|timestamp_iso }}">x</time>{{ b|timestamp }}',
        "<td>{{ b | timestamp(seconds=true) }}</td>",
    ],
)
def test_timestamp_scanner_detects_a_filter_outside_a_time_element(source: str) -> None:
    assert _timestamps_outside_time(source)


@pytest.mark.parametrize(
    "source",
    [
        '<time datetime="{{ a|timestamp_iso }}">{{ a|timestamp }}</time>',
        '<time datetime="{{ a|timestamp_iso }}">{{ a|timestamp(seconds=true) }}</time>',
        "{{ a|timestamp_iso }}",
        "<p>{{ a|relative_age }}</p>",
    ],
)
def test_timestamp_scanner_accepts_the_filter_inside_a_time_element(source: str) -> None:
    assert _timestamps_outside_time(source) == []


@pytest.mark.parametrize("path", TEMPLATES, ids=TEMPLATE_IDS)
def test_template_formats_times_only_through_the_timestamp_filter_inside_time(path: Path) -> None:
    source = strip_jinja_comments(path.read_text(encoding="utf-8"))

    assert DATE_FORMATTING.findall(source) == []
    assert _timestamps_outside_time(source) == []


@pytest.mark.parametrize(
    "markup",
    [
        "<script>go()</script>",
        '<a style="x:y">',
        "<style>a{}</style>",
        '<button onclick="go()">',
        '<div hx-on:click="go()">',
        '<div hx-on::after-swap="go()">',
        '<a href="javascript:go()">',
    ],
)
def test_inline_code_scanner_detects_inline_code(markup: str) -> None:
    assert inline_code_in(markup)


@pytest.mark.parametrize(
    "markup",
    [
        '<script src="/a.js" defer></script>',
        '<a href="/x" class="btn" aria-label="Online">',
        '<div hx-get="/x" hx-target="#t" hx-swap="outerHTML">',
        '<input name="one" type="text">',
    ],
)
def test_inline_code_scanner_accepts_external_script_and_declarative_attributes(
    markup: str,
) -> None:
    assert inline_code_in(markup) == []


@pytest.mark.parametrize(
    "source",
    ["{{ value|safe }}", "{{ value | safe }}", "{{ Markup(value) }}", "{% autoescape false %}"],
)
def test_unsafe_scanner_detects_safe_markup_and_autoescape_off(source: str) -> None:
    assert _unsafe_in(source)


@pytest.mark.parametrize(
    "source", ["{{ value|forceescape }}", "{{ value|tojson }}", "{{ safely }}"]
)
def test_unsafe_scanner_accepts_escaping_filters(source: str) -> None:
    assert not _unsafe_in(source)


def test_template_directory_yields_templates() -> None:
    assert len(TEMPLATES) > 0


@pytest.mark.parametrize("path", TEMPLATES, ids=TEMPLATE_IDS)
def test_template_source_has_no_inline_script_style_or_event_handler(path: Path) -> None:
    source = path.read_text(encoding="utf-8")

    assert inline_code_in(strip_jinja(source)) == []


@pytest.mark.parametrize("path", TEMPLATES, ids=TEMPLATE_IDS)
def test_template_source_never_marks_output_safe(path: Path) -> None:
    source = path.read_text(encoding="utf-8")

    assert not _unsafe_in(strip_jinja_comments(source))


@pytest.mark.parametrize("target", ["/ui-kit", "/ui-kit/progress", "/missing"])
async def test_rendered_page_has_no_inline_script_style_or_event_handler(
    admin_client: AsyncClient, target: str
) -> None:
    response = await admin_client.get(target)

    assert inline_code_in(response.text) == []
