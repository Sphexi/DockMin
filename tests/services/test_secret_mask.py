"""Tests for app/services/secret_mask.py."""

import pytest

from app.services.secret_mask import (
    EDGE_LENGTH,
    MASK,
    MASK_CHARACTER,
    MASK_WIDTH,
    MIN_EXPOSABLE_LENGTH,
    mask,
)

FIRST_EDGE = "ABCD"
LAST_EDGE = "WXYZ"
EXPOSED = FIRST_EDGE + MASK + LAST_EDGE


def _value(length: int) -> str:
    return FIRST_EDGE + "m" * (length - EDGE_LENGTH * 2) + LAST_EDGE


MINIMUM = _value(MIN_EXPOSABLE_LENGTH)
MIDDLE = EDGE_LENGTH + 2


def test_the_mask_is_sixteen_bullets() -> None:
    assert MASK == "•" * 16
    assert (MASK_CHARACTER, MASK_WIDTH) == ("•", 16)


@pytest.mark.parametrize(
    ("value", "expose", "shown"),
    [
        pytest.param(MINIMUM, True, EXPOSED, id="the-minimum-length-exposes-the-edges"),
        pytest.param(_value(200), True, EXPOSED, id="a-long-value-still-exposes-only-the-edges"),
        pytest.param(
            MINIMUM[:MIDDLE] + " " + MINIMUM[MIDDLE + 1 :], True, EXPOSED, id="an-inner-space"
        ),
        pytest.param(_value(MIN_EXPOSABLE_LENGTH - 1), True, MASK, id="one-short-of-the-minimum"),
        pytest.param("", True, MASK, id="empty"),
        pytest.param(MINIMUM[:MIDDLE] + "\t" + MINIMUM[MIDDLE + 1 :], True, MASK, id="tab"),
        pytest.param(MINIMUM[:MIDDLE] + "\n" + MINIMUM[MIDDLE + 1 :], True, MASK, id="newline"),
        pytest.param(" " + MINIMUM[1:], True, MASK, id="leading-space"),
        pytest.param(MINIMUM[:-1] + " ", True, MASK, id="trailing-space"),
        pytest.param(MINIMUM, False, MASK, id="not-asked-to-expose"),
        pytest.param(_value(200), False, MASK, id="long-and-not-asked-to-expose"),
    ],
)
def test_the_edges_show_only_for_a_long_plain_value_when_asked(
    value: str, expose: bool, shown: str
) -> None:
    assert mask(value, expose=expose) == shown


def test_an_exposed_value_never_carries_more_than_the_four_characters_at_each_end() -> None:
    shown = mask(_value(200), expose=True)

    assert shown.count("m") == 0
    assert len(shown) == EDGE_LENGTH * 2 + MASK_WIDTH
