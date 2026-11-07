"""Tests that the CSRF token reaches the page's header attribute and form fields, and no URL."""

import json

import pytest
from httpx import AsyncClient

from app.web.csrf import CSRF_COOKIE, CSRF_FORM_FIELD, CSRF_HEADER, new_token
from tests.auth_support import cookie_header
from tests.web.helpers import parse_tags, urls_in

MALFORMED_TOKEN = "short"


async def test_token_reaches_the_body_hx_headers_attribute(admin_client: AsyncClient) -> None:
    token = admin_client.cookies[CSRF_COOKIE]
    response = await admin_client.get("/ui-kit")

    body = next(tag for tag in parse_tags(response.text) if tag.name == "body")

    assert json.loads(body.attrs["hx-headers"]) == {CSRF_HEADER: token}


async def test_token_reaches_every_hidden_form_field(admin_client: AsyncClient) -> None:
    token = admin_client.cookies[CSRF_COOKIE]
    response = await admin_client.get("/ui-kit")

    values = [
        tag.attrs["value"]
        for tag in parse_tags(response.text)
        if tag.name == "input" and tag.attrs.get("name") == CSRF_FORM_FIELD
    ]

    assert values
    assert set(values) == {token}


async def test_token_appears_in_no_url_or_query_string(admin_client: AsyncClient) -> None:
    token = admin_client.cookies[CSRF_COOKIE]
    response = await admin_client.get("/ui-kit?limit=5")

    urls = urls_in(response.text)

    assert urls
    assert [url for url in urls if token in url] == []
    assert token not in str(response.request.url)


async def test_a_well_formed_cookie_token_is_reused_and_not_reissued(
    admin_client: AsyncClient,
) -> None:
    token = new_token()

    response = await admin_client.get(
        "/ui-kit", headers=cookie_header(admin_client, **{CSRF_COOKIE: token})
    )

    body = next(tag for tag in parse_tags(response.text) if tag.name == "body")
    assert json.loads(body.attrs["hx-headers"]) == {CSRF_HEADER: token}
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("presented", [MALFORMED_TOKEN, ""])
async def test_a_malformed_cookie_token_is_replaced(
    admin_client: AsyncClient, presented: str
) -> None:
    response = await admin_client.get(
        "/ui-kit", headers=cookie_header(admin_client, **{CSRF_COOKIE: presented})
    )

    issued = response.cookies[CSRF_COOKIE]
    body = next(tag for tag in parse_tags(response.text) if tag.name == "body")
    assert issued != presented
    assert json.loads(body.attrs["hx-headers"]) == {CSRF_HEADER: issued}
