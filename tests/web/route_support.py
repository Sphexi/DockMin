"""Walking the application's route table, nested routers included."""

import re

from fastapi import FastAPI
from fastapi.routing import iter_route_contexts

PATH_PARAMETER = re.compile(r"\{(\w+)\}")
STREAM_MARKERS = ("/stream", "/events")


def get_route_paths(app: FastAPI) -> list[str]:
    """Return every GET path, however deeply its router is nested; mounts are not included."""
    return sorted(
        {
            context.path
            for context in iter_route_contexts(app.router.routes)
            if context.path is not None and "GET" in (context.methods or ())
        }
    )


def fill(path: str, **values: object) -> str:
    """Replace each path parameter with the value given for it, else 1."""
    return PATH_PARAMETER.sub(lambda found: str(values.get(found.group(1), 1)), path)
