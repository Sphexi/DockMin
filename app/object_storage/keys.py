"""Object key and prefix validation."""

import re
from typing import Final

from app.object_storage.errors import StorageFailure

MAX_KEY_BYTES: Final = 1024
_KEY: Final = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", re.ASCII)


def validate_key(key: str) -> str:
    """Return key unchanged, or raise StorageFailure('key_invalid').

    The alphabet and segment rules keep the signer's dot-segment removal and percent-encoding
    from changing what is sent.
    """
    if (
        len(key) > MAX_KEY_BYTES
        or _KEY.fullmatch(key) is None
        or any(segment in (".", "..") for segment in key.split("/"))
    ):
        raise StorageFailure("key_invalid")
    return key


def validate_prefix(prefix: str) -> str:
    """Return a configured prefix without its one trailing slash; '' means the bucket root.

    Raises StorageFailure('key_invalid') when the rest is not a valid key.
    """
    if not prefix:
        return ""
    return validate_key(prefix.removesuffix("/"))


def validate_listing_prefix(prefix: str) -> str:
    """Return a listing prefix unchanged: empty, or a key with at most one trailing slash."""
    if prefix:
        validate_key(prefix.removesuffix("/"))
    return prefix
