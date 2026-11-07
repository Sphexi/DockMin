"""WCAG 2.2 contrast ratios computed from the palette blocks of tokens.css."""

import re
from dataclasses import dataclass

from tests.web.helpers import CSS_DIR, css_rules

LIGHT_BLOCK = ":root"
DARK_BLOCK = '[data-theme="dark"]'
SYSTEM_DARK_BLOCK = ":root:not([data-theme])"
THEMES = ("light", "dark", "system-dark")

TEXT = 4.5
NON_TEXT = 3.0

_HEX = re.compile(r"#([0-9a-fA-F]{6})\b")
_REFERENCE = re.compile(r"var\(\s*(--[\w-]+)\s*\)")
_LINEAR_THRESHOLD = 0.03928


@dataclass(frozen=True)
class Pair:
    """A foreground token drawn on a background token, and the ratio it must reach."""

    foreground: str
    background: str
    minimum: float | None
    label: str

    @property
    def pair_id(self) -> str:
        return f"{self.label}:{self.foreground}-on-{self.background}"


def _merged(selector: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for found, properties in css_rules((CSS_DIR / "tokens.css").read_text(encoding="utf-8")):
        if found == selector:
            merged |= properties
    return merged


def palette(theme: str) -> dict[str, str]:
    """Return every custom property in effect for a theme, dark blocks laid over the light one."""
    palette_ = _merged(LIGHT_BLOCK)
    if theme == "dark":
        palette_ |= _merged(DARK_BLOCK)
    elif theme == "system-dark":
        palette_ |= _merged(SYSTEM_DARK_BLOCK)
    return palette_


def resolve(values: dict[str, str], token: str) -> str:
    """Follow var() references until a literal remains."""
    value = values[token]
    reference = _REFERENCE.fullmatch(value)
    return resolve(values, reference.group(1)) if reference else value


def _channel(byte: int) -> float:
    unit = byte / 255
    return unit / 12.92 if unit <= _LINEAR_THRESHOLD else ((unit + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    found = _HEX.fullmatch(colour)
    if found is None:
        raise ValueError(f"not a six-digit hex colour: {colour!r}")
    digits = found.group(1)
    red, green, blue = (_channel(int(digits[i : i + 2], 16)) for i in (0, 2, 4))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: str, second: str) -> float:
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
