"""The shape shared by every issued token: a mark, an eight-character prefix and a secret."""

import re
import secrets
import string
from typing import Final

PREFIX_LENGTH: Final = 8
SECRET_BYTES: Final = 32
PREFIX_ATTEMPTS: Final = 5
WEBHOOK_TOKEN_MARK: Final = "dkwh"
_SECRET_CHARS: Final = 43
_PREFIX_ALPHABET: Final = string.ascii_letters + string.digits
_SEPARATOR: Final = r"(?:_|%5[Ff])"


def token_pattern(mark: str) -> re.Pattern[str]:
    """Return the pattern of a whole token; group 1 is its prefix.

    The secret may contain "_", so the prefix is found by its fixed width, never by splitting.
    """
    return re.compile(rf"{mark}_([A-Za-z0-9]{{{PREFIX_LENGTH}}})_[A-Za-z0-9_-]{{{_SECRET_CHARS}}}")


def token_remnant_pattern(mark: str) -> re.Pattern[str]:
    """Return the pattern of a token or any truncated or percent-encoded part of one."""
    return re.compile(
        rf"{mark}{_SEPARATOR}(?P<prefix>[A-Za-z0-9]{{{PREFIX_LENGTH}}}){_SEPARATOR}[A-Za-z0-9_%-]+"
    )


def new_token(mark: str) -> tuple[str, str]:
    """Return a token and its prefix; the secret part is 256 random bits."""
    prefix = "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(PREFIX_LENGTH))
    return f"{mark}_{prefix}_{secrets.token_urlsafe(SECRET_BYTES)}", prefix
