"""Helpers for comparing error pages that differ only by the reference they print."""

import re

REFERENCE = re.compile(r'Reference <span class="mono">[^<]*</span>')


def without_reference(html: str) -> str:
    """Return the page with the request reference blanked, so two refusals compare equal."""
    return REFERENCE.sub("Reference", html)
