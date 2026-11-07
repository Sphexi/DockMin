"""Paths a browser requests unprompted answer without a session and before setup completes."""

from httpx import AsyncClient

from tests.auth_support import Browsers

FAVICON_PATH = "/favicon.ico"
FAVICON_TARGET = "/static/icons/favicon.svg"
PERMANENT_REDIRECT = 308


async def test_the_favicon_redirects_to_the_svg_icon_before_setup_has_completed(
    fresh_browsers: Browsers,
) -> None:
    client = fresh_browsers.open()

    response = await client.get(FAVICON_PATH)

    assert (response.status_code, response.headers["location"]) == (
        PERMANENT_REDIRECT,
        FAVICON_TARGET,
    )


async def test_the_favicon_redirects_to_the_svg_icon_for_a_visitor_with_no_session(
    anonymous_client: AsyncClient,
) -> None:
    response = await anonymous_client.get(FAVICON_PATH)

    assert (response.status_code, response.headers["location"]) == (
        PERMANENT_REDIRECT,
        FAVICON_TARGET,
    )
