"""Resolves the template a create form was opened from or posted with, and its form panel."""

from dataclasses import dataclass
from typing import Final

from fastapi import Depends, Request

from app.auth.dependencies import CurrentUser
from app.services.assembly import TemplateReads
from app.services.template_prefill import (
    container_prefill,
    env_hints,
    settings_for,
    withheld_notice,
)
from app.services.template_reader import TemplateEntry

TEMPLATE_PARAM: Final = "template"
TEMPLATE_FIELD: Final = "template_ref"
REF_MAX: Final = 128
UNAVAILABLE: Final = "That template is not available to you, so the form is blank."


@dataclass(frozen=True, slots=True)
class TemplateBinding:
    """The template a request names; `entry` is None when it is not available to the viewer."""

    entry: TemplateEntry | None


@dataclass(frozen=True, slots=True)
class TemplatePanel:
    """What a create form says about the template it came from."""

    title: str
    catalogue: str
    href: str | None
    notices: tuple[str, ...]
    hints: tuple[str, ...]


async def resolve_request_template(
    request: Request, actor: CurrentUser, reads: TemplateReads
) -> None:
    """Set `request.state.template` from the `template` query or the `template_ref` field."""
    request.state.template = None
    text: str | None = None
    if request.method == "GET":
        given = request.query_params.getlist(TEMPLATE_PARAM)
        text = given[0] if len(given) == 1 else None
    else:
        value = (await request.form()).get(TEMPLATE_FIELD)
        text = value if isinstance(value, str) else None
    if text and len(text) <= REF_MAX:
        request.state.template = TemplateBinding(await reads.get_template(actor, text))


RequestTemplate = Depends(resolve_request_template)


def bound_entry(request: Request) -> TemplateEntry | None:
    """Return the template the request names when the viewer may use it."""
    binding: TemplateBinding | None = getattr(request.state, "template", None)
    return None if binding is None else binding.entry


def bound_ref(request: Request) -> str | None:
    """Return the reference of the resolved template, or None."""
    entry = bound_entry(request)
    return None if entry is None else entry.ref


def container_values(request: Request, *, is_administrator: bool) -> dict[str, str] | None:
    """Return the container form values of the resolved template, or None."""
    entry = bound_entry(request)
    if entry is None:
        return None
    return container_prefill(entry.template, is_administrator=is_administrator).values


def container_panel(request: Request, *, is_administrator: bool) -> TemplatePanel | None:
    """Describe the template on the container form, or the lack of it on a page load."""
    entry = bound_entry(request)
    if entry is None:
        return _unavailable(request)
    prefill = container_prefill(entry.template, is_administrator=is_administrator)
    notices = tuple(note for note in (prefill.withheld, prefill.not_carried) if note)
    return _panel(request, entry, notices, prefill.hints)


def stack_panel(request: Request, *, is_administrator: bool) -> TemplatePanel | None:
    """Describe the template on the stack form, or the lack of it on a page load."""
    entry = bound_entry(request)
    if entry is None:
        return _unavailable(request)
    settings = settings_for(entry.template, is_administrator=is_administrator)
    notice = withheld_notice(settings)
    return _panel(request, entry, () if notice is None else (notice,), env_hints(settings.env))


def _unavailable(request: Request) -> TemplatePanel | None:
    binding: TemplateBinding | None = getattr(request.state, "template", None)
    if binding is None or request.method != "GET":
        return None
    return TemplatePanel("", "", None, (UNAVAILABLE,), ())


def _panel(
    request: Request, entry: TemplateEntry, notices: tuple[str, ...], hints: tuple[str, ...]
) -> TemplatePanel:
    href = str(
        request.app.url_path_for(
            "template_detail", catalogue_id=entry.catalogue_id, key=entry.template.key
        )
    )
    return TemplatePanel(entry.template.title, entry.catalogue_name, href, notices, hints)
