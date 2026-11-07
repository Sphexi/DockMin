"""Tests for app/templates/partials/: every macro, discovered from the directory."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import UndefinedError

from app.db.repositories.pagination import CursorPosition, Page, encode_cursor
from app.web.pagination import DEFAULT_LIMIT, Column, PageView
from tests.web.helpers import (
    BENIGN,
    ESCAPED_HOSTILE,
    HOSTILE,
    REQUEST,
    TOKEN,
    MacroSpec,
    macro_arguments,
    partial_macros,
    render_macro,
)
from tests.web.html_tree import Page as Document

MACROS = partial_macros()
MACRO_IDS = [f"{spec.partial}.{spec.name}" for spec in MACROS]
ECHOLESS = frozenset({"navigation_counts.navigation_counts"})
ECHOING = [
    pytest.param(spec, id=name)
    for spec, name in zip(MACROS, MACRO_IDS, strict=True)
    if name not in ECHOLESS
]

LINE_COUNT = 2


def stack_logs_view(text: str) -> dict[str, Any]:
    """Return the Logs tab mapping with text in every free-text field."""
    return {
        "kind": "compose",
        "name": text,
        "choices": [{"name": text, "checked": True}, {"name": f"{text}-2", "checked": False}],
        "members": LINE_COUNT,
        "hidden": 1,
        "over_cap": 1,
        "unreadable": 1,
        "selection": {
            "tail": text,
            "since": text,
            "until": text,
            "q": text,
            "stdout": True,
            "stderr": True,
            "timestamps": True,
        },
        "errors": dict.fromkeys(("tail", "since", "until", "q", "streams"), text),
        "tail_options": [text],
        "budget_options": [LINE_COUNT],
        "since_presets": [text],
        "max_filter": LINE_COUNT,
        "static": True,
        "recent": True,
        "lines": [{"time": text, "origin": text, "text": text, "stderr": True}],
        "total": LINE_COUNT,
        "cut": True,
        "clamped": True,
        "failure": None,
        "page_tail": LINE_COUNT,
        "running": True,
        "stream_href": f"/{text}",
        "download_base": f"/{text}",
        "download_text_href": f"/{text}",
        "download_ndjson_href": f"/{text}",
        "refresh_href": f"/{text}",
        "recent_href": f"/{text}",
        "action_href": f"/{text}",
    }


STRUCTURED_ARGUMENTS: dict[str, Callable[[str], dict[str, Any]]] = {
    "stack_logs.stack_logs": lambda text: {"logs": stack_logs_view(text)},
    "back_actions.back_actions": lambda text: {
        "back_href": f"/{text}",
        "back_label": text,
        "origin": SimpleNamespace(href=f"/{text}", label=text),
    },
}

NOT_A_CURSOR_CONTRACT = [
    pytest.param(None, id="none"),
    pytest.param({}, id="empty-dict"),
    pytest.param({"item_count": 1, "limit": 25}, id="dict-with-fields"),
    pytest.param(
        SimpleNamespace(is_empty=False, is_first_page=True, has_next=False, item_count=1, limit=25),
        id="lookalike-object",
    ),
    pytest.param("PageView", id="string"),
]

INVALID_CHOICES = [
    pytest.param("badge", "badge", {"label": BENIGN, "tone": HOSTILE}, id="badge-tone"),
    pytest.param("toast", "toast", {"level": HOSTILE, "message": BENIGN}, id="toast-level"),
    pytest.param(
        "progress_panel",
        "progress_panel",
        {"panel_id": "p", "title": BENIGN, "state": HOSTILE, "href": "/p"},
        id="progress-state",
    ),
    pytest.param(
        "form_field", "form_field", {"name": "n", "label": BENIGN, "type": HOSTILE}, id="field-type"
    ),
]


def _render(spec: MacroSpec, text: str, *, required_only: bool) -> str:
    build = STRUCTURED_ARGUMENTS.get(f"{spec.partial}.{spec.name}")
    arguments = build(text) if build else macro_arguments(spec, text, required_only=required_only)
    return render_macro(spec.partial, spec.name, **arguments)


def test_partials_directory_yields_macros() -> None:
    assert len(MACROS) > 0


@pytest.mark.parametrize("spec", MACROS, ids=MACRO_IDS)
def test_macro_renders_from_its_required_arguments_alone(spec: MacroSpec) -> None:
    output = _render(spec, BENIGN, required_only=True)

    assert BENIGN in output


@pytest.mark.parametrize("spec", ECHOING)
def test_macro_escapes_hostile_text_in_every_argument(spec: MacroSpec) -> None:
    output = _render(spec, HOSTILE, required_only=False)

    assert ESCAPED_HOSTILE in output
    assert HOSTILE not in output
    assert "<script>" not in output


def test_navigation_counts_never_echoes_its_arguments_and_encodes_the_active_key() -> None:
    environment = macro_arguments(
        next(spec for spec in MACROS if spec.name == "navigation_counts"),
        HOSTILE,
        required_only=True,
    )["environment"]

    output = render_macro(
        "navigation_counts",
        "navigation_counts",
        environment=environment,
        active=HOSTILE,
        pending=True,
    )

    assert HOSTILE not in output and ESCAPED_HOSTILE not in output and "<script>" not in output
    assert "%22%3E%3Cscript%3E" in output
    assert 'hx-trigger="counts-changed from:body, load"' in output


def test_navigation_counts_lists_swarm_links_and_listens_for_changes_only_for_administrators() -> (
    None
):
    environment = macro_arguments(
        next(spec for spec in MACROS if spec.name == "navigation_counts"),
        BENIGN,
        required_only=True,
    )["environment"]
    standard = {
        "csrf_token": TOKEN,
        "request": REQUEST,
        "principal": SimpleNamespace(is_administrator=False),
    }

    as_admin = render_macro(
        "navigation_counts", "navigation_counts", environment=environment, active="nodes"
    )
    as_standard = render_macro(
        "navigation_counts",
        "navigation_counts",
        context=standard,
        environment=environment,
        active="services",
    )

    assert 'hx-trigger="counts-changed from:body"' in as_admin
    assert 'aria-current="page"' in as_admin and "/swarm/nodes" in as_admin
    assert "hx-get" not in as_standard and "/swarm/nodes" not in as_standard
    assert "/swarm/services" in as_standard


@pytest.mark.parametrize(("partial", "name", "arguments"), INVALID_CHOICES)
def test_macro_rejects_a_value_outside_its_allowed_choices(
    partial: str, name: str, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="is not one of"):
        render_macro(partial, name, **arguments)


@pytest.mark.parametrize("pager", NOT_A_CURSOR_CONTRACT)
def test_pagination_without_the_cursor_contract_raises(pager: object) -> None:
    with pytest.raises(TypeError, match="PageView"):
        render_macro("pagination", "pagination", pager=pager, table_id="t")


@pytest.mark.parametrize("pager", NOT_A_CURSOR_CONTRACT)
def test_data_table_without_the_cursor_contract_raises(pager: object) -> None:
    with pytest.raises(TypeError, match="PageView"):
        render_macro(
            "data_table",
            "data_table",
            table_id="t",
            caption=BENIGN,
            columns=[],
            pager=pager,
            caller=lambda: "",
        )


def test_confirmation_dialog_carries_the_csrf_token_in_a_hidden_field() -> None:
    output = render_macro(
        "confirmation_dialog",
        "confirmation_dialog",
        dialog_id="d",
        title=BENIGN,
        message=BENIGN,
        action="/remove",
        confirm_label="Remove",
    )

    assert f'<input type="hidden" name="csrf_token" value="{TOKEN}">' in output


def test_confirmation_dialog_without_a_csrf_token_in_scope_fails_the_render() -> None:
    with pytest.raises(UndefinedError):
        render_macro(
            "confirmation_dialog",
            "confirmation_dialog",
            context={},
            dialog_id="d",
            title=BENIGN,
            message=BENIGN,
            action="/remove",
            confirm_label="Remove",
        )


EMPTY_TITLE = "No widgets yet"
EMPTY_BODY = "Add a widget to start."
ACTION_LABEL = "Add a widget"
ACTION_HREF = "/widgets/new"
CLEAR_HREF = "/widgets?clear=1"
BASE_PATH = "/widgets"


def _empty_pager(*, first_page: bool = True) -> PageView:
    cursor = None if first_page else encode_cursor(CursorPosition("a", 1))
    return PageView.of(
        Page(items=[], next_cursor=None), base_path=BASE_PATH, limit=DEFAULT_LIMIT, cursor=cursor
    )


def _empty_table(pager: PageView | None = None, **arguments: Any) -> Document:
    html = render_macro(
        "data_table",
        "data_table",
        table_id="t",
        caption="Widgets",
        columns=[],
        pager=pager or _empty_pager(),
        caller=lambda: "",
        **arguments,
    )
    return Document(200, html)


def _links(page: Document) -> dict[str, str]:
    return {n.text(): n.attrs["href"] for n in page.tags("a")}


def test_empty_table_shows_its_title_its_next_step_and_a_link_to_take_it() -> None:
    page = _empty_table(
        empty_title=EMPTY_TITLE,
        empty_body=EMPTY_BODY,
        empty_action_label=ACTION_LABEL,
        empty_action_href=ACTION_HREF,
    )

    assert [n.text() for n in page.tags("h2")] == [EMPTY_TITLE]
    assert EMPTY_BODY in page.root.text()
    assert _links(page) == {ACTION_LABEL: ACTION_HREF}
    assert not list(page.tags("table"))


def test_empty_table_without_an_action_offers_no_link() -> None:
    page = _empty_table(empty_title=EMPTY_TITLE, empty_action_label=ACTION_LABEL)

    assert _links(page) == {}


@pytest.mark.parametrize(("clear_href", "expected"), [(CLEAR_HREF, CLEAR_HREF), (None, BASE_PATH)])
def test_filtered_empty_table_says_nothing_matches_and_offers_to_clear_the_filters(
    clear_href: str | None, expected: str
) -> None:
    page = _empty_table(
        empty_title=EMPTY_TITLE,
        empty_action_label=ACTION_LABEL,
        empty_action_href=ACTION_HREF,
        filtered=True,
        clear_href=clear_href,
    )

    assert [n.text() for n in page.tags("h2")] == ["Nothing matches these filters"]
    assert _links(page) == {"Clear filters": expected}


def test_empty_table_past_the_first_page_links_back_to_the_first() -> None:
    page = _empty_table(_empty_pager(first_page=False), empty_title=EMPTY_TITLE)

    assert _links(page) == {"Back to the first page": f"{BASE_PATH}?limit={DEFAULT_LIMIT}"}


def test_every_table_announces_that_it_is_loading_for_the_busy_state() -> None:
    page = _empty_table(empty_title=EMPTY_TITLE)

    loading = [n for n in page.tags("p") if "loading" in n.classes()]
    assert [n.attrs["role"] for n in loading] == ["status"]


ENGINE_TEXT = "network x is ambiguous <b>&"


def test_error_state_without_engine_text_shows_no_docker_block() -> None:
    html = render_macro("error_state", "error_state", title="Failed", message="Try again.")

    assert "Docker said" not in html


def test_error_state_with_engine_text_shows_it_escaped_in_a_labelled_scrollable_region() -> None:
    html = render_macro(
        "error_state",
        "error_state",
        title="Failed",
        message="Try again.",
        engine_detail=ENGINE_TEXT,
    )
    page = Document(200, html)
    block = next(n for n in page.tags("pre"))

    assert "Docker said" in page.root.text()
    assert block.text() == ENGINE_TEXT
    assert "<b>" not in html
    assert block.attrs["tabindex"] == "0"
    assert block.attrs["role"] == "region"
    assert block.attrs["aria-label"]


def test_error_state_is_an_alert_with_a_reference_and_a_retry_link() -> None:
    html = render_macro(
        "error_state",
        "error_state",
        title="Failed",
        message="Try again.",
        request_id="abc123",
        retry_href="/retry",
    )
    page = Document(200, html)
    retry = next(page.tags("a"))

    assert next(page.tags("div")).attrs["role"] == "alert"
    assert "abc123" in page.root.text()
    assert retry.attrs["href"] == "/retry"
    assert "hx-get" not in retry.attrs


@pytest.mark.parametrize(
    ("swap", "root_id"), [("outerHTML", "region"), ("innerHTML", None)], ids=["outer", "inner"]
)
def test_error_state_with_a_region_retries_into_it_and_keeps_the_id_only_when_it_replaces_it(
    swap: str, root_id: str | None
) -> None:
    html = render_macro(
        "error_state",
        "error_state",
        title="Failed",
        message="Try again.",
        retry_href="/retry",
        region_id="region",
        swap=swap,
    )
    page = Document(200, html)
    retry = next(page.tags("a"))

    assert next(page.tags("div")).attrs.get("id") == root_id
    assert (retry.attrs["href"], retry.attrs["hx-get"]) == ("/retry", "/retry")
    assert (retry.attrs["hx-target"], retry.attrs["hx-swap"]) == ("#region", swap)


def test_error_state_refuses_a_swap_outside_its_allowlist() -> None:
    with pytest.raises(ValueError, match="outerHTML"):
        render_macro(
            "error_state",
            "error_state",
            title="Failed",
            message="x",
            retry_href="/retry",
            region_id="region",
            swap="beforeend",
        )


def test_check_status_is_a_hidden_live_region_that_replaces_its_content_out_of_band() -> None:
    html = render_macro("check_status", "check_status", status_id="s", text="Checked")
    region = next(Document(200, html).tags("p"))

    assert region.attrs["id"] == "s"
    assert region.attrs["role"] == "status"
    assert region.attrs["hx-swap-oob"] == "innerHTML"
    assert "visually-hidden" in region.classes()
    assert region.text() == "Checked"


def test_optional_columns_are_marked_for_the_narrow_layout_and_the_name_column_never_is() -> None:
    pager = PageView.of(Page(items=["row"], next_cursor=None), base_path=BASE_PATH, limit=25)
    html = render_macro(
        "data_table",
        "data_table",
        table_id="t",
        caption="Widgets",
        columns=[Column("name", "Name", sortable=True), Column("size", "Size", optional=True)],
        pager=pager,
        caller=lambda: "<tr><td>row</td><td>1</td></tr>",
    )
    page = Document(200, html)
    headers = {n.text(): n for n in page.tags("th")}
    table = next(page.tags("table"))

    assert "col-optional" not in headers["Name"].classes()
    assert "col-optional" in headers["Size"].classes()
    assert table.attrs["data-optional"] == "2"
    assert all(n.attrs["scope"] == "col" for n in headers.values())
