"""Tests for app/web/pages.py."""

from httpx import AsyncClient


async def test_index_returns_an_html_page(admin_client: AsyncClient) -> None:
    response = await admin_client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
