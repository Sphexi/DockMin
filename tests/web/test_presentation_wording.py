"""The Automation, Jobs and activity detail pages say automation where a rule is meant."""

import re

import pytest

from tests.web.html_tree import Page
from tests.web.page_world import (  # noqa: F401
    PAGES,
    ROLES,
    Key,
    world_pages,
    world_webhook_token_pages,
)

pytestmark = pytest.mark.xdist_group("page-world")

OK = 200
RULE_WORD = re.compile(r"\brules?\b", re.IGNORECASE)
WORDED_PREFIXES = ("automation-", "job-")
WORDED_LABELS = ("activity-detail",)
NAMING_ATTRIBUTES = ("aria-label", "title", "placeholder", "alt")
BUTTON_INPUTS = ("submit", "button")
OWNER_COLUMN = "Owner"
NAME_COLUMN = "Name"
LIST_PAGE = "automation-overview"
WEBHOOK_PREFIX = "automation-webhook-"


def worded_labels() -> list[str]:
    return [label for label in PAGES if label.startswith(WORDED_PREFIXES) or label in WORDED_LABELS]


def webhook_labels() -> list[str]:
    return [label for label in worded_labels() if label.startswith(WEBHOOK_PREFIX)]


def visible_strings(page: Page) -> list[str]:
    strings: list[str] = []
    for node in page.nodes:
        strings += [child for child in node.children if isinstance(child, str)]
        strings += [node.attrs[name] for name in NAMING_ATTRIBUTES if name in node.attrs]
        if node.tag == "input" and node.attrs.get("type") in BUTTON_INPUTS:
            strings.append(node.attrs.get("value", ""))
    return strings


def column_headings(page: Page) -> list[str]:
    table = next(page.tags("table"))
    return [heading.text() for heading in table.walk() if heading.tag == "th"]


@pytest.mark.parametrize("role", ROLES)
def test_no_visible_text_on_the_automation_job_and_event_detail_pages_says_rule(
    pages: dict[Key, Page], role: str
) -> None:
    found = [
        f"{label}: {text.strip()}"
        for label in worded_labels()
        for text in visible_strings(pages[(role, label)])
        if RULE_WORD.search(text)
    ]

    assert found == []


def test_the_wording_check_covers_the_automation_job_and_event_detail_pages() -> None:
    labels = worded_labels()

    assert {"automation-overview", "automation-rule-detail", "job-list", "job-run-detail"} <= set(
        labels
    )
    assert "activity-detail" in labels


def test_the_administrator_reaches_every_page_whose_wording_is_checked(
    pages: dict[Key, Page],
) -> None:
    statuses = {label: pages[("admin", label)].status for label in worded_labels()}

    assert set(statuses.values()) == {OK}


def test_the_automation_list_opens_with_name_then_owner_for_an_administrator(
    pages: dict[Key, Page],
) -> None:
    headings = column_headings(pages[("admin", LIST_PAGE)])

    assert headings[:2] == [NAME_COLUMN, OWNER_COLUMN]


def test_the_wording_check_covers_every_webhook_page() -> None:
    labels = set(webhook_labels())

    assert {
        "automation-webhook-new",
        "automation-webhook-detail",
        "automation-webhook-rotate-confirm",
        "automation-webhook-revoke-confirm",
    } <= labels


@pytest.mark.parametrize("role", ROLES)
def test_each_role_reaches_every_webhook_page_of_its_own_automation(
    pages: dict[Key, Page], webhook_token_pages: dict[str, Page], role: str
) -> None:
    statuses = {label: pages[(role, label)].status for label in webhook_labels()}

    assert set(statuses.values()) == {OK}
    assert webhook_token_pages[role].status == OK


@pytest.mark.parametrize("role", ROLES)
def test_no_visible_text_on_the_page_that_shows_a_webhook_url_says_rule(
    webhook_token_pages: dict[str, Page], role: str
) -> None:
    found = [
        text.strip()
        for text in visible_strings(webhook_token_pages[role])
        if RULE_WORD.search(text)
    ]

    assert found == []
