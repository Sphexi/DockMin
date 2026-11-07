"""Tests that a page fetched without executing script carries its content and working links."""

from fastapi import FastAPI
from httpx import AsyncClient

from tests.auth_support import csrf_form
from tests.web.flash_support import SEE_OTHER, flash_texts, land, location
from tests.web.helpers import (
    COMPONENT_SHEETS,
    CSS_DIR,
    css_rules,
    fetch_statuses,
    parse_tags,
    same_origin_links,
)
from tests.web.registry_support import create_set, custom_form, visible_text

NOJS_SHEET = "nojs.css"
GROUP_NAME = "edge"
REGISTRY_NAME = "doomed"
RESULT_PREFIX = "/results/"
PAGE_SIZE = 5
PAGED_URL = f"/ui-kit?limit={PAGE_SIZE}"
OK = 200


async def test_fetched_page_contains_its_content_without_script(admin_client: AsyncClient) -> None:
    response = await admin_client.get(PAGED_URL)

    assert "<h1" in response.text
    assert "service-01" in response.text
    assert "<table" in response.text
    assert "<dialog" in response.text
    assert "nojs.css" in response.text


async def test_fetched_page_links_all_resolve(admin_client: AsyncClient) -> None:
    response = await admin_client.get(PAGED_URL)
    links = same_origin_links(response.text)

    statuses = await fetch_statuses(admin_client, links)

    assert "/ui-kit/progress?step=2" in links
    assert set(statuses.values()) == {OK}


async def test_skip_link_targets_an_element_on_the_page(admin_client: AsyncClient) -> None:
    response = await admin_client.get("/")
    tags = parse_tags(response.text)

    skip_targets = [t.attrs["href"] for t in tags if "skip-link" in t.attrs.get("class", "")]
    ids = {f"#{t.attrs['id']}" for t in tags if "id" in t.attrs}

    assert skip_targets == ["#main"]
    assert "#main" in ids


async def test_next_page_link_shows_the_following_rows(admin_client: AsyncClient) -> None:
    first = await admin_client.get(PAGED_URL)
    next_href = next(
        t.attrs["href"] for t in parse_tags(first.text) if t.attrs.get("rel") == "next"
    )

    second = await admin_client.get(next_href)

    assert "service-05" in first.text
    assert "service-06" not in first.text
    assert "service-06" in second.text
    assert "service-05" not in second.text


async def test_progress_panel_offers_a_refresh_link_in_place_of_polling(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/ui-kit/progress")

    assert "Refresh status" in response.text
    assert "/ui-kit/progress?step=2" in same_origin_links(response.text)


def _nojs_rules() -> dict[str, dict[str, str]]:
    rules = css_rules((CSS_DIR / NOJS_SHEET).read_text(encoding="utf-8"))
    return dict(rules)


def _component_rules(selector: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for sheet in COMPONENT_SHEETS:
        if sheet != NOJS_SHEET:
            for found, properties in css_rules((CSS_DIR / sheet).read_text(encoding="utf-8")):
                if found == selector:
                    merged |= properties
    return merged


def test_only_the_dialog_whose_link_was_followed_is_shown() -> None:
    rules = _nojs_rules()
    dialog_rules = {selector: props for selector, props in rules.items() if ".dialog" in selector}

    assert set(dialog_rules) == {".dialog:target"}
    assert dialog_rules[".dialog:target"]["display"] == "block"
    assert dialog_rules[".dialog:target"]["position"] == "static"


def test_script_only_controls_are_removed_and_their_links_stay() -> None:
    rules = _nojs_rules()
    selector = next(s for s in rules if "button[data-dialog-open]" in s)
    hidden = rules[selector]

    assert hidden == {"display": "none"}
    assert "a[data-dialog-open]" not in selector
    assert "[data-toast-close]" in selector


def test_toasts_sit_in_the_page_flow_instead_of_floating_over_it() -> None:
    floating = _component_rules(".toasts")
    in_flow = _nojs_rules()[".toasts"]

    assert floating["position"] == "fixed"
    assert in_flow["position"] == "static"


async def test_a_post_redirects_and_the_notice_shows_on_the_next_page_without_script(
    admin_client: AsyncClient,
) -> None:
    created = await admin_client.post("/groups/new", data=csrf_form(admin_client, name=GROUP_NAME))
    landing = await land(admin_client, created)

    assert created.status_code == SEE_OTHER
    assert landing.status_code == OK
    assert flash_texts(landing.text) == ["Group created."]
    assert 'class="toast"' in landing.text


async def test_a_result_page_carries_its_outcome_and_a_way_back_without_script(
    admin_client: AsyncClient, started_app: FastAPI
) -> None:
    credential_id = await create_set(admin_client, custom_form(admin_client, REGISTRY_NAME))
    deleted = await admin_client.post(
        f"/registries/{credential_id}/delete",
        data=csrf_form(admin_client, expected_name=REGISTRY_NAME, expected_bindings="0"),
    )

    result = await admin_client.get(location(deleted))
    tags = parse_tags(result.text)

    assert location(deleted).startswith(RESULT_PREFIX)
    assert result.status_code == OK
    assert REGISTRY_NAME in visible_text(result.text)
    assert [t.name for t in tags].count("h1") == 1
    assert "/registries" in same_origin_links(result.text)
    assert all("src" in t.attrs for t in tags if t.name == "script")
