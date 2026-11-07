"""The user's theme choice, read from the cookie the toggle writes."""

from fastapi import Request

THEME_COOKIE = "dockmin_theme"
THEMES = ("light", "dark")


def requested_theme(request: Request) -> str | None:
    """Return "light" or "dark" when the cookie holds one, otherwise None for system default."""
    value = request.cookies.get(THEME_COOKIE)
    # Allowlisted because the value is written into the opening <html> tag.
    return value if value in THEMES else None
