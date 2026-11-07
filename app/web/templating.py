"""Jinja2 environment, per-request template context, and the response helpers HTML routes share."""

import json
import re
from collections.abc import Callable, Collection, Mapping
from importlib import metadata
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined, pass_context
from jinja2.runtime import Context
from starlette.responses import HTMLResponse, Response

from app.services.activity import request_changed_environment
from app.services.template_markup import plain_text, safe_markup
from app.web import filters
from app.web.csrf import CSRF_HEADER, attach_csrf_cookie, csrf_token
from app.web.flash import clear_flashes, pending_flashes
from app.web.nav_keys import navigation_key
from app.web.origin_links import with_origin
from app.web.pagination import PageView
from app.web.theme import requested_theme

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

COUNTS_CHANGED_EVENT = "counts-changed"
TRIGGER_HEADER = "HX-Trigger"
_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PARTIALS_PREFIX = "partials"
_PARTIAL_NAME = re.compile(r"[a-z][a-z0-9_]*")
_PRODUCT = "dockmin"


def _product_version() -> str:
    try:
        return metadata.version(_PRODUCT)
    except metadata.PackageNotFoundError:
        return "dev"


PRODUCT_VERSION = _product_version()


def is_htmx_request(request: Request) -> bool:
    """Report whether the caller wants a fragment rather than a full page."""
    headers = request.headers
    if headers.get("HX-Request") != "true":
        return False
    # A boosted navigation is a whole-page load that htmx extracts the body from; a history
    # restore replaces the whole document and so needs one too.
    return (
        headers.get("HX-Boosted") != "true" and headers.get("HX-History-Restore-Request") != "true"
    )


def _modified_version(target: Path) -> int:
    return int(target.stat().st_mtime) if target.is_file() else 0


def _static_versions() -> dict[str, int]:
    return {
        path.relative_to(STATIC_DIR).as_posix(): _modified_version(path)
        for path in STATIC_DIR.rglob("*")
        if path.is_file()
    }


def _static_path_function(versions: Mapping[str, int]) -> Callable[..., str]:
    @pass_context
    def static_path(context: Context, path: str) -> str:
        """Return the URL of a static file, versioned by modification time for cache busting."""
        request: Request = context["request"]
        root = request.scope.get("root_path", "")
        version = versions.get(path)
        if version is None:
            version = _modified_version(STATIC_DIR / path)
        return f"{root}/static/{path}?v={version}"

    return static_path


@pass_context
def _route_path(context: Context, name: str, **params: Any) -> str:
    """Return the relative URL of a named route, never an absolute one."""
    request: Request = context["request"]
    return str(request.app.url_path_for(name, **params))


def _require_pager(value: object) -> PageView:
    """Fail the render unless the value is the cursor contract."""
    if not isinstance(value, PageView):
        raise TypeError(f"a PageView is required to render a list, got {type(value).__name__}")
    return value


def _choice(value: str, allowed: Collection[str]) -> str:
    """Fail the render unless value is one of allowed; keeps free text out of class names."""
    if value not in allowed:
        raise ValueError(f"{value!r} is not one of {sorted(allowed)}")
    return value


