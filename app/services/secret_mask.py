"""Masking of secret values for display, with an opt-in partial reveal for long values."""

MASK_CHARACTER = "•"
MASK_WIDTH = 16
MASK = MASK_CHARACTER * MASK_WIDTH
EDGE_LENGTH = 4
MIN_EXPOSABLE_LENGTH = 24


def mask(value: str, *, expose: bool) -> str:
    """Return a fixed-width mask, keeping the edges only of a long, plain value when exposed."""
    if (
        expose
        and len(value) >= MIN_EXPOSABLE_LENGTH
        and value.isprintable()
        and value == value.strip()
    ):
        return value[:EDGE_LENGTH] + MASK + value[-EDGE_LENGTH:]
    return MASK
