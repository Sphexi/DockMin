"""Tests for the server-side theme decision written into the opening <html> tag."""

import pytest
from httpx import AsyncClient

from app.web.theme import THEME_COOKIE
from tests.auth_support import cookie_header
from tests.web.helpers import parse_tags

UNSET_TAG = '<html lang="en">'
HOSTILE_COOKIES = [
    pytest.param('"><script>alert(1)</script>', id="script-injection"),
    pytest.param('dark" onload="alert(1)', id="attribute-injection"),
    pytest.param("Dark", id="wrong-case"),
    pytest.param("system", id="unlisted-theme"),
    pytest.param("", id="empty"),
]


def _cookie(admin_client: AsyncClient, value: str) -> dict[str, str]:
    return cookie_header(admin_client, **{THEME_COOKIE: value})


async def test_page_without_a_theme_cookie_leaves_the_theme_to_the_system(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/")

    root = next(tag for tag in parse_tags(response.text) if tag.name == "html")
    assert UNSET_TAG in response.text
    assert "data-theme" not in root.attrs


@pytest.mark.parametrize("theme", ["light", "dark"])
async def test_page_with_a_theme_cookie_renders_that_theme_in_the_opening_tag(
    admin_client: AsyncClient, theme: str
) -> None:
    response = await admin_client.get("/", headers=_cookie(admin_client, theme))

    assert f'<html lang="en" data-theme="{theme}">' in response.text


@pytest.mark.parametrize("value", HOSTILE_COOKIES)
async def test_page_with_a_hostile_theme_cookie_ignores_it(
    admin_client: AsyncClient, value: str
) -> None:
    response = await admin_client.get("/", headers=_cookie(admin_client, value))

    assert UNSET_TAG in response.text
    assert "alert(1)" not in response.text
    assert "onload" not in response.text


async def test_every_script_is_external_and_deferred_so_none_decides_first_paint(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/", headers=_cookie(admin_client, "dark"))

    scripts = [tag for tag in parse_tags(response.text) if tag.name == "script"]
    assert scripts
    assert all("src" in script.attrs and "defer" in script.attrs for script in scripts)


async def test_theme_toggle_is_named_and_stays_hidden_until_script_reveals_it(
    admin_client: AsyncClient,
) -> None:
    response = await admin_client.get("/")

    toggle = next(tag for tag in parse_tags(response.text) if "data-theme-toggle" in tag.attrs)
    assert toggle.name == "button"
    assert toggle.attrs["type"] == "button"
    assert "hidden" in toggle.attrs
    assert toggle.attrs["aria-pressed"] == "false"
    assert "Dark theme" in response.text
