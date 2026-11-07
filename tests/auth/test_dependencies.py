"""Tests for the redirect-target rule in app/auth/dependencies.py."""

import pytest

from app.auth.dependencies import safe_next

HOME = "/"

LOCAL_PATHS = ["/users", "/users/3?tab=sessions", "/ui-kit?limit=5"]
FOREIGN_TARGETS = [
    pytest.param(None, id="absent"),
    pytest.param("", id="empty"),
    pytest.param("https://evil.example/", id="absolute-url"),
    pytest.param("//evil.example/", id="scheme-relative"),
    pytest.param(r"/\evil.example", id="backslash"),
    pytest.param("/ok\r\nSet-Cookie: x=1", id="control-characters"),
    pytest.param("users", id="relative-path"),
]


@pytest.mark.parametrize("target", LOCAL_PATHS)
def test_safe_next_keeps_a_path_on_this_site(target: str) -> None:
    assert safe_next(target) == target


@pytest.mark.parametrize("target", FOREIGN_TARGETS)
def test_safe_next_falls_back_to_the_home_page_for_anything_else(target: str | None) -> None:
    assert safe_next(target) == HOME
