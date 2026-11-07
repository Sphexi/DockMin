"""The `from` query parameter on links that carry the page they start from."""

from typing import Final
from urllib.parse import urlencode

from app.services.origins import parse_origin

ORIGIN_PARAM: Final = "from"


def with_origin(href: str, ref: str | None) -> str:
    """Append the origin reference to an internal path; anything else is returned unchanged."""
    if not ref or parse_origin(ref) is None:
        return href
    if not href.startswith("/") or href.startswith("//") or "\\" in href:
        return href
    path, mark, fragment = href.partition("#")
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urlencode({ORIGIN_PARAM: ref})}{mark}{fragment}"
