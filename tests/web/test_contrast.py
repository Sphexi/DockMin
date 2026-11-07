"""Contrast of every token pair the stylesheets draw, computed in each theme."""

import re

import pytest

from tests.web.contrast_support import (
    NON_TEXT,
    TEXT,
    THEMES,
    Pair,
    contrast_ratio,
    luminance,
    palette,
    resolve,
)
from tests.web.helpers import COMPONENT_SHEETS, CSS_DIR, css_rules

SURFACES = ("color-bg", "color-surface", "color-surface-sunken")
TONES = ("ok", "warn", "danger", "info", "local")
TERMINAL_BACKGROUND = "color-surface-sunken"
CONTROL_SELECTOR = re.compile(r"\binput\b|\bselect\b|\btextarea\b|__control|checkbox|radio")


def _tokens(*names: str) -> list[str]:
    return [f"--{name}" for name in names]


def _pairs(
    label: str, minimum: float | None, foregrounds: list[str], backgrounds: list[str]
) -> list[Pair]:
    return [Pair(fg, bg, minimum, label) for fg in foregrounds for bg in backgrounds]


TEXT_ON_SURFACE = _pairs(
    "text-on-surface",
    TEXT,
    _tokens("color-text", "color-text-muted", "color-accent", "color-accent-hover")
    + _tokens(*(f"color-{tone}-fg" for tone in TONES if tone != "local")),
    _tokens(*SURFACES),
)
TEXT_ON_FILL = (
    [
        Pair("--color-on-accent", "--color-accent-solid", TEXT, "button-primary"),
        Pair("--color-on-accent", "--color-accent-solid-hover", TEXT, "button-primary-hover"),
        Pair("--color-nav-text", "--color-nav-bg", TEXT, "sidebar"),
        Pair("--color-nav-text-muted", "--color-nav-bg", TEXT, "sidebar-muted"),
        Pair("--color-nav-text", "--color-nav-hover", TEXT, "sidebar-hover"),
        Pair("--color-nav-active-text", "--color-nav-active-bg", TEXT, "sidebar-current"),
        Pair("--color-text", "--color-topbar-bg", TEXT, "top-bar"),
        Pair("--color-text-subtle", "--color-surface", TEXT, "subtle-text"),
        Pair("--color-neutral-fg", "--color-neutral-bg", TEXT, "badge-neutral-tone"),
        Pair("--color-on-danger-solid", "--color-danger-solid", TEXT, "button-danger"),
        Pair("--color-on-danger-solid", "--color-danger-solid-hover", TEXT, "button-danger-hover"),
        Pair("--color-text-muted", "--color-surface-sunken", TEXT, "badge-neutral"),
        Pair("--color-text", "--color-surface-sunken", TEXT, "button-hover"),
        Pair("--color-text", "--color-info-bg", TEXT, "terminal-selection"),
    ]
    + [Pair(f"--color-{tone}-fg", f"--color-{tone}-bg", TEXT, f"badge-{tone}") for tone in TONES]
    + [Pair("--color-text", f"--color-{tone}-bg", TEXT, f"notice-{tone}") for tone in TONES]
)
TERMINAL_TEXT = [
    Pair(foreground, f"--{TERMINAL_BACKGROUND}", TEXT, "terminal")
    for foreground in _tokens(
        "color-text",
        "color-terminal-black",
        "color-terminal-bright-black",
        "color-terminal-white",
        "color-terminal-bright-white",
        "color-terminal-bright-red",
        "color-danger-fg",
        "color-ok-fg",
        "color-warn-fg",
        "color-info-fg",
        "color-local-fg",
        "color-accent",
        "color-accent-hover",
    )
]
OUTLINES = _pairs(
    "outline",
    NON_TEXT,
    _tokens("color-border-strong", "color-focus"),
    _tokens(*SURFACES),
)
STATUS_FILLS = _pairs(
    "status-fill",
    NON_TEXT,
    _tokens("color-accent", "color-danger-solid")
    + _tokens(*(f"color-{tone}-fg" for tone in TONES)),
    _tokens("color-surface"),
)
CHART_SERIES = _pairs(
    "chart-series",
    NON_TEXT,
    _tokens("color-chart-1", "color-chart-2", "color-chart-3", "color-chart-4"),
    _tokens("color-surface"),
)
DISABLED_BUTTON = [
    Pair("--color-disabled-fg", "--color-disabled-bg", NON_TEXT, "button-disabled"),
]
DECORATIVE = _pairs(
    "decorative",
    None,
    _tokens("color-border", "color-chart-grid", "color-disabled-border"),
    _tokens("color-surface", "color-bg"),
)
ENFORCED = (
    TEXT_ON_SURFACE
    + TEXT_ON_FILL
    + TERMINAL_TEXT
    + OUTLINES
    + STATUS_FILLS
    + CHART_SERIES
    + DISABLED_BUTTON
)


def _ratio(theme: str, pair: Pair) -> float:
    values = palette(theme)
    return contrast_ratio(resolve(values, pair.foreground), resolve(values, pair.background))


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("#000000", "#ffffff", 21.0),
        ("#ffffff", "#000000", 21.0),
        ("#336699", "#336699", 1.0),
        ("#767676", "#ffffff", 4.54),
        ("#777777", "#ffffff", 4.48),
    ],
)
def test_contrast_ratio_matches_published_wcag_values(
    first: str, second: str, expected: float
) -> None:
    assert contrast_ratio(first, second) == pytest.approx(expected, abs=0.01)


def test_luminance_of_a_non_hex_colour_is_rejected() -> None:
    with pytest.raises(ValueError, match="six-digit hex"):
        luminance("rgb(0 0 0)")


@pytest.mark.parametrize("theme", THEMES)
def test_every_declared_token_resolves_to_a_hex_colour_in_each_theme(theme: str) -> None:
    values = palette(theme)
    declared = {t for pair in ENFORCED + DECORATIVE for t in (pair.foreground, pair.background)}

    unresolved = [token for token in sorted(declared) if token not in values]

    assert unresolved == []


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("pair", ENFORCED, ids=lambda pair: pair.pair_id)
def test_token_pair_meets_its_contrast_threshold(theme: str, pair: Pair) -> None:
    assert pair.minimum is not None

    assert _ratio(theme, pair) >= pair.minimum


@pytest.mark.parametrize("pair", DECORATIVE, ids=lambda pair: pair.pair_id)
def test_decorative_border_declares_no_threshold(pair: Pair) -> None:
    assert pair.minimum is None


def test_decorative_border_token_is_never_used_on_a_form_control() -> None:
    offenders = [
        selector
        for sheet in COMPONENT_SHEETS
        for selector, properties in css_rules((CSS_DIR / sheet).read_text(encoding="utf-8"))
        if CONTROL_SELECTOR.search(selector)
        and any("var(--color-border)" in value for value in properties.values())
    ]

    assert offenders == []
