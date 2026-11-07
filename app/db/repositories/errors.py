"""Domain errors raised by repositories in place of raw sqlite3 constraint failures."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


class RepositoryError(Exception):
    """Base for errors a repository raises on purpose."""


class DuplicateError(RepositoryError):
    """Raised when a write would break a uniqueness rule."""


class MissingReferenceError(RepositoryError):
    """Raised when a write names a related row that does not exist."""


class UserHasActivityError(RepositoryError):
    """Raised when deleting a user whose actions are in the activity log; disable the user."""


UNIQUE_PREFIX = "UNIQUE constraint failed"
FOREIGN_KEY_PREFIX = "FOREIGN KEY constraint failed"


@contextmanager
def constraints_as_domain_errors(entity: str) -> Iterator[None]:
    """Translate uniqueness and foreign-key failures in the block into domain errors."""
    try:
        yield
    except sqlite3.IntegrityError as exc:
        message = str(exc)
        # The offending value is left out: these messages reach logs and API error bodies.
        if message.startswith(UNIQUE_PREFIX):
            raise DuplicateError(f"{entity} already exists") from exc
        if message.startswith(FOREIGN_KEY_PREFIX):
            raise MissingReferenceError(f"{entity} refers to a row that does not exist") from exc
        raise
