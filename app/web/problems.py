"""The title and message each error status shows on a page, a fragment and a toast."""

from http import HTTPStatus
from typing import Final, NamedTuple

from app.services.object_inspector import EnvironmentUnavailable


class ErrorCopy(NamedTuple):
    """What an error says and what to do about it."""

    title: str
    message: str


_COPY: Final[dict[int, ErrorCopy]] = {
    400: ErrorCopy("The request could not be read", "Reload the page and try again."),
    403: ErrorCopy("Your role does not allow this", "Ask an administrator if you need it."),
    404: ErrorCopy(
        "Not found",
        "It does not exist, or you do not have access to it. "
        "Check the address, or go back to the list.",
    ),
    405: ErrorCopy("Not available here", "Use the page's own controls."),
    408: ErrorCopy("The request took too long", "Try again."),
    409: ErrorCopy(
        "Something changed", "Reload the page to see the current state, then try again."
    ),
    413: ErrorCopy("Too large", "Send a smaller file or form."),
    422: ErrorCopy("Check the form", "Correct the fields shown, then submit again."),
    429: ErrorCopy("Too many requests", "Wait a few seconds, then try again."),
    500: ErrorCopy(
        "Something went wrong",
        "Try again. If it keeps failing, give an administrator the reference below.",
    ),
    502: ErrorCopy(
        "Docker answered with an error",
        "Try again. If it keeps failing, check the Docker host.",
    ),
    503: ErrorCopy("Not available right now", "Try again shortly."),
}


def error_copy(status: int) -> ErrorCopy:
    """Return the copy for a status; an unlisted one keeps its phrase over the 500 message."""
    known = _COPY.get(status)
    if known is not None:
        return known
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = _COPY[500].title
    return ErrorCopy(phrase, _COPY[500].message)


UNREACHABLE_TITLE: Final = "The environment is not reachable"
UNREACHABLE_NEXT: Final = "Check that the Docker host is running and reachable, then try again."


def unreachable_copy(error: Exception) -> ErrorCopy:
    """Return the title and message for a host that cannot be reached or read."""
    if isinstance(error, EnvironmentUnavailable):
        return ErrorCopy(UNREACHABLE_TITLE, f"{error} {UNREACHABLE_NEXT}")
    return ErrorCopy(_COPY[503].title, str(error))
