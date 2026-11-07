"""Tests that one URL serves a full page or a fragment depending on the HX-Request header."""

import pytest
from httpx import AsyncClient

from tests.auth_support import ADMIN_SESSION_TOKEN, Browsers

pytestmark = [
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.xdist_group("test_htmx_routing"),
    pytest.mark.usefixtures("module_app_guard"),
]

HTMX = {"HX-Request": "true"}
BOOSTED = {"HX-Request": "true", "HX-Boosted": "true"}
FULL_PAGE_MARKER = "<!doctype html>"
ERROR_FRAGMENT_MARKER = 'class="state state--error"'

SUCCESS_URLS = [
    pytest.param("/ui-kit", "kit-table", id="table"),
    pytest.param("/ui-kit/progress", "kit-progress", id="progress"),
]
ERROR_URLS = [
    pytest.param("/missing", 404, id="not-found"),
    pytest.param("/ui-kit?cursor=!!", 400, id="invalid-cursor"),
]


@pytest.fixture
def admin_client(module_browsers: Browsers) -> AsyncClient:
    return module_browsers.open(session_token=ADMIN_SESSION_TOKEN)


@pytest.mark.parametrize(("url", "element_id"), SUCCESS_URLS)
async def test_url_without_the_header_returns_a_full_page(
    admin_client: AsyncClient, url: str, element_id: str
) -> None:
    response = await admin_client.get(url)

    assert FULL_PAGE_MARKER in response.text
    assert f'id="{element_id}"' in response.text


@pytest.mark.parametrize(("url", "element_id"), SUCCESS_URLS)
async def test_url_with_the_header_returns_only_the_fragment(
    admin_client: AsyncClient, url: str, element_id: str
) -> None:
    response = await admin_client.get(url, headers=HTMX)

    assert f'id="{element_id}"' in response.text
    assert FULL_PAGE_MARKER not in response.text
    assert "<body" not in response.text


async def test_boosted_navigation_returns_a_full_page(admin_client: AsyncClient) -> None:
    response = await admin_client.get("/ui-kit", headers=BOOSTED)

    assert FULL_PAGE_MARKER in response.text


@pytest.mark.parametrize(("url", "status"), ERROR_URLS)
async def test_error_without_the_header_returns_a_full_error_page(
    admin_client: AsyncClient, url: str, status: int
) -> None:
    response = await admin_client.get(url)

    assert response.status_code == status
    assert FULL_PAGE_MARKER in response.text


@pytest.mark.parametrize(("url", "status"), ERROR_URLS)
async def test_error_with_the_header_returns_an_error_fragment_with_the_same_status(
    admin_client: AsyncClient, url: str, status: int
) -> None:
    response = await admin_client.get(url, headers=HTMX)

    assert response.status_code == status
    assert ERROR_FRAGMENT_MARKER in response.text
    assert FULL_PAGE_MARKER not in response.text


@pytest.mark.parametrize("url", ["/ui-kit", "/missing"])
async def test_response_varies_on_the_header_that_selects_the_form(
    admin_client: AsyncClient, url: str
) -> None:
    response = await admin_client.get(url)

    assert "HX-Request" in response.headers["vary"]
