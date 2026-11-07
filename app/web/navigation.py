"""Response helpers shared by the form routes: redirects that suit htmx, and cache control."""

from collections.abc import Sequence

from fastapi import Request
from starlette.responses import RedirectResponse, Response

from app.web.flash import Flash, write_flashes
from app.web.templating import is_htmx_request


def redirect(request: Request, location: str, flashes: Sequence[Flash] = ()) -> Response:
    """Answer a completed form post with a redirect, or the htmx equivalent for a fragment swap.

    The notices travel in a cookie and are shown by the next full page.
    """
    response: Response
    if is_htmx_request(request):
        response = Response(status_code=204, headers={"HX-Redirect": location})
    else:
        response = RedirectResponse(location, status_code=303)
    write_flashes(request, response, flashes)
    return response


def no_store(response: Response) -> Response:
    """Forbid caching of a page that carries account or credential state."""
    response.headers["Cache-Control"] = "no-store"
    return response