def build_environment(*, development_checks: bool = False) -> Environment:
    """Create the Jinja2 environment: autoescaping on, undefined names are errors.

    Outside development checks, templates are compiled once and the static file versions are read
    once; with them on, both are re-read as files change.
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        auto_reload=development_checks,
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        timestamp=filters.timestamp,
        timestamp_iso=filters.timestamp_iso,
        filesize=filters.filesize,
        duration=filters.duration,
        relative_age=filters.relative_age,
        visible_text=filters.visible_text,
        catalogue_markup=safe_markup,
        catalogue_text=plain_text,
    )
    env.globals.update(
        static_path=_static_path_function({} if development_checks else _static_versions()),
        route_path=_route_path,
        require_pager=_require_pager,
        choice=_choice,
        csrf_header=CSRF_HEADER,
        with_origin=with_origin,
    )
    return env


def template_context(request: Request) -> dict[str, Any]:
    """Return the variables every template sees; each is derived from the request alone."""
    state = request.app.state
    navigation = getattr(request.state, "navigation", None)
    route = request.scope.get("route")
    origin = getattr(request.state, "origin", None)
    return {
        # None until the session layer attaches a principal to request.state.
        "principal": getattr(request.state, "principal", None),
        "csrf_token": csrf_token(request),
        "theme": requested_theme(request),
        "flashes": pending_flashes(request),
        "product_version": PRODUCT_VERSION,
        "compose_version": getattr(state, "compose_version", None),
        "nav_environments": () if navigation is None else navigation.environments,
        "nav_environment": None if navigation is None else navigation.current,
        "nav_environment_remembered": navigation is not None and navigation.remembered,
        "nav_counts_pending": getattr(request.state, "nav_counts_pending", False),
        "nav_key": navigation_key(getattr(route, "name", None) or ""),
        "origin": origin,
        "origin_ref": None if origin is None else origin.ref,
    }


def build_templates(*, development_checks: bool = False) -> Jinja2Templates:
    """Create the template environment with autoescaping enabled."""
    return Jinja2Templates(
        env=build_environment(development_checks=development_checks),
        context_processors=[template_context],
    )


def get_templates(request: Request) -> Jinja2Templates:
    """Return the template environment attached to the running application."""
    templates: Jinja2Templates = request.app.state.templates
    return templates


def _with_event(existing: str | None, event: str) -> str:
    """Add an event to an HX-Trigger value, which is a comma list or a JSON object."""
    value = (existing or "").strip()
    if not value:
        return event
    if value.startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if not isinstance(parsed, dict):
            return value
        parsed.setdefault(event, True)
        return json.dumps(parsed)
    names = [name.strip() for name in value.split(",") if name.strip()]
    if event not in names:
        names.append(event)
    return ", ".join(names)


def _announce_changes(request: Request, response: Response) -> None:
    if (
        request.method not in _READ_ONLY_METHODS
        and is_htmx_request(request)
        and 200 <= response.status_code < 300
        and request_changed_environment(request.scope)
    ):
        response.headers[TRIGGER_HEADER] = _with_event(
            response.headers.get(TRIGGER_HEADER), COUNTS_CHANGED_EVENT
        )


def _finish(request: Request, response: Response) -> Response:
    attach_csrf_cookie(request, response)
    _announce_changes(request, response)
    # Full page and fragment share a URL, so a cache must key on the header that picks one.
    response.headers.append("Vary", "HX-Request")
    return response


def render_page(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """Render a full page template, showing and then clearing any pending notices."""
    response = get_templates(request).TemplateResponse(
        request, name, context, status_code=status_code, headers=headers
    )
    clear_flashes(request, response)
    return _finish(request, response)


def render_partial(
    request: Request,
    name: str,
    /,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    **arguments: Any,
) -> Response:
    """Render the macro `name` from `partials/<name>.html` as a bare fragment.

    Both leading parameters are positional-only so a macro argument may itself be called `name`.
    """
    return render_fragment(
        request,
        f"{PARTIALS_PREFIX}/{name}.html",
        name,
        status_code=status_code,
        headers=headers,
        **arguments,
    )


def render_fragment(
    request: Request,
    template_name: str,
    macro_name: str,
    /,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
    **arguments: Any,
) -> Response:
    """Render one macro of any template as a bare fragment.

    The template name must be a constant of the calling route, never request input.
    """
    if not _PARTIAL_NAME.fullmatch(macro_name):
        raise ValueError(f"invalid partial name: {macro_name!r}")
    templates = get_templates(request)
    context: dict[str, Any] = {"request": request}
    for processor in templates.context_processors:
        context.update(processor(request))
    template = templates.get_template(template_name)
    macro = getattr(template.make_module(context), macro_name)
    return _finish(
        request, HTMLResponse(str(macro(**arguments)), status_code=status_code, headers=headers)
    )
